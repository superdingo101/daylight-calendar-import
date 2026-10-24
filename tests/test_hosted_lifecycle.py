"""P02 recovery: safe binding, exact request order and retained private bundles."""

from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.storage import Store

from custom_components.daylight_calendar_import.hosted_credentials import (
    BoundCredential,
    CandidateCredential,
    CredentialCheckpoint,
)
from custom_components.daylight_calendar_import.hosted_lifecycle import (
    HostedLifecycleController,
)
from custom_components.daylight_calendar_import.hosted_transport import (
    HostedTransportError,
)

_REAL_LOAD = Store._async_load
_REAL_WRITE = Store._async_write_data

ACCOUNT = "account_1234567890"
INSTALLATION = "installation_1234567890"
EXPIRES = "2026-12-01T00:00:00+00:00"
CURRENT = BoundCredential(ACCOUNT, INSTALLATION, "old" + "x" * 43, EXPIRES)
CANDIDATE = CandidateCredential(
    INSTALLATION, "new" + "x" * 43, EXPIRES, "2026-10-24T00:10:00Z"
)


def status(**changes):
    return {
        "account_id": ACCOUNT,
        "installation_id": INSTALLATION,
        "credential_expires_at": EXPIRES,
        "credential_status": "current",
        **changes,
    }


def controller(*, current=CURRENT, candidate=CANDIDATE, consent=True):
    store = AsyncMock()
    store.snapshot = CredentialCheckpoint(
        processing_consent=consent, current=current, candidate=candidate
    )
    transport = AsyncMock()
    transport.installation_status.return_value = status()
    transport.confirm.return_value = {
        "installation_id": INSTALLATION,
        "expires_at": EXPIRES,
    }
    return HostedLifecycleController(store, transport), store, transport


async def test_active_candidate_promotes_only_after_bound_status():
    c, store, transport = controller()
    result = await c.async_reconcile()
    assert (result.state, result.credential_status) == ("connected", "current")
    proposed = store.async_checkpoint.call_args.args[1]
    assert proposed.current.token == CANDIDATE.token
    assert proposed.current.namespace == CURRENT.namespace
    assert proposed.candidate is None and proposed.pairing_verifier is None
    transport.confirm.assert_not_called()


@pytest.mark.parametrize("auth_status", [401, 403])
async def test_staged_candidate_confirmed_once_then_status_binds(auth_status):
    c, store, transport = controller(current=None)
    transport.installation_status.side_effect = [
        HostedTransportError(
            "unauthenticated" if auth_status == 401 else "forbidden", status=auth_status
        ),
        status(),
    ]
    assert (await c.async_reconcile()).state == "connected"
    transport.confirm.assert_awaited_once_with(CANDIDATE.token)
    assert transport.installation_status.await_count == 2
    store.async_checkpoint.assert_awaited_once()


@pytest.mark.parametrize("old_status", ["current", "overlap"])
async def test_failed_candidate_retains_verified_previous(old_status):
    c, store, transport = controller()
    transport.installation_status.side_effect = [
        HostedTransportError("unauthenticated", status=401),
        status(credential_status=old_status),
    ]
    transport.confirm.side_effect = HostedTransportError("forbidden", status=403)
    result = await c.async_reconcile()
    assert (result.state, result.credential_status) == ("previous_retained", old_status)
    store.async_checkpoint.assert_not_called()
    assert transport.installation_status.call_args.args == (CURRENT.token,)


@pytest.mark.parametrize(
    "changes",
    [
        {"installation_id": INSTALLATION + "other"},
        {"account_id": ACCOUNT + "other"},
        {"credential_expires_at": "2027-01-01T00:00:00Z"},
    ],
)
async def test_previous_identity_and_deadline_never_rebound(changes):
    c, store, transport = controller(candidate=None)
    transport.installation_status.return_value = status(**changes)
    with pytest.raises(HostedTransportError, match="identity_mismatch"):
        await c.async_reconcile()
    store.async_checkpoint.assert_not_called()


async def test_replacement_blocked_before_network_or_promotion():
    c, store, transport = controller(
        candidate=replace(CANDIDATE, installation_id=INSTALLATION + "other")
    )
    assert (await c.async_reconcile()).state == "reconciliation_required"
    assert not transport.mock_calls and not store.async_checkpoint.mock_calls


@pytest.mark.parametrize("where", ["status", "confirm", "after_confirm"])
async def test_uncertain_network_result_keeps_all_bundles(where):
    c, store, transport = controller()
    unavailable = HostedTransportError(
        "service_unavailable", status=503, retryable=True
    )
    unauthorized = HostedTransportError("unauthenticated", status=401)
    transport.installation_status.side_effect = (
        [unavailable] if where == "status" else [unauthorized, unavailable]
    )
    if where == "confirm":
        transport.confirm.side_effect = unavailable
    with pytest.raises(HostedTransportError, match="service_unavailable"):
        await c.async_reconcile()
    store.async_checkpoint.assert_not_called()
    assert transport.confirm.await_count <= 1


@pytest.mark.parametrize(
    "changes",
    [
        {"installation_id": INSTALLATION + "other"},
        {"expires_at": "2027-01-01T00:00:00Z"},
    ],
)
async def test_confirmation_identity_and_original_deadline(changes):
    c, store, transport = controller()
    transport.installation_status.side_effect = HostedTransportError(
        "unauthenticated", status=401
    )
    transport.confirm.return_value.update(changes)
    with pytest.raises(HostedTransportError, match="identity_mismatch"):
        await c.async_reconcile()
    store.async_checkpoint.assert_not_called()


async def test_candidate_household_mismatch_retains_previous():
    c, store, transport = controller()
    transport.installation_status.return_value = status(account_id=ACCOUNT + "other")
    with pytest.raises(HostedTransportError, match="identity_mismatch"):
        await c.async_reconcile()
    store.async_checkpoint.assert_not_called()


async def test_superseded_candidate_preserves_both_and_requires_repair():
    c, store, transport = controller()
    transport.installation_status.return_value = status(credential_status="overlap")
    result = await c.async_reconcile()
    assert (result.state, result.credential_status) == ("repair_required", "overlap")
    store.async_checkpoint.assert_not_called()


@pytest.mark.parametrize("current", [None, CURRENT])
async def test_neither_saved_credential_authorized_requires_repair(current):
    c, store, transport = controller(current=current)
    transport.installation_status.side_effect = HostedTransportError(
        "unauthenticated", status=401
    )
    transport.confirm.side_effect = HostedTransportError("forbidden", status=403)
    assert (await c.async_reconcile()).state == "repair_required"
    store.async_checkpoint.assert_not_called()


async def test_no_consent_has_no_network_side_effect():
    c, _store, transport = controller(current=None, candidate=None, consent=False)
    assert (await c.async_reconcile()).state == "disconnected"
    assert not transport.mock_calls


async def test_previous_only_verified_and_transient_failure_propagates():
    c, store, transport = controller(candidate=None)
    assert (await c.async_reconcile()).state == "connected"
    transport.installation_status.side_effect = HostedTransportError(
        "service_unavailable", retryable=True
    )
    with pytest.raises(HostedTransportError):
        await c.async_reconcile()
    store.async_checkpoint.assert_not_called()


async def test_storage_failure_never_publishes_success():
    c, store, transport = controller()
    store.async_checkpoint.side_effect = RuntimeError("unavailable")
    with pytest.raises(RuntimeError):
        await c.async_reconcile()
    transport.confirm.assert_not_called()


async def test_real_disk_restart_recovers_lost_confirmation(
    hass, tmp_path, monkeypatch
):
    from custom_components.daylight_calendar_import.hosted_credentials import (
        HostedCredentialStore,
        _ImmediatePrivateStore,
    )
    # conftest mocks HA storage globally; opt into the actual atomic reader/writer.

    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_load", _REAL_LOAD)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_write_data", _REAL_WRITE)
    original = HostedCredentialStore(hass, "recovery")
    blank = await original.async_load()
    active = await original.async_checkpoint(
        blank,
        replace(
            blank,
            processing_consent=True,
            current=CURRENT,
        ),
    )
    await original.async_checkpoint(active, replace(active, candidate=CANDIDATE))
    transport = AsyncMock()
    transport.installation_status.side_effect = HostedTransportError(
        "unauthenticated", status=401
    )
    transport.confirm.side_effect = HostedTransportError(
        "service_unavailable", retryable=True
    )
    with pytest.raises(HostedTransportError):
        await HostedLifecycleController(original, transport).async_reconcile()
    restarted = HostedCredentialStore(hass, "recovery")
    recovered = await restarted.async_load()
    assert recovered.current == CURRENT and recovered.candidate == CANDIDATE
    transport.installation_status.side_effect = None
    transport.installation_status.return_value = status()
    assert (
        await HostedLifecycleController(restarted, transport).async_reconcile()
    ).state == "connected"
    transport.confirm.assert_awaited_once()
    final = await HostedCredentialStore(hass, "recovery").async_load()
    assert final.current.token == CANDIDATE.token and final.candidate is None


@pytest.mark.parametrize("candidate", [None, CANDIDATE])
@pytest.mark.parametrize(
    "code,http_status", [("invalid_response", 401), ("unknown_code", 403)]
)
async def test_malformed_or_unknown_auth_failure_never_reconfirms_or_discards(
    candidate, code, http_status
):
    c, store, transport = controller(candidate=candidate)
    transport.installation_status.side_effect = HostedTransportError(
        code, status=http_status
    )
    with pytest.raises(HostedTransportError, match=code):
        await c.async_reconcile()
    transport.confirm.assert_not_called()
    store.async_checkpoint.assert_not_called()
