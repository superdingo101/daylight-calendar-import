"""Hosted acceptance and acknowledgement evidence share one durable write."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest

from custom_components.daylight_calendar_import.hosted_delivery_journal import (
    HostedDeliveryEvidence,
    HostedDeliveryJournalEntry,
)
from custom_components.daylight_calendar_import.storage import PendingImportStorageError
from tests.test_storage import FakeStoreBackend, draft, make_store


def evidence(delivery_id="delivery"):
    return HostedDeliveryEvidence("entry", delivery_id, "secret-token",
        (datetime.now(UTC) + timedelta(days=1)).isoformat(), "a" * 64, "b" * 64)


@pytest.mark.parametrize("events,expected", [([draft()], "pending"), ([], "no_events")])
async def test_atomic_acceptance_survives_restart_and_review(monkeypatch, events, expected):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    result = await store.async_add(source_text="source", events=events,
                                   source_id=item.source_id, hosted_evidence=item)
    assert len(backend.saved) == 1
    snapshot = backend.saved[-1]
    assert snapshot["hosted_delivery_journal"][0]["acceptance_disposition"] == expected
    assert bool(snapshot["items"]) == bool(events)
    restarted = make_store(monkeypatch, FakeStoreBackend(load_result=snapshot))
    await restarted.async_load()
    assert restarted._hosted_delivery_journal[0].evidence == item
    if result.pending:
        assert await restarted.async_remove(result.pending.id)
        assert restarted._hosted_delivery_journal[0].evidence == item


async def test_duplicate_and_failed_write_preserve_atomicity(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    first = evidence("first")
    await store.async_add(source_text="source", events=[draft()], source_id=first.source_id, hosted_evidence=first)
    second = evidence("second")
    result = await store.async_add(source_text="source", events=[draft()], source_id=second.source_id, hosted_evidence=second)
    assert result.duplicate_events == 1
    assert backend.saved[-1]["hosted_delivery_journal"][-1]["acceptance_disposition"] == "duplicate"
    third = evidence("third")
    before = backend.saved[-1]
    backend.save_error = OSError("ambiguous save")
    with pytest.raises(OSError):
        await store.async_add(source_text="source", events=[], source_id=third.source_id, hosted_evidence=third)
    assert backend.saved[-1] is before
    assert len(store._hosted_delivery_journal) == 2
    with pytest.raises(PendingImportStorageError):
        await store.async_add(source_text="source", events=[replace(draft(), title="Other")])


async def test_reject_expiry_identity_and_duplicate_obligation(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    with pytest.raises(ValueError):
        await store.async_add(source_text="source", events=[], source_id="other", hosted_evidence=item)
    expired = replace(item, source_expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(ValueError):
        await store.async_add(source_text="source", events=[], source_id=expired.source_id, hosted_evidence=expired)
    assert not backend.saved
    await store.async_add(source_text="source", events=[], source_id=item.source_id, hosted_evidence=item)
    with pytest.raises(ValueError):
        await store.async_add(source_text="source", events=[], source_id=item.source_id, hosted_evidence=item)


def test_evidence_validation_fails_closed_and_hides_token():
    item = evidence()
    assert "secret-token" not in repr(item)
    for invalid in (replace(item, delivery_id="bad:identity"), replace(item, lease_token=""),
                    replace(item, source_expires_at="tomorrow"), replace(item, source_evidence_sha256="x")):
        with pytest.raises(ValueError):
            invalid.validate()
    with pytest.raises(ValueError):
        HostedDeliveryJournalEntry.from_dict({})


def test_journal_rejects_corrupt_fields():
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    item = evidence()
    record = HostedDeliveryJournalEntry(item, source_fingerprint(item.source_id), "pending").as_dict()
    for key, value in (("acceptance_disposition", "failed"), ("ack_status", "unknown"),
                       ("attempt_count", -1), ("last_attempt_at", 42),
                       ("source_fingerprint", "wrong")):
        with pytest.raises(ValueError):
            HostedDeliveryJournalEntry.from_dict({**record, key: value})
    with pytest.raises(ValueError):
        replace(item, source_expires_at="2030-01-01T00:00:00").validate()


async def test_preexisting_source_can_record_duplicate_obligation(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    await store.async_add(source_text="source", events=[draft()], source_id=item.source_id)
    result = await store.async_add(source_text="source", events=[draft()], source_id=item.source_id,
                                   hosted_evidence=item)
    assert result.duplicate_source
    assert backend.saved[-1]["hosted_delivery_journal"][0]["acceptance_disposition"] == "duplicate"


async def test_ambiguous_commit_requires_reload_and_preserves_outcome(monkeypatch):
    class AmbiguousBackend(FakeStoreBackend):
        async def async_save(self, data):
            self.load_result = data
            raise OSError("response lost after commit")

    backend = AmbiguousBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    with pytest.raises(OSError):
        await store.async_add(source_text="source", events=[draft()], source_id=item.source_id,
                              hosted_evidence=item)
    assert not store._hosted_delivery_journal
    backend.__class__ = FakeStoreBackend
    await store.async_load()
    assert store._hosted_delivery_journal[0].evidence == item
    assert len(store.list()) == 1


async def test_active_source_claim_is_not_an_ack_eligible_duplicate(monkeypatch):
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    store._source_claims["other-worker"] = source_fingerprint(item.source_id)
    with pytest.raises(ValueError, match="already being processed"):
        await store.async_add(source_text="source", events=[draft()], source_id=item.source_id,
                              hosted_evidence=item)
    assert not store._hosted_delivery_journal
    assert not backend.saved
