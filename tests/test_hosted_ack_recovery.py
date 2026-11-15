"""Restart recovery submits only bounded durable pending obligations."""
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import.hosted_ack_recovery import async_recover_pending_acks
from custom_components.daylight_calendar_import.hosted_transport import HostedTransportError
from tests.test_hosted_ack_dispatch import ACCOUNT, INSTALLATION, Transport, checkpoint
from tests.test_hosted_delivery_journal import evidence
from tests.test_storage import FakeStoreBackend, make_store


async def accepted(store, delivery_id):
    item = replace(evidence(delivery_id), account_id=ACCOUNT, installation_id=INSTALLATION,
                   public_source_id="source_" + delivery_id.ljust(16, "0"))
    await store.async_add(source_text="text", events=[], source_id=item.source_id,
                          hosted_evidence=item)


async def run(store, transport, limit=20, credentials=None):
    return await async_recover_pending_acks(
        store=store, credentials=credentials or SimpleNamespace(snapshot=checkpoint()),
        transport=transport, local_config_entry_id="entry", limit=limit,
    )


@pytest.mark.parametrize("limit", [0, 51, True, "1"])
async def test_limit_rejected_before_side_effect(monkeypatch, limit):
    store = make_store(monkeypatch, FakeStoreBackend())
    transport = Transport()
    with pytest.raises(ValueError, match="ACK recovery limit"):
        await run(store, transport, limit)
    assert not transport.calls


async def test_restart_recovery_is_bounded_and_skips_confirmed(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    await accepted(store, "first")
    await accepted(store, "second")
    restarted = make_store(monkeypatch, FakeStoreBackend(load_result=backend.saved[-1]))
    await restarted.async_load()
    transport = Transport()
    assert len(await run(restarted, transport, 1)) == 1
    assert [x["delivery_id"] for x in transport.calls] == ["first"]
    assert len(await run(restarted, transport, 1)) == 1
    assert [x["delivery_id"] for x in transport.calls] == ["first", "second"]
    assert await run(restarted, transport) == []


async def test_lost_ack_response_stays_pending_and_stops_batch(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    await accepted(store, "first")
    await accepted(store, "second")
    transport = Transport(error=HostedTransportError("unavailable", retryable=True))
    with pytest.raises(HostedTransportError):
        await run(store, transport)
    assert [x["delivery_id"] for x in transport.calls] == ["first"]
    assert [x.ack_status for x in store.hosted_ack_journal()] == ["pending", "pending"]
    assert [x.attempt_count for x in store.hosted_ack_journal()] == [1, 0]


async def test_other_entry_is_never_dispatched(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    await accepted(store, "first")
    entry = store.hosted_ack_journal()[0]
    store._hosted_delivery_journal = (replace(
        entry, evidence=replace(entry.evidence, local_config_entry_id="other"),
    ),)
    transport = Transport()
    assert await run(store, transport) == []
    assert not transport.calls


async def test_expired_pending_is_marked_without_credential_or_network(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    await accepted(store, "first")
    entry = store.hosted_ack_journal()[0]
    expired = replace(entry, evidence=replace(
        entry.evidence, source_expires_at=(datetime.now(UTC) - timedelta(seconds=1)).isoformat(),
    ))
    store._hosted_delivery_journal = (expired,)
    transport = Transport()
    result = await run(store, transport, credentials=SimpleNamespace(snapshot=None))
    assert result[0].ack_status == "expired"
    assert not transport.calls


async def test_stale_lease_is_left_pending_and_other_acks_recovered(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    await accepted(store, "first")
    await accepted(store, "second")
    transport = Transport(error=HostedTransportError("lease_not_current", status=409))
    assert await run(store, transport) == []
    assert len(transport.calls) == 2
    assert all(item.ack_status == "pending" for item in store.hosted_ack_journal())


@pytest.mark.parametrize(
    ("status", "code"),
    [(409, "some_other_conflict"), (503, "lease_not_current")],
)
async def test_only_definitive_stale_lease_allows_recovery_to_continue(
    monkeypatch, status, code,
):
    """A matching code alone or HTTP conflict alone is not sufficient evidence."""
    store = make_store(monkeypatch, FakeStoreBackend())
    await accepted(store, "first")
    await accepted(store, "second")
    transport = Transport(error=HostedTransportError(code, status=status))
    with pytest.raises(HostedTransportError) as failure:
        await run(store, transport)
    assert failure.value.code == code
    assert failure.value.status == status
    assert [call["delivery_id"] for call in transport.calls] == ["first"]
    assert [item.ack_status for item in store.hosted_ack_journal()] == [
        "pending", "pending",
    ]


async def test_recovery_default_limit_is_exactly_twenty(monkeypatch):
    """Recovery must not accidentally drain a larger journal on one pass."""
    store = make_store(monkeypatch, FakeStoreBackend())
    for index in range(21):
        await accepted(store, f"delivery-{index:02}")
    transport = Transport()
    completed = await async_recover_pending_acks(
        store=store,
        credentials=SimpleNamespace(snapshot=checkpoint()),
        transport=transport,
        local_config_entry_id="entry",
    )
    assert len(completed) == 20
    assert len(transport.calls) == 20
    assert transport.calls[0]["delivery_id"] == "delivery-00"
    assert transport.calls[-1]["delivery_id"] == "delivery-19"
    assert [item.ack_status for item in store.hosted_ack_journal()].count("pending") == 1


async def test_recovery_allows_documented_maximum_limit(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    assert await run(store, Transport(), limit=50) == []


async def test_recovery_invalid_limit_error_is_stable(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    with pytest.raises(ValueError) as failure:
        await run(store, Transport(), limit=51)
    assert str(failure.value) == "ACK recovery limit must be 1 to 50"
