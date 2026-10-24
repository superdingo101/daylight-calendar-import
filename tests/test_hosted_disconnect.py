"""Local disconnect state is durable and distinct from remote confirmation."""

import asyncio
from dataclasses import asdict, replace
from unittest.mock import AsyncMock

import pytest

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
    _REAL_LOAD,
    _REAL_WRITE,
    CANDIDATE,
    CURRENT,
    status,
)


class MemoryStore:
    def __init__(self, current=CURRENT):
        self.snapshot = CredentialCheckpoint(service_binding=HostedServiceBinding("https://api.example.test", "https://account.example.test"), processing_consent=True, current=current)
        self.writes = []
        self.fail = False

    async def async_checkpoint(self, expected, proposed):
        assert expected is self.snapshot
        if self.fail:
            raise RuntimeError("checkpoint unavailable")
        self.writes.append(proposed)
        self.snapshot = replace(proposed, revision=expected.revision + 1)
        return self.snapshot


def setup(*, current=CURRENT):
    store = MemoryStore(current)
    transport = AsyncMock()
    transport.service_binding = HostedServiceBinding("https://api.example.test", "https://account.example.test")
    transport.installation_status.return_value = status()
    transport.disconnect.return_value = {
        "installation_id": CURRENT.installation_id,
        "status": "disconnected",
    }
    return HostedLifecycleController(store, transport), store, transport


async def test_stop_is_committed_before_network_then_success_removes_private_material():
    c, store, transport = setup()
    store.snapshot = replace(
        store.snapshot, candidate=CANDIDATE, pairing_verifier="v" * 43
    )

    async def remote_status(token):
        assert store.snapshot.local_disconnect == "unconfirmed"
        assert (
            store.snapshot.current == CURRENT and store.snapshot.candidate == CANDIDATE
        )
        return status()

    transport.installation_status.side_effect = remote_status
    assert (await c.async_disconnect()).state == "disconnected"
    final = store.snapshot
    assert final.local_disconnect == "confirmed" and not final.processing_consent
    assert final.current is final.candidate is final.pairing_verifier is None
    transport.disconnect.assert_awaited_once_with(CURRENT.token)
    assert (await c.async_reconcile()).state == "disconnected"
    assert (await c.async_disconnect()).state == "disconnected"
    transport.disconnect.assert_awaited_once()
    assert len(store.writes) == 2


@pytest.mark.parametrize("at", ["status", "disconnect"])
@pytest.mark.parametrize(
    "http_status,code", [(401, "unauthenticated"), (403, "forbidden")]
)
async def test_unauthorized_never_proves_remote_disconnect(at, http_status, code):
    c, store, transport = setup()
    getattr(
        transport, "installation_status" if at == "status" else at
    ).side_effect = HostedTransportError(code, status=http_status)
    assert (await c.async_disconnect()).state == "remote_disconnect_unconfirmed"
    assert (
        store.snapshot.current == CURRENT
        and store.snapshot.local_disconnect == "unconfirmed"
    )
    count = transport.disconnect.await_count
    assert (await c.async_disconnect()).state == "remote_disconnect_unconfirmed"
    assert (await c.async_reconcile()).state == "remote_disconnect_unconfirmed"
    assert transport.disconnect.await_count == count


@pytest.mark.parametrize("at", ["status", "disconnect"])
async def test_network_loss_pauses_locally_and_never_replays(at):
    c, store, transport = setup()
    getattr(
        transport, "installation_status" if at == "status" else at
    ).side_effect = HostedTransportError("service_unavailable", retryable=True)
    with pytest.raises(HostedTransportError):
        await c.async_disconnect()
    assert store.snapshot.local_disconnect == "unconfirmed"
    previous_calls = transport.mock_calls.copy()
    assert (await c.async_disconnect()).state == "remote_disconnect_unconfirmed"
    assert (await c.async_reconcile()).state == "remote_disconnect_unconfirmed"
    assert transport.mock_calls == previous_calls


async def test_missing_bearer_requires_owner_reconciliation_without_network():
    c, _store, transport = setup(current=None)
    assert (await c.async_disconnect()).state == "remote_disconnect_unconfirmed"
    assert not transport.mock_calls


async def test_overlap_credential_cannot_revoke_current():
    c, _store, transport = setup()
    transport.installation_status.return_value = status(credential_status="overlap")
    assert (await c.async_disconnect()).state == "remote_disconnect_unconfirmed"
    transport.disconnect.assert_not_called()


@pytest.mark.parametrize("at", ["status", "disconnect"])
async def test_identity_change_preserves_local_stop_and_previous_binding(at):
    c, store, transport = setup()
    if at == "status":
        transport.installation_status.return_value = status(
            account_id=CURRENT.account_id + "other"
        )
    else:
        transport.disconnect.return_value["installation_id"] += "other"
    with pytest.raises(HostedTransportError, match="identity_mismatch"):
        await c.async_disconnect()
    assert (
        store.snapshot.current == CURRENT
        and store.snapshot.local_disconnect == "unconfirmed"
    )


async def test_failed_stop_checkpoint_sends_nothing():
    c, store, transport = setup()
    store.fail = True
    with pytest.raises(RuntimeError):
        await c.async_disconnect()
    assert not transport.mock_calls and store.snapshot.local_disconnect == "none"


async def test_failed_confirmation_checkpoint_keeps_remote_state_uncertain():
    c, store, transport = setup()

    async def disconnect(token):
        store.fail = True
        return {"installation_id": CURRENT.installation_id}

    transport.disconnect.side_effect = disconnect
    with pytest.raises(RuntimeError):
        await c.async_disconnect()
    assert (
        store.snapshot.local_disconnect == "unconfirmed"
        and store.snapshot.current == CURRENT
    )


async def test_cancelled_request_preserves_stop():
    c, store, transport = setup()
    transport.disconnect.side_effect = asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError):
        await c.async_disconnect()
    assert (await c.async_reconcile()).state == "remote_disconnect_unconfirmed"
    assert store.snapshot.current == CURRENT


async def test_real_private_disk_restart_does_not_resume_or_replay(
    hass, tmp_path, monkeypatch
):
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_load", _REAL_LOAD)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_write_data", _REAL_WRITE)
    store = HostedCredentialStore(hass, "disconnect")
    blank = await store.async_load()
    await store.async_checkpoint(
        blank, replace(blank, service_binding=HostedServiceBinding("https://api.example.test", "https://account.example.test"), processing_consent=True, current=CURRENT)
    )
    transport = AsyncMock()
    transport.service_binding = HostedServiceBinding("https://api.example.test", "https://account.example.test")
    transport.installation_status.return_value = status()
    transport.disconnect.side_effect = HostedTransportError(
        "service_unavailable", retryable=True
    )
    with pytest.raises(HostedTransportError):
        await HostedLifecycleController(store, transport).async_disconnect()
    restarted = HostedCredentialStore(hass, "disconnect")
    assert (await restarted.async_load()).local_disconnect == "unconfirmed"
    controller = HostedLifecycleController(restarted, transport)
    before = transport.mock_calls.copy()
    assert (await controller.async_reconcile()).state == "remote_disconnect_unconfirmed"
    assert (
        await controller.async_disconnect()
    ).state == "remote_disconnect_unconfirmed"
    assert transport.mock_calls == before


def test_legacy_checkpoints_upgrade_without_changing_identity_or_intent():
    current = CredentialCheckpoint(
        processing_consent=True, current=CURRENT, candidate=CANDIDATE
    )
    old = asdict(current)
    del old["local_disconnect"]
    assert _decode(old) == current
    assert _decode(asdict(current)) == current


@pytest.mark.parametrize("value", ["invalid", None, True, 1, []])
def test_invalid_disconnect_state_rejected(value):
    with pytest.raises(ValueError, match="invalid local disconnect state"):
        CredentialCheckpoint(local_disconnect=value)


@pytest.mark.parametrize(
    "changes",
    [
        {"processing_consent": True},
        {"pairing_verifier": "v" * 43},
        {"current": CURRENT},
        {"candidate": CANDIDATE},
    ],
)
def test_confirmed_disconnect_cannot_retain_consent_or_secrets(changes):
    with pytest.raises(
        ValueError, match="confirmed disconnect retains credential material"
    ):
        CredentialCheckpoint(local_disconnect="confirmed", **changes)
