"""Hosted acceptance and acknowledgement evidence share one durable write."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from hashlib import sha256

import pytest

from custom_components.daylight_calendar_import.hosted_delivery_journal import (
    HostedDeliveryEvidence,
    HostedDeliveryJournalEntry,
)
from custom_components.daylight_calendar_import.storage import PendingImportStorageError
from tests.test_storage import FakeStoreBackend, draft, make_store


def evidence(delivery_id="delivery"):
    return HostedDeliveryEvidence("entry", delivery_id, "secret-token",
        (datetime.now(UTC) + timedelta(days=1)).isoformat(), "a" * 64, sha256(b'{"attachments":[]}').hexdigest())


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
    before = backend.saved[-1]
    with pytest.raises(ValueError, match="no proven successful outcome"):
        await store.async_add(source_text="source", events=[draft()], source_id=item.source_id,
                              hosted_evidence=item)
    assert backend.saved[-1] is before
    assert not store._hosted_delivery_journal


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
    with pytest.raises(ValueError, match="no proven successful outcome"):
        await store.async_add(source_text="source", events=[draft()], source_id=item.source_id,
                              hosted_evidence=item)
    assert not store._hosted_delivery_journal
    assert not backend.saved


async def test_terminal_failure_history_cannot_authorize_ack(monkeypatch):
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    store._seen_source_fingerprints = (source_fingerprint(item.source_id),)
    with pytest.raises(ValueError, match="no proven successful outcome"):
        await store.async_add(source_text="source", events=[draft()], source_id=item.source_id,
                              hosted_evidence=item)
    assert not store._hosted_delivery_journal and not backend.saved


def test_attachment_manifest_persists_and_rejects_invalid_evidence():
    import json
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    descriptor = {"id": "image-1", "filename": "é.png", "media_type": "image/png",
                  "size_bytes": 7, "sha256": "c" * 64}
    checked = {"id": "image-1", "size_bytes": 7, "sha256": "c" * 64}
    digest = sha256(json.dumps({"attachments": [descriptor]}, ensure_ascii=False,
                              sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    item = replace(evidence(), attachment_descriptors=(descriptor,),
                   verified_attachments=(checked,), attachment_manifest_sha256=digest)
    item.validate()
    record = HostedDeliveryJournalEntry(item, source_fingerprint(item.source_id), "pending")
    assert HostedDeliveryJournalEntry.from_dict(record.as_dict()) == record
    for invalid in (replace(item, attachment_descriptors=None),
                    replace(item, verified_attachments=()),
                    replace(item, verified_attachments=({"other": 1},)),
                    replace(item, attachment_descriptors=({**descriptor, "filename": "\ud800"},)),
                    replace(item, verified_attachments=({**checked, "size_bytes": 8},)),
                    replace(item, attachment_manifest_sha256="0" * 64),
                    replace(item, attachment_descriptors=({**descriptor, "media_type": "text/plain"},)),
                    replace(item, attachment_descriptors=(descriptor,) * 5)):
        with pytest.raises(ValueError):
            invalid.validate()


# Mutation regression coverage: verify the exact trust-boundary contract, not just
# whether a malformed payload happened to raise some exception.
@pytest.mark.parametrize("field,value,message", [
    ("local_config_entry_id", "", "invalid hosted delivery identity"),
    ("local_config_entry_id", "invalid:id", "invalid hosted delivery identity"),
    ("delivery_id", "bad/id", "invalid hosted delivery identity"),
    ("delivery_id", "x" * 129, "invalid hosted delivery identity"),
    ("lease_token", "", "invalid hosted lease token"),
    ("lease_token", "x" * 1025, "invalid hosted lease token"),
    ("lease_token", None, "invalid hosted lease token"),
    ("source_evidence_sha256", "A" * 64, "invalid hosted evidence digest"),
    ("source_evidence_sha256", "x" * 64, "invalid hosted evidence digest"),
    ("attachment_manifest_sha256", "0" * 63, "invalid hosted evidence digest"),
    ("attachment_descriptors", None, "invalid hosted attachment evidence"),
    ("verified_attachments", None, "invalid hosted attachment evidence"),
    ("attachment_descriptors", ({},), "invalid hosted attachment evidence"),
    ("source_expires_at", "2026-01-01T00:00:00", "invalid hosted source expiry"),
])
def test_evidence_rejects_invalid_values_with_specific_error(field, value, message):
    with pytest.raises(ValueError, match="^" + message + "$"):
        replace(evidence(), **{field: value}).validate()


def test_lease_token_and_identity_inclusive_limits():
    base = evidence()
    for token in ("a", "x" * 1024):
        replace(base, lease_token=token).validate()
    for ident in ("a", "A_0-" * 32):
        replace(base, local_config_entry_id=ident, delivery_id=ident).validate()
    assert replace(base, local_config_entry_id="x", delivery_id="y").source_id == "hosted:x:y"


def _with_attachments(descriptors, verified=None):
    import json

    if verified is None:
        verified = tuple({"id": d["id"], "size_bytes": d["size_bytes"], "sha256": d["sha256"]}
                         for d in descriptors)
    manifest = json.dumps({"attachments": list(descriptors)}, ensure_ascii=False,
                          sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return replace(evidence(), attachment_descriptors=tuple(descriptors),
                   verified_attachments=tuple(verified),
                   attachment_manifest_sha256=sha256(manifest).hexdigest())


def _descriptor(identifier="a", media_type="image/png", size=1, filename="a"):
    return {"id": identifier, "filename": filename, "media_type": media_type,
            "size_bytes": size, "sha256": "a" * 64}


@pytest.mark.parametrize("media_type", [
    "image/jpeg", "image/png", "image/webp", "application/pdf",
])
def test_all_supported_attachment_media_types_round_trip(media_type):
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    item = _with_attachments([_descriptor(media_type=media_type)])
    item.validate()
    entry = HostedDeliveryJournalEntry(item, source_fingerprint(item.source_id), "pending")
    assert HostedDeliveryJournalEntry.from_dict(entry.as_dict()) == entry


def test_four_attachments_and_total_size_limit_are_inclusive():
    entries = [_descriptor(str(i), size=1) for i in range(4)]
    _with_attachments(entries).validate()
    entries[0]["size_bytes"] = 10485757
    _with_attachments(entries).validate()
    entries[0]["size_bytes"] = 10485758
    with pytest.raises(ValueError, match="invalid hosted attachment manifest"):
        _with_attachments(entries).validate()


@pytest.mark.parametrize("field,value", [
    ("id", "bad/id"), ("id", ""), ("id", ".hidden"), ("id", "x" * 65),
    ("media_type", "text/plain"), ("media_type", "IMAGE/PNG"),
    ("size_bytes", 0), ("size_bytes", 10485761), ("size_bytes", True),
    ("sha256", "A" * 64), ("sha256", "a" * 63),
    ("filename", ""), ("filename", "a" * 513), ("filename", 42),
])
def test_attachment_field_rejection(field, value):
    entry = _descriptor()
    entry[field] = value
    item = _with_attachments([entry])
    with pytest.raises(ValueError, match="invalid hosted attachment evidence"):
        item.validate()


def test_attachment_field_boundaries_and_optional_filename():
    for size in (1, 10485760):
        _with_attachments([_descriptor(size=size)]).validate()
    for filename in (None, "x", "x" * 512):
        _with_attachments([_descriptor(filename=filename)]).validate()
    descriptor = _descriptor()
    del descriptor["filename"]
    _with_attachments([descriptor]).validate()


def test_attachment_duplicate_ids_mismatched_checks_and_extra_fields():
    first, second = _descriptor("a"), _descriptor("a")
    with pytest.raises(ValueError, match="invalid hosted attachment evidence"):
        _with_attachments([first, second]).validate()

    good = _descriptor()
    verified = {"id": "a", "size_bytes": 1, "sha256": "a" * 64}
    for bad in (
        {**verified, "size_bytes": 2},
        {**verified, "sha256": "b" * 64},
        {**verified, "id": "b"},
        {**verified, "extra": "x"},
        {**verified, "size_bytes": True},
        {"id": "a", "sha256": "a" * 64},
    ):
        with pytest.raises(ValueError, match="invalid hosted attachment evidence"):
            _with_attachments([good], [bad]).validate()
    with pytest.raises(ValueError, match="invalid hosted attachment evidence"):
        _with_attachments([{**good, "extra": "x"}]).validate()
    with pytest.raises(ValueError, match="invalid hosted attachment evidence"):
        _with_attachments([good], []).validate()


@pytest.mark.parametrize("field,value", [
    ("acceptance_disposition", "failed"),
    ("ack_status", "unknown"),
    ("attempt_count", -1),
    ("attempt_count", True),
    ("attempt_count", "1"),
    ("last_attempt_at", 7),
    ("source_fingerprint", "invalid"),
])
def test_journal_field_validation_exact_error(field, value):
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    item = evidence()
    record = HostedDeliveryJournalEntry(item, source_fingerprint(item.source_id), "pending").as_dict()
    with pytest.raises(ValueError, match="^invalid hosted " +
                       ("source fingerprint" if field == "source_fingerprint"
                        else "delivery journal") + "$"):
        HostedDeliveryJournalEntry.from_dict({**record, field: value})


@pytest.mark.parametrize("disposition", ["pending", "duplicate", "no_events"])
@pytest.mark.parametrize("status", ["pending", "confirmed", "expired"])
def test_journal_accepts_all_persisted_states(disposition, status):
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    item = evidence()
    entry = HostedDeliveryJournalEntry(item, source_fingerprint(item.source_id),
                                       disposition, status, 1, "2026-11-08T12:00:00Z")
    assert HostedDeliveryJournalEntry.from_dict(entry.as_dict()) == entry


def test_journal_rejects_missing_and_extra_fields_without_discarding_evidence():
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    item = evidence()
    data = HostedDeliveryJournalEntry(item, source_fingerprint(item.source_id), "pending").as_dict()
    for field in data:
        damaged = dict(data)
        damaged.pop(field)
        with pytest.raises(ValueError, match="invalid hosted delivery journal"):
            HostedDeliveryJournalEntry.from_dict(damaged)
    with pytest.raises(ValueError, match="invalid hosted delivery journal"):
        HostedDeliveryJournalEntry.from_dict({**data, "unknown": "value"})


async def test_hosted_acceptance_rejects_ambiguous_identity_expiry_and_duplicate_with_exact_errors(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    with pytest.raises(ValueError, match="^hosted source identity does not match evidence$"):
        await store.async_add(source_text="source", events=[], source_id="unrelated",
                              hosted_evidence=item)
    expired = replace(item, source_expires_at="2020-01-01T00:00:00Z")
    with pytest.raises(ValueError, match="^hosted source has expired$"):
        await store.async_add(source_text="source", events=[],
                              source_id=item.source_id, hosted_evidence=expired)
    await store.async_add(source_text="source", events=[],
                          source_id=item.source_id, hosted_evidence=item)
    with pytest.raises(ValueError, match="^hosted delivery already has an ACK obligation$"):
        await store.async_add(source_text="source", events=[],
                              source_id=item.source_id, hosted_evidence=item)
    assert len(backend.saved) == 1


async def test_hosted_journal_write_failure_blocks_services_and_reload_recovers(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    backend.save_error = OSError("disk unavailable")
    item = evidence()
    with pytest.raises(OSError, match="disk unavailable"):
        await store.async_add(source_text="source", events=[draft()],
                              source_id=item.source_id, hosted_evidence=item)
    assert store._storage_blocked is True
    assert store.accepting_services is False
    assert store._hosted_delivery_journal == ()
    backend.save_error = None
    await store.async_load()
    assert store._storage_blocked is False
    assert store._hosted_delivery_journal == ()


async def test_hosted_no_events_persists_fingerprint_and_disposition_without_pending(monkeypatch):
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    result = await store.async_add(source_text="source", events=[],
                                   source_id=item.source_id, hosted_evidence=item)
    assert result.pending is None
    assert len(backend.saved) == 1
    snapshot = backend.saved[-1]
    assert snapshot["items"] == []
    assert source_fingerprint(item.source_id) in snapshot["seen_source_fingerprints"]
    journal = snapshot["hosted_delivery_journal"]
    assert len(journal) == 1
    assert journal[0]["acceptance_disposition"] == "no_events"
    assert journal[0]["source_fingerprint"] == source_fingerprint(item.source_id)
    assert journal[0]["ack_status"] == "pending"
    assert journal[0]["attempt_count"] == 0
    assert journal[0]["last_attempt_at"] is None


def test_attachment_manifest_detects_modified_digest_and_filename():
    original = _descriptor(filename="one.png")
    item = _with_attachments([original])
    item.validate()
    for changed in (
        {**original, "filename": "two.png"},
        {**original, "sha256": "b" * 64},
        {**original, "media_type": "image/jpeg"},
    ):
        with pytest.raises(ValueError):
            replace(item, attachment_descriptors=(changed,)).validate()


async def test_default_import_metadata_is_unchanged_with_hosted_journal(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    result = await store.async_add(source_text="source", events=[draft()],
                                   source_id=item.source_id, hosted_evidence=item)
    assert result.pending is not None
    assert result.pending.source_kind == "manual_text"
    assert result.pending.events[0].routing_unresolved is False
    assert result.duplicate_events == 0
    assert store._hosted_delivery_journal[0].acceptance_disposition == "pending"


async def test_hosted_source_claim_mismatch_has_distinct_error(monkeypatch):
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    store._source_claims["claim"] = source_fingerprint("different")
    with pytest.raises(ValueError, match="^source_id does not match claimed source$"):
        await store.async_add(source_text="source", events=[draft()],
                              source_id=item.source_id, hosted_evidence=item,
                              activity_id="claim")
    assert not backend.saved


async def test_hosted_preexisting_fingerprint_error_is_exact(monkeypatch):
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    store._seen_source_fingerprints = (source_fingerprint(item.source_id),)
    with pytest.raises(ValueError, match="^hosted source has no proven successful outcome$"):
        await store.async_add(source_text="source", events=[draft()],
                              source_id=item.source_id, hosted_evidence=item)
    assert not backend.saved


def test_five_distinct_attachments_exceed_hosted_attachment_count_limit():
    descriptors = [_descriptor(str(index)) for index in range(5)]
    with pytest.raises(ValueError, match="^invalid hosted attachment evidence$"):
        _with_attachments(descriptors).validate()


def test_uppercase_hosted_attachment_identifier_is_supported():
    _with_attachments([_descriptor("ABC_09")]).validate()


def test_hosted_manifest_mismatch_reports_exact_error():
    item = _with_attachments([_descriptor()])
    with pytest.raises(ValueError, match="^invalid hosted attachment manifest$"):
        replace(item, attachment_manifest_sha256="0" * 64).validate()


def test_invalid_unicode_filename_reports_exact_evidence_error():
    item = _with_attachments([_descriptor()])
    malformed = {**item.attachment_descriptors[0], "filename": chr(0xD800)}
    with pytest.raises(ValueError, match="^invalid hosted attachment evidence$"):
        replace(item, attachment_descriptors=(malformed,)).validate()


@pytest.mark.parametrize("has_attachment", [False, True])
async def test_lease_replacement_accepts_list_evidence_after_restart(monkeypatch, has_attachment):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = _with_attachments([_descriptor()] if has_attachment else [])
    await store.async_add(source_text="source", events=[], source_id=item.source_id, hosted_evidence=item)
    restarted = make_store(monkeypatch, FakeStoreBackend(load_result=backend.saved[-1]))
    await restarted.async_load()
    entry = restarted.hosted_ack_journal()[0]
    fresh = replace(item, lease_token="new-secret",
                    attachment_descriptors=list(item.attachment_descriptors),
                    verified_attachments=list(item.verified_attachments))
    updated = await restarted.async_reconcile_hosted_ack(entry=entry, action="replace_lease", new_evidence=fresh)
    assert updated.evidence.lease_token == "new-secret"
    assert updated.evidence.attachment_descriptors == tuple(fresh.attachment_descriptors)
    assert updated.evidence.verified_attachments == tuple(fresh.verified_attachments)


async def test_ack_reconciliation_persists_cas_and_rejects_changed_evidence(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    await store.async_add(source_text="source", events=[], source_id=item.source_id, hosted_evidence=item)
    original = store.hosted_ack_journal()[0]
    attempted = await store.async_reconcile_hosted_ack(entry=original, action="attempt")
    assert attempted.attempt_count == 1 and attempted.last_attempt_at
    with pytest.raises(ValueError, match="stale"):
        await store.async_reconcile_hosted_ack(entry=original, action="confirm")
    replacement = replace(item, lease_token="new-secret")
    with pytest.raises(ValueError, match="changed"):
        await store.async_reconcile_hosted_ack(entry=attempted, action="replace_lease",
                                                new_evidence=replace(replacement, source_evidence_sha256="b" * 64))
    updated = await store.async_reconcile_hosted_ack(entry=attempted, action="replace_lease", new_evidence=replacement)
    with pytest.raises(ValueError, match="stale"):
        await store.async_reconcile_hosted_ack(entry=attempted, action="confirm")
    confirmed = await store.async_reconcile_hosted_ack(entry=updated, action="confirm")
    assert confirmed.ack_status == "confirmed"
    restarted = make_store(monkeypatch, FakeStoreBackend(load_result=backend.saved[-1]))
    await restarted.async_load()
    assert restarted.hosted_ack_journal() == (confirmed,)
    with pytest.raises(ValueError, match="terminal"):
        await restarted.async_reconcile_hosted_ack(entry=confirmed, action="attempt")


async def test_ack_reconciliation_rejects_invalid_actions_and_expiry(monkeypatch):
    from custom_components.daylight_calendar_import import storage as storage_module

    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    await store.async_add(source_text="source", events=[], source_id=item.source_id, hosted_evidence=item)
    entry = store.hosted_ack_journal()[0]
    for action, replacement in (("bogus", None), ("expire", None), ("replace_lease", None),
                                ("attempt", item), ("confirm", item)):
        with pytest.raises(ValueError):
            await store.async_reconcile_hosted_ack(entry=entry, action=action, new_evidence=replacement)
    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=2)
    monkeypatch.setattr(storage_module, "datetime", Future)
    with pytest.raises(ValueError, match="expired"):
        await store.async_reconcile_hosted_ack(entry=entry, action="confirm")
    expired = await store.async_reconcile_hosted_ack(entry=entry, action="expire")
    assert expired.ack_status == "expired"


async def test_ack_reconciliation_blocks_after_uncertain_write(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    await store.async_add(source_text="source", events=[], source_id=item.source_id, hosted_evidence=item)
    entry = store.hosted_ack_journal()[0]
    backend.save_error = OSError("uncertain")
    with pytest.raises(OSError):
        await store.async_reconcile_hosted_ack(entry=entry, action="confirm")
    with pytest.raises(PendingImportStorageError):
        store.hosted_ack_journal()
    with pytest.raises(PendingImportStorageError):
        await store.async_reconcile_hosted_ack(entry=entry, action="attempt")


async def test_cancelled_ack_update_finishes_the_durable_write(monkeypatch):
    import asyncio

    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence()
    await store.async_add(source_text="source", events=[], source_id=item.source_id, hosted_evidence=item)
    entry = store.hosted_ack_journal()[0]
    started, release = asyncio.Event(), asyncio.Event()
    save = backend.async_save_immediate

    async def delayed_save(data):
        started.set()
        await release.wait()
        await save(data)

    monkeypatch.setattr(backend, "async_save_immediate", delayed_save)
    task = asyncio.create_task(store.async_reconcile_hosted_ack(entry=entry, action="attempt"))
    await started.wait()
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert store.hosted_ack_journal()[0].attempt_count == 1
    restarted = make_store(monkeypatch, FakeStoreBackend(load_result=backend.saved[-1]))
    await restarted.async_load()
    assert restarted.hosted_ack_journal()[0].attempt_count == 1


# Mutation regressions for durable ACK journal state transitions.
async def _accepted_journal(monkeypatch, *, item=None):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    item = evidence() if item is None else item
    await store.async_add(source_text="source", events=[], source_id=item.source_id, hosted_evidence=item)
    return store, backend, store.hosted_ack_journal()[0]


async def test_ack_attempts_are_monotonic_and_persist_each_increment(monkeypatch):
    store, backend, entry = await _accepted_journal(monkeypatch)
    initial_count = len(backend.saved)
    last_attempt = entry.last_attempt_at
    for count in (1, 2, 3):
        entry = await store.async_reconcile_hosted_ack(entry=entry, action="attempt")
        assert entry.ack_status == "pending"
        assert entry.attempt_count == count
        assert entry.last_attempt_at is not None
        assert entry.last_attempt_at != last_attempt or count > 1
        assert backend.saved[-1]["hosted_delivery_journal"][0]["attempt_count"] == count
        last_attempt = entry.last_attempt_at
    assert len(backend.saved) == initial_count + 3
    restarted = make_store(monkeypatch, FakeStoreBackend(load_result=backend.saved[-1]))
    await restarted.async_load()
    assert restarted.hosted_ack_journal() == (entry,)


async def test_ack_confirmation_only_changes_terminal_status(monkeypatch):
    store, backend, entry = await _accepted_journal(monkeypatch)
    attempted = await store.async_reconcile_hosted_ack(entry=entry, action="attempt")
    confirmed = await store.async_reconcile_hosted_ack(entry=attempted, action="confirm")
    assert confirmed.ack_status == "confirmed"
    assert confirmed.evidence == attempted.evidence
    assert confirmed.attempt_count == attempted.attempt_count
    assert confirmed.last_attempt_at == attempted.last_attempt_at
    assert backend.saved[-1]["hosted_delivery_journal"][0]["ack_status"] == "confirmed"
    for action in ("attempt", "confirm", "expire", "replace_lease"):
        with pytest.raises(ValueError, match="terminal"):
            await store.async_reconcile_hosted_ack(
                entry=confirmed, action=action,
                new_evidence=replace(confirmed.evidence, lease_token="replacement") if action == "replace_lease" else None,
            )


async def test_ack_replacement_only_rotates_lease_token(monkeypatch):
    store, backend, entry = await _accepted_journal(monkeypatch)
    fresh = replace(entry.evidence, lease_token="rotated-token")
    updated = await store.async_reconcile_hosted_ack(
        entry=entry, action="replace_lease", new_evidence=fresh,
    )
    assert updated.evidence == fresh
    assert updated.evidence.lease_token != entry.evidence.lease_token
    assert updated.ack_status == entry.ack_status
    assert updated.attempt_count == entry.attempt_count
    assert updated.last_attempt_at == entry.last_attempt_at
    assert backend.saved[-1]["hosted_delivery_journal"][0]["lease_token"] == "rotated-token"


@pytest.mark.parametrize("field,value", [
    ("local_config_entry_id", "different"),
    ("delivery_id", "different"),
    ("source_expires_at", "2099-01-01T00:00:00Z"),
    ("source_evidence_sha256", "b" * 64),
    ("attachment_manifest_sha256", "b" * 64),
])
async def test_ack_lease_replacement_rejects_changed_immutable_evidence(monkeypatch, field, value):
    store, backend, entry = await _accepted_journal(monkeypatch)
    fresh = replace(entry.evidence, lease_token="new-secret", **{field: value})
    snapshot = backend.saved[-1]
    with pytest.raises(ValueError):
        await store.async_reconcile_hosted_ack(entry=entry, action="replace_lease", new_evidence=fresh)
    assert backend.saved[-1] is snapshot
    assert store.hosted_ack_journal() == (entry,)


async def test_ack_lease_replacement_rejects_corrupt_fresh_evidence(monkeypatch):
    store, backend, entry = await _accepted_journal(monkeypatch)
    snapshot = backend.saved[-1]
    with pytest.raises(ValueError):
        await store.async_reconcile_hosted_ack(
            entry=entry, action="replace_lease",
            new_evidence=replace(entry.evidence, lease_token=""),
        )
    assert backend.saved[-1] is snapshot


async def test_ack_reconcile_rejects_unexpected_new_evidence_without_write(monkeypatch):
    store, backend, entry = await _accepted_journal(monkeypatch)
    snapshot = backend.saved[-1]
    for action in ("attempt", "confirm", "expire"):
        with pytest.raises(ValueError):
            await store.async_reconcile_hosted_ack(
                entry=entry, action=action, new_evidence=entry.evidence,
            )
        assert backend.saved[-1] is snapshot
    assert store.hosted_ack_journal() == (entry,)


async def test_ack_journal_returns_independent_snapshots(monkeypatch):
    store, _, entry = await _accepted_journal(monkeypatch, item=_with_attachments([_descriptor()]))
    first = store.hosted_ack_journal()
    assert first is not store.hosted_ack_journal()
    first[0].evidence.attachment_descriptors[0]["filename"] = "changed.png"
    assert store.hosted_ack_journal() == (entry,)


async def test_ack_reconcile_rejects_stale_or_missing_identity_without_write(monkeypatch):
    store, backend, entry = await _accepted_journal(monkeypatch)
    snapshot = backend.saved[-1]
    stale = replace(entry, attempt_count=999)
    with pytest.raises(ValueError, match="stale"):
        await store.async_reconcile_hosted_ack(entry=stale, action="attempt")
    missing = replace(entry, evidence=replace(entry.evidence, local_config_entry_id="missing"))
    with pytest.raises(ValueError, match="stale"):
        await store.async_reconcile_hosted_ack(entry=missing, action="attempt")
    assert backend.saved[-1] is snapshot


async def test_ack_expiration_persists_and_is_terminal(monkeypatch):
    from custom_components.daylight_calendar_import import storage as storage_module
    store, backend, entry = await _accepted_journal(monkeypatch)
    expiry = datetime.fromisoformat(entry.evidence.source_expires_at)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return expiry if tz else expiry.replace(tzinfo=None)
    monkeypatch.setattr(storage_module, "datetime", Clock)
    expired = await store.async_reconcile_hosted_ack(entry=entry, action="expire")
    assert expired.ack_status == "expired"
    assert expired.evidence == entry.evidence
    assert expired.attempt_count == entry.attempt_count
    assert expired.last_attempt_at == entry.last_attempt_at
    assert backend.saved[-1]["hosted_delivery_journal"][0]["ack_status"] == "expired"
    with pytest.raises(ValueError, match="terminal"):
        await store.async_reconcile_hosted_ack(entry=expired, action="confirm")


async def test_ack_expiration_rejects_early_or_unexpected_evidence(monkeypatch):
    store, backend, entry = await _accepted_journal(monkeypatch)
    snapshot = backend.saved[-1]
    with pytest.raises(ValueError, match="not expired"):
        await store.async_reconcile_hosted_ack(entry=entry, action="expire")
    assert backend.saved[-1] is snapshot


async def test_ack_reconcile_rejects_attempt_and_lease_replacement_after_expiry(monkeypatch):
    from custom_components.daylight_calendar_import import storage as storage_module
    store, backend, entry = await _accepted_journal(monkeypatch)
    class Future(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.now(tz) + timedelta(days=2)
    monkeypatch.setattr(storage_module, "datetime", Future)
    before = backend.saved[-1]
    for action, fresh in (("attempt", None), ("replace_lease", replace(entry.evidence, lease_token="new-secret"))):
        with pytest.raises(ValueError, match="expired"):
            await store.async_reconcile_hosted_ack(entry=entry, action=action, new_evidence=fresh)
        assert backend.saved[-1] is before
