"""First-connection pairing uses real HA disk checkpoints and single-use exchange."""

import asyncio
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.storage import Store

from custom_components.daylight_calendar_import.hosted_credentials import (
    CredentialCheckpoint,
    HostedCredentialStore,
    PairingAttempt,
    _decode,
    _ImmediatePrivateStore,
)
from custom_components.daylight_calendar_import.hosted_lifecycle import (
    HostedLifecycleController,
    HostedPairingInstructions,
)
from custom_components.daylight_calendar_import.hosted_transport import (
    HostedTransportError,
)
from tests.test_hosted_lifecycle import (
    CANDIDATE,
    CURRENT,
    EXPIRES,
    INSTALLATION,
    status,
)

_REAL_LOAD = Store._async_load
_REAL_WRITE = Store._async_write_data
PAIRING = "pairing_1234567890"
DEADLINE = "2026-10-24T00:10:00Z"


@pytest.fixture
async def pairing(hass, tmp_path, monkeypatch, freezer):
    freezer.move_to("2026-10-24T00:00:00Z")
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_load", _REAL_LOAD)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_write_data", _REAL_WRITE)
    store = HostedCredentialStore(hass, "pairing")
    await store.async_load()
    transport = AsyncMock()
    transport.start_pairing.return_value = {
        "pairing_id": PAIRING, "user_code": "ABCD1234EFGH",
        "verification_uri": "https://account.example.test/connect",
        "expires_at": DEADLINE, "poll_interval_seconds": 5,
    }
    transport.pairing_status.return_value = {
        "pairing_id": PAIRING, "expires_at": DEADLINE, "status": "approved",
    }
    transport.exchange.return_value = {
        "installation_id": INSTALLATION, "installation_token": CANDIDATE.token,
        "expires_at": EXPIRES, "confirmation_expires_at": CANDIDATE.confirmation_expires_at,
    }
    transport.installation_status.return_value = status()
    return HostedLifecycleController(store, transport), store, transport


async def start(c):
    return await c.async_start_pairing(processing_consent=True, installation_label="Home")


async def restart(hass):
    store = HostedCredentialStore(hass, "pairing")
    await store.async_load()
    return store


async def test_verifier_before_creation_exchange_intent_before_issuance(pairing, hass, freezer):
    c, store, transport = pairing
    async def create(verifier, label):
        disk = (await restart(hass)).snapshot
        assert disk.processing_consent and disk.pairing_verifier == verifier
        assert disk.pairing is None and label == "Home"
        return transport.start_pairing.return_value
    transport.start_pairing.side_effect = create
    instructions = await start(c)
    assert isinstance(instructions, HostedPairingInstructions)
    assert instructions.user_code == "ABCD1234EFGH"
    assert instructions.verification_uri == "https://account.example.test/connect"
    assert "ABCD1234EFGH" not in repr(instructions)
    verifier = store.snapshot.pairing_verifier
    assert len(verifier) == 43 and verifier not in repr(instructions)
    assert instructions.expires_at == "2026-10-24T00:10:00+00:00"
    assert instructions.poll_interval_seconds == 5
    assert (await c.async_poll_pairing()).state == "pairing_wait"
    transport.pairing_status.assert_not_called()
    async def exchange(pairing_id, secret):
        disk = (await restart(hass)).snapshot
        assert disk.pairing.exchange_attempted and disk.candidate is None
        assert pairing_id == PAIRING and secret == verifier
        return transport.exchange.return_value
    transport.exchange.side_effect = exchange
    freezer.tick(5)
    assert (await c.async_poll_pairing()).state == "candidate_saved"
    transport.pairing_status.assert_awaited_once_with(PAIRING, verifier)
    saved = (await restart(hass)).snapshot
    assert saved.current is None and saved.candidate == CANDIDATE
    transport.confirm.assert_not_called()
    assert (await c.async_poll_pairing()).state == "recovery_required"
    transport.exchange.assert_awaited_once()
    assert (await c.async_reconcile()).state == "connected"
    final = (await restart(hass)).snapshot
    assert final.current.token == CANDIDATE.token
    assert final.pairing is None and final.pairing_verifier is None


@pytest.mark.parametrize("failure", [HostedTransportError("service_unavailable", retryable=True), asyncio.CancelledError()])
@pytest.mark.parametrize("operation", ["start_pairing", "exchange"])
async def test_lost_mutation_response_never_replayed_on_restart(pairing, hass, freezer, failure, operation):
    c, _store, transport = pairing
    if operation == "exchange":
        await start(c)
        freezer.tick(5)
    getattr(transport, operation).side_effect = failure
    with pytest.raises(type(failure)):
        if operation == "start_pairing":
            await start(c)
        else:
            await c.async_poll_pairing()
    recovered = await restart(hass)
    assert recovered.snapshot.pairing_verifier is not None
    assert recovered.snapshot.candidate is None and recovered.snapshot.current is None
    assert (await HostedLifecycleController(recovered, transport).async_poll_pairing()).state == "fresh_pairing_required"
    assert getattr(transport, operation).await_count == 1
    transport.confirm.assert_not_called()


async def test_new_explicit_pairing_abandons_old_verifier(pairing):
    c, store, transport = pairing
    transport.start_pairing.side_effect = HostedTransportError("service_unavailable")
    with pytest.raises(HostedTransportError):
        await start(c)
    old = store.snapshot.pairing_verifier
    transport.start_pairing.side_effect = None
    await start(c)
    assert store.snapshot.pairing_verifier != old
    assert transport.start_pairing.await_count == 2


@pytest.mark.parametrize("consent", [False, None, "yes", 1])
async def test_consent_is_explicit_boolean_before_any_io(pairing, consent):
    c, store, transport = pairing
    original = store.snapshot
    result = await c.async_start_pairing(processing_consent=consent, installation_label="Home")
    assert result.state == "consent_required"
    assert not transport.mock_calls and store.snapshot is original


@pytest.mark.parametrize("changes,expected", [
    ({"processing_consent": True, "current": CURRENT}, "reconciliation_required"),
    ({"processing_consent": True, "candidate": CANDIDATE}, "reconciliation_required"),
    ({"local_disconnect": "unconfirmed"}, "remote_disconnect_unconfirmed"),
    ({"local_disconnect": "confirmed"}, "disconnected"),
])
async def test_start_cannot_bypass_replacement_or_disconnect(pairing, changes, expected):
    c, store, transport = pairing
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, **changes))
    assert (await start(c)).state == expected
    assert not transport.mock_calls


@pytest.mark.parametrize("changes,expected", [
    ({}, "disconnected"),
    ({"processing_consent": True}, "fresh_pairing_required"),
    ({"processing_consent": True, "current": CURRENT}, "reconciliation_required"),
    ({"processing_consent": True, "candidate": CANDIDATE}, "recovery_required"),
    ({"local_disconnect": "unconfirmed"}, "remote_disconnect_unconfirmed"),
])
async def test_poll_ineligible_state_has_no_io(pairing, changes, expected):
    c, store, transport = pairing
    old = store.snapshot
    await store.async_checkpoint(old, replace(old, **changes))
    assert (await c.async_poll_pairing()).state == expected
    assert not transport.mock_calls


@pytest.mark.parametrize("remote", ["pending", "exchanged"])
async def test_nonapproved_status_never_exchanges_and_polls_are_bounded(pairing, freezer, remote):
    c, store, transport = pairing
    await start(c)
    freezer.tick(5)
    transport.pairing_status.return_value["status"] = remote
    assert (await c.async_poll_pairing()).state == ("pairing_pending" if remote == "pending" else "fresh_pairing_required")
    assert (await c.async_poll_pairing()).state == "pairing_wait"
    transport.pairing_status.assert_awaited_once()
    transport.exchange.assert_not_called()
    assert store.snapshot.pairing.next_poll_at == "2026-10-24T00:00:25+00:00"


async def test_rate_limit_survives_restart_and_honors_retry_after(pairing, hass, freezer):
    c, _store, transport = pairing
    await start(c)
    freezer.tick(5)
    transport.pairing_status.side_effect = HostedTransportError("rate_limited", status=429, retryable=True, retry_after=300)
    with pytest.raises(HostedTransportError):
        await c.async_poll_pairing()
    recovered = await restart(hass)
    assert recovered.snapshot.pairing.next_poll_at == "2026-10-24T00:05:05+00:00"
    freezer.tick(299)
    assert (await HostedLifecycleController(recovered, transport).async_poll_pairing()).state == "pairing_wait"
    transport.pairing_status.assert_awaited_once()


async def test_transient_poll_failure_preserves_bounded_gate(pairing, freezer):
    c, store, transport = pairing
    await start(c)
    freezer.tick(5)
    transport.pairing_status.side_effect = HostedTransportError("service_unavailable")
    with pytest.raises(HostedTransportError):
        await c.async_poll_pairing()
    assert (await c.async_poll_pairing()).state == "pairing_wait"
    transport.exchange.assert_not_called()
    assert store.snapshot.pairing.exchange_attempted is False


async def test_expired_pairing_stops_polling(pairing, freezer):
    c, _store, transport = pairing
    await start(c)
    freezer.tick(600)
    assert (await c.async_poll_pairing()).state == "fresh_pairing_required"
    transport.pairing_status.assert_not_called()


async def test_pairing_expiry_is_immutable(pairing, freezer):
    c, store, transport = pairing
    await start(c)
    freezer.tick(5)
    transport.pairing_status.return_value["expires_at"] = "2026-10-24T00:10:01Z"
    with pytest.raises(HostedTransportError, match="^identity_mismatch$"):
        await c.async_poll_pairing()
    transport.exchange.assert_not_called()
    assert store.snapshot.pairing.exchange_attempted is False


async def test_overlong_creation_expiry_not_saved(pairing):
    c, store, transport = pairing
    transport.start_pairing.return_value["expires_at"] = "2026-10-24T00:10:31Z"
    with pytest.raises(HostedTransportError, match="^invalid_response$"):
        await start(c)
    assert store.snapshot.pairing is None and store.snapshot.pairing_verifier is not None


@pytest.mark.parametrize("stage", ["verifier", "creation", "poll", "exchange_intent", "candidate"])
async def test_failed_checkpoint_cannot_authorize_next_network_step(pairing, freezer, monkeypatch, stage):
    c, store, transport = pairing
    if stage in ("poll", "exchange_intent", "candidate"):
        await start(c)
        freezer.tick(5)
    original = store.async_checkpoint
    count = 0
    target = {"verifier": 1, "creation": 2, "poll": 1, "exchange_intent": 2, "candidate": 3}[stage]
    async def checkpoint(*args):
        nonlocal count
        count += 1
        if count == target:
            raise RuntimeError("storage unavailable")
        return await original(*args)
    monkeypatch.setattr(store, "async_checkpoint", checkpoint)
    with pytest.raises(RuntimeError):
        if stage in ("verifier", "creation"):
            await start(c)
        else:
            await c.async_poll_pairing()
    assert transport.start_pairing.await_count == (0 if stage == "verifier" else 1)
    assert transport.exchange.await_count == (1 if stage == "candidate" else 0)
    assert transport.pairing_status.await_count == (1 if stage in ("exchange_intent", "candidate") else 0)
    transport.confirm.assert_not_called()
    assert store.snapshot.candidate is None


async def test_slow_poll_checkpoint_does_not_send_with_stale_gate(pairing, freezer, monkeypatch):
    c, store, transport = pairing
    await start(c)
    freezer.tick(5)
    original = store.async_checkpoint
    async def slow(*args):
        result = await original(*args)
        freezer.tick(16)
        return result
    monkeypatch.setattr(store, "async_checkpoint", slow)
    assert (await c.async_poll_pairing()).state == "pairing_wait"
    transport.pairing_status.assert_not_called()


@pytest.mark.parametrize("changes", [
    {"poll_interval_seconds": True}, {"poll_interval_seconds": 4},
    {"poll_interval_seconds": 31}, {"exchange_attempted": 1},
    {"pairing_id": "bad"},
])
def test_pairing_metadata_rejects_invalid_types(changes):
    with pytest.raises(ValueError):
        PairingAttempt(**{"pairing_id": PAIRING, "expires_at": DEADLINE, "poll_interval_seconds": 5, "next_poll_at": DEADLINE, **changes})


def test_checkpoint_requires_verifier_and_decodes_legacy():
    attempt = PairingAttempt(PAIRING, DEADLINE, 5, DEADLINE)
    for pairing in (attempt, {}):
        with pytest.raises(ValueError, match="pairing metadata requires"):
            CredentialCheckpoint(processing_consent=True, pairing=pairing)
    checkpoint = CredentialCheckpoint(processing_consent=True, pairing_verifier="v" * 43, pairing=attempt)
    assert _decode(asdict(checkpoint)) == checkpoint
    with pytest.raises(ValueError):
        _decode({**asdict(CredentialCheckpoint()), "pairing": []})
    data = asdict(CredentialCheckpoint())
    for optional in ("pairing", "rotation_not_before", "local_disconnect"):
        del data[optional]
        assert _decode(data) == CredentialCheckpoint()


async def test_concurrent_pollers_exchange_only_once(pairing, freezer):
    c, _store, transport = pairing
    await start(c)
    freezer.tick(5)
    results = await asyncio.gather(c.async_poll_pairing(), c.async_poll_pairing())
    assert {result.state for result in results} == {"candidate_saved", "recovery_required"}
    transport.exchange.assert_awaited_once()
    transport.pairing_status.assert_awaited_once()


async def test_bad_exchange_deadline_never_saves_or_replays_candidate(pairing, hass, freezer):
    c, store, transport = pairing
    await start(c)
    freezer.tick(5)
    transport.exchange.return_value["expires_at"] = "2027-10-24T00:00:00Z"
    with pytest.raises(HostedTransportError, match="^invalid_response$"):
        await c.async_poll_pairing()
    assert store.snapshot.candidate is None
    recovered = await restart(hass)
    assert (await HostedLifecycleController(recovered, transport).async_poll_pairing()).state == "fresh_pairing_required"
    transport.exchange.assert_awaited_once()
    transport.confirm.assert_not_called()


async def test_new_explicit_creation_durably_abandons_existing_pairing(pairing, hass):
    c, store, transport = pairing
    await start(c)
    previous = store.snapshot.pairing_verifier
    assert store.snapshot.pairing is not None

    async def lost_creation(verifier, _label):
        disk = (await restart(hass)).snapshot
        assert verifier != previous and disk.pairing_verifier == verifier
        assert disk.pairing is None
        raise HostedTransportError("service_unavailable")

    transport.start_pairing.side_effect = lost_creation
    with pytest.raises(HostedTransportError, match="^service_unavailable$"):
        await start(c)
    recovered = await restart(hass)
    assert recovered.snapshot.pairing is None
    assert (await HostedLifecycleController(recovered, transport).async_poll_pairing()).state == "fresh_pairing_required"
    transport.exchange.assert_not_called()


async def test_pairing_creation_accepts_exact_maximum_deadline(pairing):
    c, store, transport = pairing
    transport.start_pairing.return_value["expires_at"] = "2026-10-24T00:10:30Z"
    instructions = await start(c)
    assert instructions.expires_at == "2026-10-24T00:10:30+00:00"
    assert store.snapshot.pairing.expires_at == instructions.expires_at
    transport.exchange.assert_not_called()


async def test_poll_checkpoint_at_exact_safety_margin_can_send(pairing, freezer, monkeypatch):
    c, store, transport = pairing
    await start(c)
    verifier = store.snapshot.pairing_verifier
    freezer.tick(5)
    original = store.async_checkpoint

    async def delayed(*args):
        result = await original(*args)
        freezer.tick(15)
        return result

    monkeypatch.setattr(store, "async_checkpoint", delayed)
    transport.pairing_status.return_value["status"] = "pending"
    assert (await c.async_poll_pairing()).state == "pairing_pending"
    transport.pairing_status.assert_awaited_once_with(PAIRING, verifier)
    transport.exchange.assert_not_called()


async def test_confirmed_disconnect_cannot_poll_pairing(pairing):
    c, store, transport = pairing
    initial = store.snapshot
    await store.async_checkpoint(initial, replace(initial, local_disconnect="confirmed"))
    assert (await c.async_poll_pairing()).state == "disconnected"
    assert not transport.mock_calls


def test_pairing_metadata_accepts_maximum_poll_interval():
    attempt = PairingAttempt(PAIRING, DEADLINE, 30, DEADLINE)
    checkpoint = CredentialCheckpoint(
        processing_consent=True, pairing_verifier="v" * 43, pairing=attempt,
    )
    assert _decode(asdict(checkpoint)).pairing.poll_interval_seconds == 30
