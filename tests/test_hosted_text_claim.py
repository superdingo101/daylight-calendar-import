"""New and re-leased claims preserve acceptance before acknowledgment."""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import.hosted_ack_dispatch import AckDispatchBlocked
from custom_components.daylight_calendar_import.hosted_text_claim import async_handle_text_claim
from tests.test_hosted_ack_dispatch import SERVICE, Transport, checkpoint
from tests.test_hosted_text_acceptance import page
from tests.test_hosted_text_re_lease import setup
from tests.test_storage import FakeStoreBackend, draft, make_store


async def handle(store, creds, transport, delivery, *, parse=None):
    async def default_parse(_source):
        return SimpleNamespace(events=[draft()], event_assumptions=(), warnings=[]), None

    def route(source):
        return SimpleNamespace(parser_source=source, calendar_entity="calendar.test",
                               requires_confirmation=False, warnings=[])

    return await async_handle_text_claim(
        store=store, credentials=creds, claim_checkpoint=creds.snapshot,
        service_binding=SERVICE, transport=transport, local_config_entry_id="entry",
        delivery=delivery, route_source=route, parse_source=parse or default_parse,
    )


async def test_new_claim_is_accepted_before_ack(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    transport = Transport(on_call=lambda: backend.saved or pytest.fail("missing durable acceptance"))
    await handle(store, SimpleNamespace(snapshot=checkpoint()), transport, page()["deliveries"][0])
    assert len(transport.calls) == 1
    assert store.hosted_ack_journal()[0].ack_status == "confirmed"


async def test_re_lease_is_saved_before_ack_without_reparse(monkeypatch):
    store, backend, creds, entry, delivery = await setup(monkeypatch)

    async def no_parse(_source):
        pytest.fail("re-leased source was parsed again")

    def on_call():
        saved = backend.saved[-1]["hosted_delivery_journal"][0]
        assert saved["lease_token"] == delivery["lease_token"]
        assert saved["ack_status"] == "pending"

    transport = Transport(on_call=on_call)
    await handle(store, creds, transport, delivery, parse=no_parse)
    assert store.hosted_ack_journal()[0].ack_status == "confirmed"
    assert len(transport.calls) == 1


async def test_mismatched_re_lease_never_acknowledges(monkeypatch):
    store, _backend, creds, entry, delivery = await setup(monkeypatch)
    delivery = deepcopy(delivery)
    delivery["source"]["text"] = "changed"
    transport = Transport()
    with pytest.raises(ValueError):
        await handle(store, creds, transport, delivery)
    assert not transport.calls
    assert store.hosted_ack_journal()[0] == entry


async def test_expired_lease_after_persist_does_not_send(monkeypatch):
    from custom_components.daylight_calendar_import import hosted_text_claim

    store, _backend, creds, _entry, delivery = await setup(monkeypatch)
    original = hosted_text_claim.async_re_lease_hosted_text

    async def slow_save(**kwargs):
        result = await original(**kwargs)
        monkeypatch.setattr(hosted_text_claim, "_date", lambda _: datetime.now(UTC) - timedelta(seconds=1))
        return result

    monkeypatch.setattr(hosted_text_claim, "async_re_lease_hosted_text", slow_save)
    transport = Transport()
    with pytest.raises(AckDispatchBlocked):
        await handle(store, creds, transport, delivery)
    assert not transport.calls
    assert store.hosted_ack_journal()[0].ack_status == "pending"


async def test_terminal_prior_record_and_ambiguous_record_fail_closed():
    delivery = page()["deliveries"][0]
    evidence = SimpleNamespace(local_config_entry_id="entry", delivery_id=delivery["delivery_id"])
    terminal = SimpleNamespace(evidence=evidence, ack_status="confirmed")
    creds = SimpleNamespace(snapshot=checkpoint())
    transport = Transport()
    for entries in ((terminal,), (terminal, terminal)):
        store = SimpleNamespace(hosted_ack_journal=lambda: entries)
        with pytest.raises(AckDispatchBlocked):
            await handle(store, creds, transport, delivery)
    assert not transport.calls


@pytest.mark.parametrize("failure", ["not_dict", "missing_id", "binding", "credentials"])
async def test_invalid_claim_authority_never_acts(failure):
    from dataclasses import replace
    from custom_components.daylight_calendar_import.hosted_transport import HostedServiceBinding

    delivery = page()["deliveries"][0]
    creds = SimpleNamespace(snapshot=checkpoint())
    claim = creds.snapshot
    transport = Transport()
    if failure == "not_dict":
        delivery = []
    elif failure == "missing_id":
        delivery.pop("delivery_id")
    elif failure == "binding":
        transport.service_binding = HostedServiceBinding(
            "https://other.example.test", "https://account.example.test"
        )
    else:
        claim = replace(claim, revision=claim.revision + 1)
    store = SimpleNamespace(hosted_ack_journal=lambda: pytest.fail("journal examined"))
    with pytest.raises(AckDispatchBlocked):
        await async_handle_text_claim(
            store=store, credentials=creds, claim_checkpoint=claim, service_binding=SERVICE,
            transport=transport, local_config_entry_id="entry", delivery=delivery,
            route_source=None, parse_source=None,
        )
    assert not transport.calls


async def test_credential_change_during_re_lease_never_acknowledges(monkeypatch):
    from dataclasses import replace
    from custom_components.daylight_calendar_import import hosted_text_claim

    store, _backend, creds, _entry, delivery = await setup(monkeypatch)
    original = hosted_text_claim.async_re_lease_hosted_text

    async def change_after_save(**kwargs):
        result = await original(**kwargs)
        creds.snapshot = replace(creds.snapshot, revision=creds.snapshot.revision + 1)
        return result

    monkeypatch.setattr(hosted_text_claim, "async_re_lease_hosted_text", change_after_save)
    transport = Transport()
    with pytest.raises(AckDispatchBlocked):
        await handle(store, creds, transport, delivery)
    assert not transport.calls
    assert store.hosted_ack_journal()[0].ack_status == "pending"
