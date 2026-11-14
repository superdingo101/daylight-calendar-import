"""One claim is bound to its lifecycle and is never automatically retried."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import import hosted_text_batch as batch
from custom_components.daylight_calendar_import.hosted_ack_dispatch import AckDispatchBlocked
from custom_components.daylight_calendar_import.hosted_credentials import CredentialCheckpoint
from tests.test_hosted_ack_dispatch import SERVICE, TOKEN, checkpoint


class Claim:
    service_binding = SERVICE

    def __init__(self, deliveries=(), failure=None, callback=None):
        self.calls = []
        self.deliveries = deliveries
        self.failure = failure
        self.callback = callback

    async def async_claim(self, **kwargs):
        self.calls.append(kwargs)
        if self.callback:
            self.callback()
        if self.failure:
            raise self.failure
        return {"deliveries": self.deliveries}


async def call(creds, client, ack=None):
    return await batch.async_process_text_batch(
        store=object(), credentials=creds, claim_transport=client,
        ack_transport=ack or SimpleNamespace(service_binding=SERVICE),
        local_config_entry_id="entry", route_source=object(), parse_source=object(), limit=2,
    )


async def test_one_claim_processes_sequentially(monkeypatch):
    creds = SimpleNamespace(snapshot=checkpoint())
    client = Claim([{"delivery_id": "one"}, {"delivery_id": "two"}])
    seen = []

    async def handle(**kwargs):
        seen.append(kwargs["delivery"]["delivery_id"])
        assert kwargs["claim_checkpoint"] == creds.snapshot
        return len(seen)

    monkeypatch.setattr(batch, "async_handle_text_claim", handle)
    assert await call(creds, client) == [1, 2]
    assert seen == ["one", "two"]
    assert client.calls == [{"token": TOKEN, "limit": 2}]


async def test_claim_error_never_retries():
    client = Claim(failure=RuntimeError("failure"))
    with pytest.raises(RuntimeError):
        await call(SimpleNamespace(snapshot=checkpoint()), client)
    assert len(client.calls) == 1


async def test_changed_credential_after_claim_never_processes():
    creds = SimpleNamespace(snapshot=checkpoint())
    client = Claim([{}], callback=lambda: setattr(creds, "snapshot", CredentialCheckpoint()))
    with pytest.raises(AckDispatchBlocked, match="authority changed"):
        await call(creds, client)
    assert len(client.calls) == 1


async def test_changed_credential_between_deliveries_stops(monkeypatch):
    creds = SimpleNamespace(snapshot=checkpoint())
    client = Claim([{}, {}])
    calls = []

    async def handle(**_kwargs):
        calls.append(1)
        creds.snapshot = CredentialCheckpoint()

    monkeypatch.setattr(batch, "async_handle_text_claim", handle)
    with pytest.raises(AckDispatchBlocked, match="authority unavailable"):
        await call(creds, client)
    assert len(calls) == 1


async def test_processing_failure_never_claims_again_or_processes_remaining(monkeypatch):
    client = Claim([{}, {}])
    calls = []

    async def handle(**_kwargs):
        calls.append(1)
        raise ValueError("processing failed")

    monkeypatch.setattr(batch, "async_handle_text_claim", handle)
    with pytest.raises(ValueError, match="processing failed"):
        await call(SimpleNamespace(snapshot=checkpoint()), client)
    assert len(calls) == len(client.calls) == 1


@pytest.mark.parametrize("kind", ["missing", "binding", "claim_service", "ack_service",
                                 "expired", "disconnect", "rotation", "candidate", "reconnect"])
async def test_unavailable_authority_never_claims(kind):
    cp = checkpoint()
    if kind == "missing":
        cp = CredentialCheckpoint()
    elif kind == "binding":
        cp = replace(cp, service_binding=None)
    elif kind == "expired":
        cp = replace(cp, current=replace(cp.current, expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat()))
    elif kind == "disconnect":
        cp = replace(cp, local_disconnect="unconfirmed")
    elif kind == "rotation":
        cp = replace(cp, rotation_not_before=(datetime.now(UTC) + timedelta(seconds=1)).isoformat())
    elif kind in {"candidate", "reconnect"}:
        # Stand-ins isolate this boundary without creating pairing workflows.
        cp = SimpleNamespace(**{name: getattr(cp, name) for name in (
            "current", "service_binding", "processing_consent", "local_disconnect",
            "rotation_not_before", "candidate", "reconnect",
        )})
        setattr(cp, kind, object())
    client = Claim()
    ack = SimpleNamespace(service_binding=SERVICE)
    if kind == "claim_service":
        client.service_binding = object()
    if kind == "ack_service":
        ack.service_binding = object()
    with pytest.raises(AckDispatchBlocked, match="authority unavailable"):
        await call(SimpleNamespace(snapshot=cp), client, ack)
    assert client.calls == []
