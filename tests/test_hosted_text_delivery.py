"""A source is durably accepted before its provider ACK is attempted."""
from dataclasses import replace
from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import.hosted_ack_dispatch import AckDispatchBlocked
from custom_components.daylight_calendar_import import hosted_text_delivery
from custom_components.daylight_calendar_import.hosted_text_delivery import async_accept_and_ack_text
from custom_components.daylight_calendar_import.hosted_transport import HostedTransportError
from tests.test_hosted_ack_dispatch import SERVICE, Transport, checkpoint
from tests.test_hosted_text_acceptance import page
from tests.test_storage import FakeStoreBackend, draft, make_store


async def deliver(store, creds, transport, delivery, *, parse=None, binding=SERVICE, claim=None):
    async def parse_default(_source):
        return SimpleNamespace(events=[draft()], event_assumptions=(), warnings=[]), None

    def route(source):
        return SimpleNamespace(parser_source=source, calendar_entity="calendar.test",
                               requires_confirmation=False, warnings=[])

    return await async_accept_and_ack_text(
        store=store, credentials=creds, claim_checkpoint=claim or creds.snapshot,
        service_binding=binding, transport=transport, local_config_entry_id="entry",
        delivery=delivery, route_source=route, parse_source=parse or parse_default,
    )


async def test_save_precedes_ack_and_confirm_survives_restart(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    credentials = SimpleNamespace(snapshot=checkpoint())
    delivery = page()["deliveries"][0]
    transport = Transport(on_call=lambda: (
        len(backend.saved) >= 2 and backend.saved[-1]["hosted_delivery_journal"][0]["ack_status"] == "pending"
    ) or pytest.fail("ACK ran before durable attempt"))
    result = await deliver(store, credentials, transport, delivery)
    assert result.pending is not None
    assert len(transport.calls) == 1
    restarted = make_store(monkeypatch, FakeStoreBackend(load_result=backend.saved[-1]))
    await restarted.async_load()
    assert restarted.hosted_ack_journal()[0].ack_status == "confirmed"


async def test_failed_parse_never_sends_ack(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    transport = Transport()

    async def failed(_source):
        raise RuntimeError("parse failed")

    with pytest.raises(RuntimeError):
        await deliver(store, SimpleNamespace(snapshot=checkpoint()), transport,
                      page()["deliveries"][0], parse=failed)
    assert not transport.calls and not store.hosted_ack_journal()


async def test_lost_ack_response_leaves_durable_obligation(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    transport = Transport(error=HostedTransportError("unavailable", retryable=True))
    with pytest.raises(HostedTransportError):
        await deliver(store, SimpleNamespace(snapshot=checkpoint()), transport,
                      page()["deliveries"][0])
    restarted = make_store(monkeypatch, FakeStoreBackend(load_result=backend.saved[-1]))
    await restarted.async_load()
    entry = restarted.hosted_ack_journal()[0]
    assert entry.ack_status == "pending" and entry.attempt_count == 1


async def test_different_claim_checkpoint_is_rejected_before_parse(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    credentials = SimpleNamespace(snapshot=checkpoint())
    transport = Transport()
    with pytest.raises(AckDispatchBlocked):
        await deliver(store, credentials, transport, page()["deliveries"][0],
                      claim=replace(credentials.snapshot, revision=1))
    assert not transport.calls and not store.hosted_ack_journal()


async def test_missing_saved_journal_never_sends_ack(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    transport = Transport()

    async def falsely_accepted(**_kwargs):
        return object()

    monkeypatch.setattr(hosted_text_delivery, "async_accept_hosted_text", falsely_accepted)
    with pytest.raises(AckDispatchBlocked):
        await deliver(store, SimpleNamespace(snapshot=checkpoint()), transport,
                      page()["deliveries"][0])
    assert not transport.calls
