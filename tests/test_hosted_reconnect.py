"""Explicit reconnect retains old namespaces and requires live local receipts."""

import asyncio
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.storage import Store

from custom_components.daylight_calendar_import.hosted_credentials import (
    AutomationAttempt,
    AutomationState,
    CandidateCredential,
    CredentialCheckpoint,
    HostedCredentialStore,
    PairingAttempt,
    ReconnectIntent,
    _decode,
    _ImmediatePrivateStore,
)
from custom_components.daylight_calendar_import.hosted_lifecycle import (
    HostedLifecycleController,
)
from custom_components.daylight_calendar_import.hosted_namespace import (
    NamespaceReconciliationError,
)
from custom_components.daylight_calendar_import.hosted_transport import (
    HostedServiceBinding,
    HostedTransportError,
)
from tests.test_hosted_lifecycle import ACCOUNT, CANDIDATE, CURRENT, EXPIRES, status

_REAL_LOAD = Store._async_load
_REAL_WRITE = Store._async_write_data
CONTRACT = "pending-v2-local-only"
SERVICE = HostedServiceBinding("https://api.example.test", "https://account.example.test")
NEW_ID = "replacement_1234567890"
PAIRING_ID = "pairing_reconnect_1234567890"


@pytest.fixture
async def reconnect(hass, tmp_path, monkeypatch, freezer):
    freezer.move_to("2026-10-24T00:00:00Z")
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_load", _REAL_LOAD)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_write_data", _REAL_WRITE)
    store = HostedCredentialStore(hass, "reconnect")
    empty = await store.async_load()
    await store.async_checkpoint(empty, replace(
        empty, service_binding=SERVICE, processing_consent=True, current=CURRENT,
    ))
    transport = AsyncMock()
    transport.service_binding = SERVICE
    transport.start_pairing.return_value = {
        "pairing_id": PAIRING_ID, "user_code": "ABCD1234EFGH",
        "verification_uri": "https://account.example.test/connect",
        "expires_at": "2026-10-24T00:10:00Z", "poll_interval_seconds": 5,
    }
    transport.pairing_status.return_value = {
        "pairing_id": PAIRING_ID, "status": "approved", "expires_at": "2026-10-24T00:10:00Z",
    }
    transport.exchange.return_value = {
        "installation_id": NEW_ID, "installation_token": CANDIDATE.token,
        "expires_at": EXPIRES, "confirmation_expires_at": "2026-10-24T00:10:00Z",
    }
    transport.installation_status.return_value = status(installation_id=NEW_ID)
    transport.confirm.return_value = {"installation_id": NEW_ID, "expires_at": EXPIRES}
    adapter = AsyncMock()
    adapter.async_receipt.return_value = CONTRACT
    controller = HostedLifecycleController(store, transport, adapter)
    return controller, store, transport, adapter


async def start(c, consent=True):
    return await c.async_start_reconnect(processing_consent=consent, installation_label="Replacement HA")


async def restarted(hass, transport, adapter=None):
    store = HostedCredentialStore(hass, "reconnect")
    await store.async_load()
    return HostedLifecycleController(store, transport, adapter), store


async def stage(reconnect, freezer):
    c, store, _transport, _adapter = reconnect
    await start(c)
    freezer.tick(5)
    assert (await c.async_poll_pairing()).state == "candidate_saved"
    return store.snapshot.candidate


@pytest.mark.parametrize("marker", ["none", "unconfirmed", "confirmed"])
async def test_real_disk_preserves_stop_namespace_and_candidate_until_authenticated_promotion(reconnect, hass, freezer, marker):
    c, store, transport, adapter = reconnect
    old = store.snapshot
    await store.async_checkpoint(old, replace(
        old, local_disconnect=marker, current=None if marker == "confirmed" else CURRENT,
        processing_consent=marker != "confirmed",
    ))
    before = store.snapshot
    async def create(*_args):
        _again, loaded = await restarted(hass, transport, adapter)
        saved = loaded.snapshot
        assert saved.current == before.current and saved.local_disconnect == marker
        assert saved.reconnect.prior_disconnect == marker
        assert saved.reconnect.namespace == (before.current.namespace if before.current else None)
        assert saved.pairing_verifier and saved.candidate is None
        return transport.start_pairing.return_value
    transport.start_pairing.side_effect = create
    await start(c)
    assert (await c.async_reconcile()).state == "pairing_pending"
    c, store = await restarted(hass, transport, adapter)
    freezer.tick(5)
    assert (await c.async_poll_pairing()).state == "candidate_saved"
    transport.installation_status.side_effect = [HostedTransportError("unauthenticated", status=401), status(installation_id=NEW_ID)]
    async def confirm(token):
        _again, loaded = await restarted(hass, transport, adapter)
        saved = loaded.snapshot
        assert saved.current == before.current and saved.local_disconnect == marker
        assert saved.candidate.token == token and saved.candidate.confirmation_attempts == 1
        assert saved.reconnect is not None
        return transport.confirm.return_value
    transport.confirm.side_effect = confirm
    assert (await c.async_reconcile()).state == "connected"
    _again, loaded = await restarted(hass, transport, adapter)
    final = loaded.snapshot
    assert final.current.installation_id == NEW_ID and final.current.token == CANDIDATE.token
    assert final.local_disconnect == "none" and final.reconnect is None
    assert final.pairing is None and final.pairing_verifier is None and final.candidate is None
    assert final.automation is None
    transport.confirm.assert_awaited_once_with(CANDIDATE.token)
    transport.disconnect.assert_not_called()


async def test_default_deny_cannot_turn_old_stop_or_candidate_into_authority(reconnect, hass, freezer):
    _c, store, transport, _adapter = reconnect
    before = store.snapshot
    denied = HostedLifecycleController(store, transport)
    assert (await start(denied)).state == "reconciliation_required"
    assert store.snapshot is before and not transport.mock_calls
    await stage(reconnect, freezer)
    transport.reset_mock()
    denied, loaded = await restarted(hass, transport)
    original = loaded.snapshot
    assert (await denied.async_poll_pairing()).state == "reconciliation_required"
    assert (await denied.async_reconcile()).state == "reconciliation_required"
    assert loaded.snapshot is original and not transport.mock_calls


@pytest.mark.parametrize("receipt", [None, True, "unknown-contract", NamespaceReconciliationError("unsupported")])
async def test_bad_receipt_never_writes_or_sends(reconnect, receipt):
    c, store, transport, adapter = reconnect
    before = store.snapshot
    if isinstance(receipt, Exception):
        adapter.async_receipt.side_effect = receipt
    else:
        adapter.async_receipt.return_value = receipt
    assert (await start(c)).state == "reconciliation_required"
    assert store.snapshot is before and not transport.mock_calls


@pytest.mark.parametrize("consent", [False, 1, None])
async def test_reconnect_needs_explicit_boolean_consent(reconnect, consent):
    c, store, transport, adapter = reconnect
    before = store.snapshot
    assert (await start(c, consent)).state == "consent_required"
    assert store.snapshot is before and not transport.mock_calls
    adapter.async_receipt.assert_not_called()


async def test_existing_unresolved_candidate_is_not_discarded(reconnect):
    c, store, transport, adapter = reconnect
    before = store.snapshot
    await store.async_checkpoint(before, replace(before, candidate=CANDIDATE))
    before = store.snapshot
    assert (await start(c)).state == "reconciliation_required"
    assert store.snapshot is before and not transport.mock_calls
    adapter.async_receipt.assert_not_called()


@pytest.mark.parametrize("field", ["api_origin", "account_origin"])
async def test_changed_service_never_receives_old_or_new_recovery_material(reconnect, field):
    c, store, transport, _adapter = reconnect
    await start(c)
    transport.reset_mock()
    values = asdict(SERVICE)
    values[field] = "https://other.example.test"
    transport.service_binding = HostedServiceBinding(**values)
    for operation in (start, lambda controller: controller.async_poll_pairing(), lambda controller: controller.async_reconcile()):
        with pytest.raises(HostedTransportError, match="service_reconciliation_required"):
            await operation(c)
    assert not transport.mock_calls and store.snapshot.current == CURRENT


async def test_receipt_revalidated_after_intent_before_creation(reconnect):
    c, store, transport, adapter = reconnect
    adapter.async_receipt.side_effect = [CONTRACT, "unsupported"]
    assert (await start(c)).state == "reconciliation_required"
    assert store.snapshot.reconnect is not None and store.snapshot.pairing_verifier
    assert store.snapshot.current == CURRENT and not transport.mock_calls


@pytest.mark.parametrize("phase", ["poll", "before_exchange", "exchange_intent"])
async def test_no_exchange_when_receipt_becomes_unavailable(reconnect, freezer, phase):
    c, store, transport, adapter = reconnect
    await start(c)
    transport.reset_mock()
    count = {"poll": 0, "before_exchange": 1, "exchange_intent": 2}[phase]
    adapter.async_receipt.side_effect = [CONTRACT] * count + ["unsupported"]
    freezer.tick(5)
    assert (await c.async_poll_pairing()).state == "reconciliation_required"
    transport.exchange.assert_not_called()
    assert store.snapshot.current == CURRENT
    assert bool(store.snapshot.pairing.exchange_attempted) is (phase == "exchange_intent")


@pytest.mark.parametrize("phase", ["before_status", "before_confirm", "before_promotion"])
async def test_candidate_activation_and_promotion_require_fresh_receipt(reconnect, freezer, phase):
    c, store, transport, adapter = reconnect
    await stage(reconnect, freezer)
    transport.reset_mock()
    if phase == "before_status":
        adapter.async_receipt.return_value = "unsupported"
    elif phase == "before_confirm":
        adapter.async_receipt.side_effect = [CONTRACT, "unsupported"]
        transport.installation_status.side_effect = HostedTransportError("unauthenticated", status=401)
    else:
        adapter.async_receipt.side_effect = [CONTRACT, "unsupported"]
    assert (await c.async_reconcile()).state == "reconciliation_required"
    assert store.snapshot.current == CURRENT and store.snapshot.candidate is not None
    transport.confirm.assert_not_called()
    if phase == "before_status":
        transport.installation_status.assert_not_called()


async def test_wrong_household_never_promotes_or_relabels_old_namespace(reconnect, freezer):
    c, store, transport, _adapter = reconnect
    await stage(reconnect, freezer)
    transport.installation_status.return_value = status(installation_id=NEW_ID, account_id=ACCOUNT + "other")
    before = store.snapshot
    assert (await c.async_reconcile()).state == "reconciliation_required"
    assert store.snapshot is before and store.snapshot.current == CURRENT
    assert store.snapshot.candidate.installation_id == NEW_ID
    transport.disconnect.assert_not_called()


async def test_reconnect_requires_new_installation_identity(reconnect, freezer):
    c, store, transport, _adapter = reconnect
    await stage(reconnect, freezer)
    before = store.snapshot
    await store.async_checkpoint(before, replace(before, candidate=replace(before.candidate, installation_id=CURRENT.installation_id)))
    transport.reset_mock()
    assert (await c.async_reconcile()).state == "reconciliation_required"
    assert not transport.mock_calls and store.snapshot.current == CURRENT


@pytest.mark.parametrize("phase", ["creation", "exchange", "confirmation"])
async def test_cancelled_remote_outcome_preserves_durable_intent_without_post_replay(reconnect, hass, freezer, phase):
    c, _store, transport, adapter = reconnect
    if phase == "creation":
        transport.start_pairing.side_effect = asyncio.CancelledError
        with pytest.raises(asyncio.CancelledError):
            await start(c)
    else:
        await start(c)
        freezer.tick(5)
        if phase == "exchange":
            transport.exchange.side_effect = asyncio.CancelledError
            with pytest.raises(asyncio.CancelledError):
                await c.async_poll_pairing()
        else:
            assert (await c.async_poll_pairing()).state == "candidate_saved"
            transport.installation_status.side_effect = HostedTransportError("unauthenticated", status=401)
            transport.confirm.side_effect = asyncio.CancelledError
            with pytest.raises(asyncio.CancelledError):
                await c.async_reconcile()
    c, loaded = await restarted(hass, transport, adapter)
    assert loaded.snapshot.current == CURRENT and loaded.snapshot.reconnect is not None
    if phase == "confirmation":
        assert loaded.snapshot.candidate.confirmation_attempts == 1
        transport.installation_status.side_effect = None
        transport.installation_status.return_value = status(installation_id=NEW_ID)
        assert (await c.async_reconcile()).state == "connected"
        assert transport.confirm.await_count == 1
    else:
        assert (await c.async_poll_pairing()).state == "fresh_pairing_required"
        assert transport.start_pairing.await_count == 1
        assert transport.exchange.await_count == (1 if phase == "exchange" else 0)


async def test_explicit_disconnect_cancels_reconnect_authority_but_keeps_uncertain_bundles(reconnect, freezer):
    c, store, transport, _adapter = reconnect
    await stage(reconnect, freezer)
    candidate = store.snapshot.candidate
    transport.reset_mock()
    assert (await c.async_disconnect()).state == "remote_disconnect_unconfirmed"
    saved = store.snapshot
    assert saved.reconnect is None and saved.local_disconnect == "unconfirmed"
    assert saved.current == CURRENT and saved.candidate == candidate
    assert (await c.async_reconcile()).state == "remote_disconnect_unconfirmed"
    assert not transport.mock_calls


async def test_ordinary_operations_cannot_replace_reconnect_intent(reconnect):
    c, store, transport, _adapter = reconnect
    await start(c)
    before = store.snapshot
    transport.reset_mock()
    assert (await c.async_start_pairing(processing_consent=True, installation_label="Wrong flow")).state == "reconciliation_required"
    assert (await c.async_rotate()).state == "reconciliation_required"
    assert store.snapshot is before and not transport.mock_calls


def intent(**changes):
    return ReconnectIntent(**{
        "prior_account_id": ACCOUNT, "prior_installation_id": CURRENT.installation_id,
        "prior_disconnect": "none", "journal_contract": CONTRACT, **changes,
    })


@pytest.mark.parametrize("changes", [
    {"prior_account_id": None}, {"prior_installation_id": None}, {"prior_account_id": "short"},
    {"prior_installation_id": 1}, {"prior_disconnect": "unknown"}, {"journal_contract": "unknown"},
    {"journal_contract": True},
])
def test_reconnect_intent_is_closed_typed_local_metadata(changes):
    with pytest.raises(ValueError):
        intent(**changes)


def test_checkpoint_requires_consistent_prior_stop_namespace_and_explicit_consent():
    values = {"service_binding": SERVICE, "processing_consent": True, "current": CURRENT,
        "pairing_verifier": "v" * 43, "reconnect": intent()}
    checkpoint = CredentialCheckpoint(**values)
    assert _decode(asdict(checkpoint)) == checkpoint
    legacy = asdict(CredentialCheckpoint())
    del legacy["reconnect"]
    assert _decode(legacy).reconnect is None
    for changes in [
        {"reconnect": {}}, {"processing_consent": False}, {"service_binding": None},
        {"pairing_verifier": None}, {"current": None}, {"local_disconnect": "unconfirmed"},
    ]:
        with pytest.raises(ValueError):
            CredentialCheckpoint(**{**values, **changes})
    raw = asdict(checkpoint)
    raw["reconnect"]["unexpected"] = True
    with pytest.raises(TypeError):
        _decode(raw)
    with pytest.raises(ValueError):
        _decode({**asdict(checkpoint), "reconnect": []})
    confirmed = CredentialCheckpoint(
        service_binding=SERVICE, local_disconnect="confirmed", processing_consent=True,
        pairing_verifier="v" * 43,
        reconnect=intent(prior_account_id=None, prior_installation_id=None, prior_disconnect="confirmed"),
    )
    assert _decode(asdict(confirmed)) == confirmed
    with pytest.raises(ValueError):
        CredentialCheckpoint(**{**values, "local_disconnect": "confirmed", "reconnect": intent(prior_disconnect="confirmed")})


async def test_overlap_candidate_cannot_clear_old_namespace_or_stop(reconnect, freezer):
    c, store, transport, _adapter = reconnect
    await stage(reconnect, freezer)
    before = store.snapshot
    transport.installation_status.return_value = status(installation_id=NEW_ID, credential_status="overlap")
    assert (await c.async_reconcile()).state == "reconciliation_required"
    assert store.snapshot is before and store.snapshot.current == CURRENT


async def test_implausible_creation_expiry_keeps_only_durable_reconnect_intent(reconnect):
    c, store, transport, _adapter = reconnect
    transport.start_pairing.return_value["expires_at"] = "2026-10-24T00:10:31Z"
    with pytest.raises(HostedTransportError, match="invalid_response"):
        await start(c)
    assert store.snapshot.reconnect is not None and store.snapshot.pairing is None
    assert store.snapshot.current == CURRENT


@pytest.mark.parametrize("failure", ["unauthorized", "budget_exhausted"])
async def test_failed_candidate_does_not_resume_stopped_prior_namespace(reconnect, freezer, failure):
    c, store, transport, _adapter = reconnect
    await stage(reconnect, freezer)
    before = store.snapshot
    if failure == "budget_exhausted":
        await store.async_checkpoint(before, replace(before, candidate=replace(before.candidate, confirmation_attempts=2)))
    transport.installation_status.side_effect = HostedTransportError("unauthenticated", status=401)
    transport.confirm.side_effect = HostedTransportError("forbidden", status=403)
    assert (await c.async_reconcile()).state == "reconciliation_required"
    assert transport.installation_status.await_count == 1
    assert transport.confirm.await_count == (1 if failure == "unauthorized" else 0)
    assert store.snapshot.current == CURRENT and store.snapshot.reconnect is not None


@pytest.mark.parametrize("phase", ["intent", "creation", "candidate", "promotion"])
async def test_real_disk_write_failure_never_authorizes_next_step(reconnect, hass, freezer, monkeypatch, phase):
    c, store, transport, adapter = reconnect
    if phase in ("candidate", "promotion"):
        await start(c)
        freezer.tick(5)
        if phase == "promotion":
            assert (await c.async_poll_pairing()).state == "candidate_saved"
    original = store._store.async_save
    count = 0
    target = {"intent": 1, "creation": 2, "candidate": 3, "promotion": 1}[phase]
    async def save(data):
        nonlocal count
        count += 1
        if count == target:
            raise OSError("secret path")
        return await original(data)
    monkeypatch.setattr(store._store, "async_save", save)
    from custom_components.daylight_calendar_import.hosted_credentials import (
        HostedCredentialStorageError,
    )
    with pytest.raises(HostedCredentialStorageError, match="preserve the original file"):
        if phase in ("intent", "creation"):
            await start(c)
        elif phase == "candidate":
            await c.async_poll_pairing()
        else:
            await c.async_reconcile()
    recovered, loaded = await restarted(hass, transport, adapter)
    assert loaded.snapshot.current == CURRENT
    assert transport.start_pairing.await_count == (0 if phase == "intent" else 1)
    assert transport.exchange.await_count == (1 if phase in ("candidate", "promotion") else 0)
    if phase == "promotion":
        assert loaded.snapshot.candidate is not None and loaded.snapshot.reconnect is not None
        assert (await recovered.async_reconcile()).state == "connected"
    if phase == "candidate":
        assert loaded.snapshot.candidate is None and loaded.snapshot.pairing.exchange_attempted
        assert (await recovered.async_poll_pairing()).state == "fresh_pairing_required"
        assert transport.exchange.await_count == 1


async def test_cancelled_intent_waiter_does_not_send_before_owned_disk_write_settles(reconnect, hass, monkeypatch):
    c, store, transport, adapter = reconnect
    entered, release = asyncio.Event(), asyncio.Event()
    original = store._store.async_save
    async def delayed(data):
        entered.set()
        await release.wait()
        return await original(data)
    monkeypatch.setattr(store._store, "async_save", delayed)
    operation = asyncio.create_task(start(c))
    await entered.wait()
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation
    transport.start_pairing.assert_not_called()
    release.set()
    recovered, loaded = await restarted(hass, transport, adapter)
    assert loaded.snapshot.current == CURRENT and loaded.snapshot.reconnect is not None
    assert loaded.snapshot.pairing is None
    assert (await recovered.async_reconcile()).state == "fresh_pairing_required"
    transport.start_pairing.assert_not_called()


@pytest.mark.parametrize("case, invalid, expected", [
    ("candidate_confirmation_limit", lambda: CandidateCredential(
        CANDIDATE.installation_id, CANDIDATE.token, CANDIDATE.expires_at,
        CANDIDATE.confirmation_expires_at, 3,
    ), "invalid confirmation budget"),
    ("pairing_poll_interval", lambda: PairingAttempt(
        PAIRING_ID, "2026-10-24T00:10:00Z", 4, "2026-10-24T00:00:00Z",
    ), "invalid pairing recovery metadata"),
    ("automatic_attempt_limit", lambda: AutomationAttempt(
        "a" * 64, 33, "2026-10-24T00:00:00Z",
    ), "invalid automatic attempt budget"),
    ("automatic_state_type", lambda: AutomationState(status=object()), "invalid automatic state"),
    ("automatic_state_envelope", lambda: AutomationState.decode([]), "invalid automatic state"),
    ("automatic_state_record", lambda: AutomationState.decode({
        "status": [], "pairing": None, "candidate": None, "rotation": None,
    }), "invalid automatic budget"),
    ("partial_prior_namespace", lambda: intent(prior_account_id=None), "incomplete prior namespace"),
    ("prior_disconnect_marker", lambda: intent(prior_disconnect="unknown"), "invalid prior disconnect marker"),
    ("unknown_local_journal", lambda: intent(journal_contract="future"), "unsupported namespace contract"),
    ("service_binding_type", lambda: CredentialCheckpoint(service_binding=object()), "invalid service binding"),
    ("automatic_without_consent", lambda: CredentialCheckpoint(
        automation=AutomationState(),
    ), "automatic budgets require processing consent"),
    ("invalid_reconnect_authority", lambda: CredentialCheckpoint(
        reconnect=intent(),
    ), "invalid reconnect intent"),
    ("changed_prior_stop", lambda: CredentialCheckpoint(
        service_binding=SERVICE, processing_consent=True, current=CURRENT,
        pairing_verifier="v" * 43, reconnect=intent(prior_disconnect="confirmed"),
    ), "reconnect changed prior namespace or stop"),
    ("invalid_disconnect_marker", lambda: CredentialCheckpoint(
        local_disconnect="future",
    ), "invalid local disconnect state"),
    ("confirmed_retains_current", lambda: CredentialCheckpoint(
        local_disconnect="confirmed", processing_consent=True, current=CURRENT,
    ), "confirmed disconnect retains credential material"),
    ("pairing_without_verifier", lambda: CredentialCheckpoint(
        processing_consent=True, pairing=PairingAttempt(
            PAIRING_ID, "2026-10-24T00:10:00Z", 5, "2026-10-24T00:00:00Z",
        ),
    ), "pairing metadata requires a verifier"),
    ("rotation_without_current", lambda: CredentialCheckpoint(
        processing_consent=True, rotation_not_before="2026-10-24T00:00:00Z",
    ), "rotation intent requires a current credential"),
], ids=lambda item: item if isinstance(item, str) else None)
def test_durable_hosted_metadata_rejects_invalid_states_with_specific_repair_reason(case, invalid, expected):
    """Invalid recovery state must fail with its precise invariant, never a generic error."""
    with pytest.raises(ValueError) as failure:
        invalid()
    assert str(failure.value) == expected, case


@pytest.mark.parametrize("marker", ["none", "confirmed"])
async def test_unbound_pristine_reconnect_is_read_only_without_relabelling_legacy_stop(reconnect, marker):
    c, store, transport, adapter = reconnect
    old = store.snapshot
    await store.async_checkpoint(old, CredentialCheckpoint(local_disconnect=marker, revision=old.revision))
    before = store.snapshot
    if marker == "none":
        assert (await start(c)).state == "first_connection_required"
    else:
        with pytest.raises(HostedTransportError, match="service_reconciliation_required"):
            await start(c)
    assert store.snapshot is before
    assert not transport.mock_calls and not adapter.mock_calls


async def test_safe_progress_tracks_saved_reconnect_across_restart_and_promotion(reconnect, hass, freezer):
    c, _store, transport, adapter = reconnect
    assert c.reconnect_in_progress is False
    await start(c)
    c, _store = await restarted(hass, transport, adapter)
    assert c.reconnect_in_progress is True
    freezer.tick(5)
    assert (await c.async_poll_pairing()).state == "candidate_saved"
    c, _store = await restarted(hass, transport, adapter)
    assert c.reconnect_in_progress is True
    assert (await c.async_reconcile()).state == "connected"
    assert c.reconnect_in_progress is False
    transport.exchange.assert_awaited_once()
