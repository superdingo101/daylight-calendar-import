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
    with pytest.raises(AckDispatchBlocked, match=r"^hosted ACK authority unavailable$"):
        await deliver(store, credentials, transport, page()["deliveries"][0],
                      claim=replace(credentials.snapshot, revision=1))
    assert not transport.calls and not store.hosted_ack_journal()


async def test_missing_saved_journal_never_sends_ack(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    transport = Transport()

    async def falsely_accepted(**_kwargs):
        return object()

    monkeypatch.setattr(hosted_text_delivery, "async_accept_hosted_text", falsely_accepted)
    with pytest.raises(AckDispatchBlocked, match=r"^hosted ACK authority unavailable$"):
        await deliver(store, SimpleNamespace(snapshot=checkpoint()), transport,
                      page()["deliveries"][0])
    assert not transport.calls


async def test_transport_binding_mismatch_is_rejected_before_parse(monkeypatch):
    """A transport bound to a different service must not parse or acknowledge."""
    from custom_components.daylight_calendar_import.hosted_transport import HostedServiceBinding

    store = make_store(monkeypatch, FakeStoreBackend())
    transport = Transport()

    async def must_not_parse(_source):
        pytest.fail("mismatched transport reached the parser")

    with pytest.raises(AckDispatchBlocked, match=r"^hosted ACK authority unavailable$"):
        await deliver(
            store, SimpleNamespace(snapshot=checkpoint()), transport,
            page()["deliveries"][0], parse=must_not_parse,
            binding=HostedServiceBinding("https://other.example.test", "https://account.example.test"),
        )
    assert not transport.calls and not store.hosted_ack_journal()


@pytest.mark.parametrize(
    "incorrect_field",
    ["local_config_entry_id", "delivery_id", "public_source_id", "lease_token", "ack_status"],
)
async def test_saved_journal_cannot_select_other_delivery(monkeypatch, incorrect_field):
    """Every evidence field AND the pending state must match before an ACK.

    Simulate acceptance having returned but the journal being unexpected; only
    the existing durable journal entry may authorize an ACK.
    """
    delivery = page()["deliveries"][0]
    evidence = {
        "local_config_entry_id": "entry",
        "delivery_id": delivery["delivery_id"],
        "public_source_id": delivery["source_id"],
        "lease_token": delivery["lease_token"],
    }
    status = "pending"
    if incorrect_field == "ack_status":
        status = "confirmed"
    else:
        evidence[incorrect_field] = "different"
    entry = SimpleNamespace(evidence=SimpleNamespace(**evidence), ack_status=status)
    store = SimpleNamespace(hosted_ack_journal=lambda: (entry,))
    credentials = SimpleNamespace(snapshot=checkpoint())
    transport = Transport()

    async def accepted(**_kwargs):
        return object()

    async def must_not_dispatch(**_kwargs):
        pytest.fail("ACK selected an unrelated or non-pending journal entry")

    monkeypatch.setattr(hosted_text_delivery, "async_accept_hosted_text", accepted)
    monkeypatch.setattr(hosted_text_delivery, "async_dispatch_ack", must_not_dispatch)
    with pytest.raises(AckDispatchBlocked, match=r"^hosted ACK authority unavailable$"):
        await deliver(store, credentials, transport, delivery)
    assert not transport.calls


async def test_multiple_matching_journal_entries_do_not_dispatch(monkeypatch):
    """An ambiguous journal must fail closed rather than select one ACK."""
    delivery = page()["deliveries"][0]
    evidence = SimpleNamespace(
        local_config_entry_id="entry", delivery_id=delivery["delivery_id"],
        public_source_id=delivery["source_id"], lease_token=delivery["lease_token"],
    )
    entry = SimpleNamespace(evidence=evidence, ack_status="pending")
    store = SimpleNamespace(hosted_ack_journal=lambda: (entry, entry))
    transport = Transport()

    async def accepted(**_kwargs):
        return object()

    async def must_not_dispatch(**_kwargs):
        pytest.fail("ambiguous journal must not trigger ACK")

    monkeypatch.setattr(hosted_text_delivery, "async_accept_hosted_text", accepted)
    monkeypatch.setattr(hosted_text_delivery, "async_dispatch_ack", must_not_dispatch)
    with pytest.raises(AckDispatchBlocked, match=r"^hosted ACK authority unavailable$"):
        await deliver(store, SimpleNamespace(snapshot=checkpoint()), transport, delivery)
    assert not transport.calls


async def test_credential_change_after_acceptance_blocks_ack(monkeypatch):
    """Re-fence after persisting acceptance, before sending the ACK."""
    delivery = page()["deliveries"][0]
    cp = checkpoint()
    credentials = SimpleNamespace(snapshot=cp)
    evidence = SimpleNamespace(
        local_config_entry_id="entry", delivery_id=delivery["delivery_id"],
        public_source_id=delivery["source_id"], lease_token=delivery["lease_token"],
    )
    store = SimpleNamespace(hosted_ack_journal=lambda: (
        SimpleNamespace(evidence=evidence, ack_status="pending"),))
    transport = Transport()

    async def accepted(**_kwargs):
        credentials.snapshot = replace(cp, revision=1)
        return object()

    async def must_not_dispatch(**_kwargs):
        pytest.fail("ACK sent after credential authority changed")

    monkeypatch.setattr(hosted_text_delivery, "async_accept_hosted_text", accepted)
    monkeypatch.setattr(hosted_text_delivery, "async_dispatch_ack", must_not_dispatch)
    with pytest.raises(AckDispatchBlocked, match=r"^hosted ACK authority unavailable$"):
        await deliver(store, credentials, transport, delivery, claim=cp)
    assert not transport.calls
