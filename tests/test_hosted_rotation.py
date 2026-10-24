"""Durable rotation never replays uncertain issuance or loses the old token."""

import asyncio
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.storage import Store

from custom_components.daylight_calendar_import.hosted_credentials import (
    CredentialCheckpoint,
    HostedCredentialStore,
    _decode,
    _ImmediatePrivateStore,
)
from custom_components.daylight_calendar_import.hosted_lifecycle import (
    HostedLifecycleController,
)
from custom_components.daylight_calendar_import.hosted_transport import (
    HostedServiceBinding,
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


@pytest.fixture
async def rotation(hass, tmp_path, monkeypatch, freezer):
    freezer.move_to("2026-10-24T00:00:00Z")
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_load", _REAL_LOAD)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_write_data", _REAL_WRITE)
    store = HostedCredentialStore(hass, "rotation")
    blank = await store.async_load()
    await store.async_checkpoint(blank, replace(blank, service_binding=HostedServiceBinding("https://api.example.test", "https://account.example.test"), processing_consent=True, current=CURRENT))
    transport = AsyncMock()
    transport.service_binding = HostedServiceBinding("https://api.example.test", "https://account.example.test")
    transport.installation_status.return_value = status()
    transport.rotate.return_value = {
        "installation_id": INSTALLATION,
        "installation_token": CANDIDATE.token,
        "expires_at": EXPIRES,
        "confirmation_expires_at": CANDIDATE.confirmation_expires_at,
    }
    return HostedLifecycleController(store, transport), store, transport


async def restart(hass):
    store = HostedCredentialStore(hass, "rotation")
    await store.async_load()
    return store


async def test_saved_intent_precedes_issuance_candidate_precedes_confirmation(rotation, hass):
    c, _store, transport = rotation
    async def issue(token):
        disk = await restart(hass)
        assert token == CURRENT.token
        assert disk.snapshot.rotation_not_before == "2026-10-24T00:10:45+00:00"
        assert disk.snapshot.current == CURRENT and disk.snapshot.candidate is None
        return transport.rotate.return_value
    transport.rotate.side_effect = issue
    assert (await c.async_rotate()).state == "candidate_saved"
    transport.confirm.assert_not_called()
    transport.installation_status.assert_awaited_once_with(CURRENT.token)
    saved = (await restart(hass)).snapshot
    assert saved.current == CURRENT and saved.candidate == CANDIDATE
    assert (await c.async_rotate()).state == "recovery_required"
    transport.rotate.assert_awaited_once()
    assert (await c.async_reconcile()).state == "connected"
    final = (await restart(hass)).snapshot
    assert final.current.token == CANDIDATE.token
    assert final.candidate is None and final.rotation_not_before is None


@pytest.mark.parametrize("failure", [HostedTransportError("service_unavailable", retryable=True), asyncio.CancelledError()])
async def test_uncertain_issuance_survives_restart_and_blocks_retry(rotation, hass, failure):
    c, _store, transport = rotation
    transport.rotate.side_effect = failure
    with pytest.raises(type(failure)):
        await c.async_rotate()
    recovered = await restart(hass)
    assert recovered.snapshot.current == CURRENT and recovered.snapshot.candidate is None
    assert (await HostedLifecycleController(recovered, transport).async_rotate()).state == "rotation_wait"
    transport.rotate.assert_awaited_once()
    # Ordinary authorized work is retained despite the issuance ambiguity.
    assert (await HostedLifecycleController(recovered, transport).async_reconcile()).state == "connected"


async def test_expired_gate_allows_explicit_attempt_with_fresh_authority(rotation, freezer):
    c, _store, transport = rotation
    transport.rotate.side_effect = HostedTransportError("rotation_pending", status=409, retryable=True)
    with pytest.raises(HostedTransportError):
        await c.async_rotate()
    freezer.tick(645)
    transport.rotate.side_effect = None
    assert (await c.async_rotate()).state == "candidate_saved"
    assert transport.installation_status.await_count == 2
    assert transport.rotate.await_count == 2


@pytest.mark.parametrize("where", ["intent", "candidate"])
async def test_failed_storage_never_confirms_or_reports_success(rotation, monkeypatch, where):
    c, store, transport = rotation
    original = store.async_checkpoint
    count = 0
    async def checkpoint(*args):
        nonlocal count
        count += 1
        if count == (1 if where == "intent" else 2):
            raise RuntimeError("storage unavailable")
        return await original(*args)
    monkeypatch.setattr(store, "async_checkpoint", checkpoint)
    with pytest.raises(RuntimeError):
        await c.async_rotate()
    assert transport.rotate.await_count == (0 if where == "intent" else 1)
    transport.confirm.assert_not_called()
    assert store.snapshot.current == CURRENT


@pytest.mark.parametrize("kind", ["no_consent", "no_current", "candidate", "verifier", "disconnect"])
async def test_ineligible_state_has_no_network(rotation, kind):
    c, store, transport = rotation
    old = store.snapshot
    changes = {
        "no_consent": {"processing_consent": False, "current": None},
        "no_current": {"current": None},
        "candidate": {"candidate": CANDIDATE},
        "verifier": {"pairing_verifier": "v" * 43},
        "disconnect": {"local_disconnect": "unconfirmed"},
    }[kind]
    await store.async_checkpoint(old, replace(old, **changes))
    result = await c.async_rotate()
    assert result.state in ("disconnected", "repair_required", "recovery_required", "remote_disconnect_unconfirmed")
    assert not transport.mock_calls


@pytest.mark.parametrize("code,http_status", [("unauthenticated", 401), ("forbidden", 403), ("service_unavailable", 503)])
async def test_authority_failure_never_issues(rotation, code, http_status):
    c, store, transport = rotation
    original = store.snapshot
    transport.installation_status.side_effect = HostedTransportError(code, status=http_status)
    if http_status == 503:
        with pytest.raises(HostedTransportError):
            await c.async_rotate()
    else:
        assert (await c.async_rotate()).state == "repair_required"
    assert store.snapshot is original
    transport.rotate.assert_not_called()


async def test_overlap_keeps_deadline_but_cannot_rotate(rotation):
    c, store, transport = rotation
    transport.installation_status.return_value = status(credential_status="overlap", credential_expires_at="2026-10-24T00:09:00Z")
    result = await c.async_rotate()
    assert result.state == "repair_required" and result.credential_status == "overlap"
    assert result.credential_expires_at == "2026-10-24T00:09:00+00:00"
    transport.rotate.assert_not_called()
    assert store.snapshot.rotation_not_before is None


async def test_wrong_rotation_namespace_never_staged_or_confirmed(rotation):
    c, store, transport = rotation
    transport.rotate.return_value["installation_id"] += "other"
    with pytest.raises(HostedTransportError, match="^identity_mismatch$"):
        await c.async_rotate()
    assert store.snapshot.current == CURRENT and store.snapshot.candidate is None
    assert store.snapshot.rotation_not_before is not None
    transport.confirm.assert_not_called()


async def test_slow_checkpoint_never_sends_with_expired_safety_gate(rotation, freezer, monkeypatch):
    c, store, transport = rotation
    original = store.async_checkpoint
    async def slow(*args):
        result = await original(*args)
        freezer.tick(31)
        return result
    monkeypatch.setattr(store, "async_checkpoint", slow)
    assert (await c.async_rotate()).state == "rotation_wait"
    transport.rotate.assert_not_called()


async def test_concurrent_requests_issue_at_most_once(rotation):
    c, _store, transport = rotation
    results = await asyncio.gather(c.async_rotate(), c.async_rotate())
    assert {r.state for r in results} == {"candidate_saved", "recovery_required"}
    transport.rotate.assert_awaited_once()


def test_rotation_checkpoint_validation_and_backward_compatibility():
    for changes in ({"rotation_not_before": EXPIRES}, {"processing_consent": True, "rotation_not_before": EXPIRES}):
        with pytest.raises(ValueError, match="rotation intent requires"):
            CredentialCheckpoint(**changes)
    data = asdict(CredentialCheckpoint())
    del data["rotation_not_before"]
    assert _decode(data) == CredentialCheckpoint()
    del data["local_disconnect"]
    assert _decode(data) == CredentialCheckpoint()
    with pytest.raises(ValueError):
        CredentialCheckpoint(local_disconnect="confirmed", rotation_not_before=EXPIRES)


@pytest.mark.parametrize("field,value", [
    ("expires_at", "2027-10-24T00:00:00Z"),
    ("confirmation_expires_at", "2026-10-24T00:10:31Z"),
])
async def test_excessive_server_deadlines_retain_uncertainty(rotation, field, value):
    c, store, transport = rotation
    transport.rotate.return_value[field] = value
    with pytest.raises(HostedTransportError, match="^invalid_response$"):
        await c.async_rotate()
    assert store.snapshot.candidate is None
    assert store.snapshot.current == CURRENT
    assert (await c.async_rotate()).state == "rotation_wait"
    transport.confirm.assert_not_called()


@pytest.mark.parametrize("field,limit", [
    ("expires_at", "2027-01-22T00:00:30+00:00"),
    ("confirmation_expires_at", "2026-10-24T00:10:30+00:00"),
])
@pytest.mark.parametrize("extra_microseconds", [0, 1, 1_000_000])
async def test_server_deadline_boundaries_are_inclusive(
    rotation, field, limit, extra_microseconds,
):
    from datetime import datetime, timedelta

    c, store, transport = rotation
    deadline = datetime.fromisoformat(limit) + timedelta(microseconds=extra_microseconds)
    transport.rotate.return_value[field] = deadline.isoformat()
    if extra_microseconds:
        with pytest.raises(HostedTransportError, match="^invalid_response$"):
            await c.async_rotate()
        assert store.snapshot.candidate is None
        assert store.snapshot.current == CURRENT
        assert (await c.async_rotate()).state == "rotation_wait"
    else:
        assert (await c.async_rotate()).state == "candidate_saved"
        assert getattr(store.snapshot.candidate, field) == deadline.isoformat()
    transport.confirm.assert_not_called()


async def test_checkpoint_at_exact_safety_margin_can_issue(rotation, freezer, monkeypatch):
    c, store, transport = rotation
    original = store.async_checkpoint

    async def delayed(*args):
        result = await original(*args)
        freezer.tick(30)
        return result

    monkeypatch.setattr(store, "async_checkpoint", delayed)
    assert (await c.async_rotate()).state == "candidate_saved"
    transport.rotate.assert_awaited_once_with(CURRENT.token)
    assert store.snapshot.current == CURRENT
    assert store.snapshot.candidate == CANDIDATE
    transport.confirm.assert_not_called()


async def test_confirmed_disconnect_cannot_resume_rotation(rotation):
    c, store, transport = rotation
    old = store.snapshot
    await store.async_checkpoint(old, replace(
        old, processing_consent=False, current=None, local_disconnect="confirmed",
    ))
    assert (await c.async_rotate()).state == "disconnected"
    assert not transport.mock_calls
