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


BLOCKED_REASON = r"^hosted ACK authority unavailable$"


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
    with pytest.raises(AckDispatchBlocked, match=BLOCKED_REASON):
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
        with pytest.raises(AckDispatchBlocked, match=BLOCKED_REASON):
            await handle(store, creds, transport, delivery)
    assert not transport.calls


@pytest.mark.parametrize(
    "failure", ["not_dict", "missing_id", "missing_lease", "binding", "credentials"]
)
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
    elif failure == "missing_lease":
        delivery.pop("lease_expires_at")
    elif failure == "binding":
        transport.service_binding = HostedServiceBinding(
            "https://other.example.test", "https://account.example.test"
        )
    else:
        claim = replace(claim, revision=claim.revision + 1)
    store = SimpleNamespace(hosted_ack_journal=lambda: pytest.fail("journal examined"))
    with pytest.raises(AckDispatchBlocked, match=BLOCKED_REASON):
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

    async def reject_dispatch(**_kwargs):
        pytest.fail("changed credentials must be blocked before dispatch")

    monkeypatch.setattr(hosted_text_claim, "async_dispatch_ack", reject_dispatch)
    transport = Transport()
    with pytest.raises(AckDispatchBlocked, match=BLOCKED_REASON):
        await handle(store, creds, transport, delivery)
    assert not transport.calls
    assert store.hosted_ack_journal()[0].ack_status == "pending"


@pytest.mark.parametrize("re_lease", [False, True])
async def test_lease_expiry_during_attempt_save_never_acknowledges(monkeypatch, re_lease):
    from custom_components.daylight_calendar_import import hosted_text_claim

    if re_lease:
        store, _backend, creds, _entry, delivery = await setup(monkeypatch)
    else:
        store = make_store(monkeypatch, FakeStoreBackend())
        creds = SimpleNamespace(snapshot=checkpoint())
        delivery = page()["deliveries"][0]
    original = store.async_reconcile_hosted_ack

    async def expire_after_attempt(**kwargs):
        result = await original(**kwargs)
        if kwargs["action"] == "attempt":
            monkeypatch.setattr(
                hosted_text_claim, "_date", lambda _: datetime.now(UTC) - timedelta(seconds=1)
            )
        return result

    monkeypatch.setattr(store, "async_reconcile_hosted_ack", expire_after_attempt)
    transport = Transport()
    with pytest.raises(AckDispatchBlocked):
        await handle(store, creds, transport, delivery)
    assert not transport.calls
    assert store.hosted_ack_journal()[0].ack_status == "pending"


async def test_duplicate_pending_claims_fail_before_re_lease(monkeypatch):
    from custom_components.daylight_calendar_import import hosted_text_claim

    delivery = page()["deliveries"][0]
    evidence = SimpleNamespace(local_config_entry_id="entry", delivery_id=delivery["delivery_id"])
    pending = SimpleNamespace(evidence=evidence, ack_status="pending")
    store = SimpleNamespace(hosted_ack_journal=lambda: (pending, pending))

    async def unexpected(**kwargs):
        pytest.fail("ambiguous pending delivery was re-leased")

    monkeypatch.setattr(hosted_text_claim, "async_re_lease_hosted_text", unexpected)
    transport = Transport()
    with pytest.raises(AckDispatchBlocked, match=BLOCKED_REASON):
        await handle(store, SimpleNamespace(snapshot=checkpoint()), transport, delivery)
    assert not transport.calls


@pytest.mark.parametrize("wrong", ["entry", "delivery"])
async def test_unrelated_journal_evidence_must_not_be_re_leased(monkeypatch, wrong):
    from custom_components.daylight_calendar_import import hosted_text_claim

    delivery = page()["deliveries"][0]
    unrelated = SimpleNamespace(
        evidence=SimpleNamespace(
            local_config_entry_id="foreign" if wrong == "entry" else "entry",
            delivery_id=delivery["delivery_id"] if wrong == "entry" else "another-delivery",
        ),
        ack_status="pending",
    )
    store = SimpleNamespace(hosted_ack_journal=lambda: (unrelated,))
    accepted = object()
    calls = []

    async def new_claim(**kwargs):
        calls.append(kwargs)
        return accepted

    async def unexpected(**kwargs):
        pytest.fail("unrelated prior acceptance was re-leased")

    monkeypatch.setattr(hosted_text_claim, "async_accept_and_ack_text", new_claim)
    monkeypatch.setattr(hosted_text_claim, "async_re_lease_hosted_text", unexpected)
    transport = Transport()
    result = await handle(store, SimpleNamespace(snapshot=checkpoint()), transport, delivery)
    assert result is accepted
    assert len(calls) == 1
    assert calls[0]["delivery"] == delivery
    assert calls[0]["local_config_entry_id"] == "entry"
    assert not transport.calls


async def test_expiry_at_exact_deadline_fences_transport(monkeypatch):
    from custom_components.daylight_calendar_import import hosted_text_claim

    deadline = datetime.now(UTC) + timedelta(minutes=1)

    class BoundaryClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return deadline if tz is None else deadline.astimezone(tz)

    monkeypatch.setattr(hosted_text_claim, "datetime", BoundaryClock)
    creds = SimpleNamespace(snapshot=checkpoint())
    transport = Transport()
    bound = hosted_text_claim._LeaseBoundTransport(
        transport, creds, creds.snapshot, deadline.isoformat()
    )
    with pytest.raises(AckDispatchBlocked, match=BLOCKED_REASON):
        await bound.acknowledge_source(token="private", delivery_id="id", lease_token="secret")
    assert not transport.calls


async def test_expiry_at_exact_deadline_fences_post_save_dispatch(monkeypatch):
    from custom_components.daylight_calendar_import import hosted_text_claim

    store, _backend, creds, _entry, delivery = await setup(monkeypatch)
    deadline = datetime.fromisoformat(delivery["lease_expires_at"])

    class BoundaryClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return deadline if tz is None else deadline.astimezone(tz)

    async def unexpected(**kwargs):
        pytest.fail("expired re-lease entered ACK dispatch")

    monkeypatch.setattr(hosted_text_claim, "datetime", BoundaryClock)
    monkeypatch.setattr(hosted_text_claim, "async_dispatch_ack", unexpected)
    transport = Transport()
    with pytest.raises(AckDispatchBlocked, match=BLOCKED_REASON):
        await handle(store, creds, transport, delivery)
    assert store.hosted_ack_journal()[0].ack_status == "pending"
    assert not transport.calls


async def test_final_transport_fence_rejects_changed_credentials():
    from dataclasses import replace
    from custom_components.daylight_calendar_import.hosted_text_claim import _LeaseBoundTransport

    checkpoint_before = checkpoint()
    creds = SimpleNamespace(snapshot=replace(checkpoint_before, revision=1))
    transport = Transport()
    bound = _LeaseBoundTransport(
        transport, creds, checkpoint_before, (datetime.now(UTC) + timedelta(minutes=1)).isoformat()
    )
    with pytest.raises(AckDispatchBlocked, match=BLOCKED_REASON):
        await bound.acknowledge_source(token="private", delivery_id="id", lease_token="secret")
    assert not transport.calls
