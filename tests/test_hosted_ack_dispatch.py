"""The one-shot ACK flow only uses durably accepted, bound evidence."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import.hosted_ack_dispatch import (
    AckDispatchBlocked, async_dispatch_ack,
)
from custom_components.daylight_calendar_import.hosted_credentials import (
    BoundCredential, CandidateCredential, CredentialCheckpoint, ReconnectIntent,
)
from custom_components.daylight_calendar_import import hosted_ack_dispatch, storage
from custom_components.daylight_calendar_import.hosted_transport import (
    HostedServiceBinding, HostedTransportError,
)
from tests.test_hosted_delivery_journal import evidence
from tests.test_storage import FakeStoreBackend, make_store

SERVICE = HostedServiceBinding("https://api.example.test", "https://account.example.test")
ACCOUNT = "a" * 20
INSTALLATION = "i" * 20
TOKEN = "t" * 43


class Transport:
    service_binding = SERVICE

    def __init__(self, *, error=None, on_call=None):
        self.calls = []
        self.error = error
        self.on_call = on_call

    async def acknowledge_source(self, **kwargs):
        self.calls.append(kwargs)
        if self.on_call:
            self.on_call()
        if self.error:
            raise self.error
        return {"status": "acknowledged"}


def checkpoint(**kw):
    cred = BoundCredential(ACCOUNT, INSTALLATION, TOKEN,
                           (datetime.now(UTC) + timedelta(days=2)).isoformat())
    return CredentialCheckpoint(service_binding=SERVICE, processing_consent=True,
                                current=cred, **kw)


async def setup(monkeypatch, *, item=None):
    store = make_store(monkeypatch, FakeStoreBackend())
    item = item or replace(evidence(), account_id=ACCOUNT, installation_id=INSTALLATION)
    await store.async_add(source_text="text", events=[], source_id=item.source_id,
                          hosted_evidence=item)
    return store, store.hosted_ack_journal()[0]


async def call(store, entry, creds, transport, local="entry"):
    return await async_dispatch_ack(store=store, credentials=creds, transport=transport,
                                    entry=entry, local_config_entry_id=local)


async def test_confirm_and_reject_stale_retry(monkeypatch):
    store, entry = await setup(monkeypatch)
    transport = Transport()
    result = await call(store, entry, SimpleNamespace(snapshot=checkpoint()), transport)
    assert result.ack_status == "confirmed" and result.attempt_count == 1
    assert transport.calls == [{"token": TOKEN, "delivery_id": "delivery", "lease_token": "secret-token"}]
    with pytest.raises(ValueError, match="stale"):
        await call(store, entry, SimpleNamespace(snapshot=checkpoint()), transport)
    assert len(transport.calls) == 1


@pytest.mark.parametrize("kind", ["none", "disconnect", "namespace", "legacy", "local", "binding", "expired_credential", "candidate", "rotation", "reconnect"])
async def test_unbound_authority_does_not_ack(monkeypatch, kind):
    store, entry = await setup(monkeypatch)
    cp = checkpoint()
    if kind == "none":
        cp = CredentialCheckpoint()
    elif kind == "disconnect":
        cp = replace(cp, local_disconnect="unconfirmed")
    elif kind == "namespace":
        cp = replace(cp, current=replace(cp.current, account_id="b" * 20))
    elif kind == "legacy":
        entry = replace(entry, evidence=replace(entry.evidence, account_id=None, installation_id=None))
    elif kind == "binding":
        cp = replace(cp, service_binding=HostedServiceBinding("https://other.example.test", "https://account.example.test"))
    elif kind == "expired_credential":
        cp = replace(cp, current=replace(cp.current, expires_at=(datetime.now(UTC) - timedelta(days=1)).isoformat()))
    elif kind == "candidate":
        deadline = (datetime.now(UTC) + timedelta(days=1)).isoformat()
        cp = replace(cp, candidate=CandidateCredential(INSTALLATION, TOKEN, deadline, deadline))
    elif kind == "rotation":
        cp = replace(cp, rotation_not_before=(datetime.now(UTC) + timedelta(hours=1)).isoformat())
    elif kind == "reconnect":
        cp = replace(cp, pairing_verifier="v" * 43,
                     reconnect=ReconnectIntent(ACCOUNT, INSTALLATION, "none", "pending-v2-local-only"))
    transport = Transport()
    with pytest.raises(AckDispatchBlocked, match="^hosted ACK authority unavailable$"):
        await call(store, entry, SimpleNamespace(snapshot=cp), transport, "other" if kind == "local" else "entry")
    assert not transport.calls
    assert store.hosted_ack_journal()[0].attempt_count == 0


async def test_lost_response_keeps_attempt_pending(monkeypatch):
    store, entry = await setup(monkeypatch)
    transport = Transport(error=HostedTransportError("unavailable", retryable=True))
    with pytest.raises(HostedTransportError):
        await call(store, entry, SimpleNamespace(snapshot=checkpoint()), transport)
    current = store.hosted_ack_journal()[0]
    assert current.ack_status == "pending" and current.attempt_count == 1
    result = await call(store, current, SimpleNamespace(snapshot=checkpoint()), Transport())
    assert result.ack_status == "confirmed" and result.attempt_count == 2


async def test_credential_change_during_request_does_not_confirm(monkeypatch):
    store, entry = await setup(monkeypatch)
    cp = checkpoint()
    creds = SimpleNamespace(snapshot=cp)
    transport = Transport(on_call=lambda: setattr(creds, "snapshot", replace(cp, revision=1)))
    with pytest.raises(AckDispatchBlocked, match="^hosted ACK authority changed$"):
        await call(store, entry, creds, transport)
    assert store.hosted_ack_journal()[0].ack_status == "pending"


async def test_expired_source_is_terminal_without_ack(monkeypatch):
    store, entry = await setup(monkeypatch)
    # Test the store's real expiry branch using an injected clock via immutable evidence
    # only after durable acceptance has occurred.
    expired = replace(entry, evidence=replace(entry.evidence,
        source_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()))
    store._hosted_delivery_journal = (expired,)
    transport = Transport()
    result = await call(store, expired, SimpleNamespace(snapshot=CredentialCheckpoint()), transport)
    assert result.ack_status == "expired" and not transport.calls


async def test_checkpoint_changed_before_send_leaves_attempt_pending(monkeypatch):
    store, entry = await setup(monkeypatch)
    cp = checkpoint()

    class ChangingCredentials:
        accesses = 0

        @property
        def snapshot(self):
            self.accesses += 1
            return cp if self.accesses == 1 else replace(cp, revision=1)

    transport = Transport()
    with pytest.raises(AckDispatchBlocked, match="^hosted ACK authority changed$"):
        await call(store, entry, ChangingCredentials(), transport)
    assert not transport.calls
    assert store.hosted_ack_journal()[0].attempt_count == 1


@pytest.mark.parametrize("which", ["source", "credential"])
async def test_expiry_during_attempt_save_prevents_ack(monkeypatch, which):
    now = datetime.now(UTC)
    deadline = now + timedelta(seconds=1)
    item = replace(evidence(), account_id=ACCOUNT, installation_id=INSTALLATION,
                   source_expires_at=(deadline if which == "source" else now + timedelta(days=1)).isoformat())
    store, entry = await setup(monkeypatch, item=item)
    cp = checkpoint()
    if which == "credential":
        cp = replace(cp, current=replace(cp.current, expires_at=deadline.isoformat()))

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls.current

    Clock.current = now
    monkeypatch.setattr(hosted_ack_dispatch, "datetime", Clock)
    monkeypatch.setattr(storage, "datetime", Clock)
    reconcile = store.async_reconcile_hosted_ack

    async def slow_save(**kwargs):
        result = await reconcile(**kwargs)
        if kwargs["action"] == "attempt":
            Clock.current = deadline
        return result

    monkeypatch.setattr(store, "async_reconcile_hosted_ack", slow_save)
    transport = Transport()
    if which == "source":
        result = await call(store, entry, SimpleNamespace(snapshot=cp), transport)
        assert result.ack_status == "expired" and result.attempt_count == 1
    else:
        with pytest.raises(AckDispatchBlocked, match="^hosted ACK authority unavailable$"):
            await call(store, entry, SimpleNamespace(snapshot=cp), transport)
        assert store.hosted_ack_journal()[0].ack_status == "pending"
    assert not transport.calls
    assert store.hosted_ack_journal()[0].attempt_count == 1


async def test_failed_attempt_save_does_not_send(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = replace(evidence(), account_id=ACCOUNT, installation_id=INSTALLATION)
    await store.async_add(source_text="text", events=[], source_id=item.source_id, hosted_evidence=item)
    entry = store.hosted_ack_journal()[0]
    backend.save_error = OSError("private")
    transport = Transport()
    with pytest.raises(OSError):
        await call(store, entry, SimpleNamespace(snapshot=checkpoint()), transport)
    assert not transport.calls
