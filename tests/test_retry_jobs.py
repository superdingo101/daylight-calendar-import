"""Durable text retry admission, restart recovery, dedupe and source privacy."""

from datetime import UTC, datetime

import pytest

from custom_components.daylight_calendar_import.retry_jobs import RetryJob
from custom_components.daylight_calendar_import.storage import _decode_storage_data
from tests.test_storage import FakeStoreBackend, draft, make_store


def source_kwargs():
    return {
        "source_kind": "manual_text", "source_title": None,
        "received_at": datetime(2026, 11, 18, 20, 35, tzinfo=UTC),
        "retry_source_text": "Christmas dinner at 6 PM on December 25, 2027",
        "retry_source_id": "manual-one", "retry_provider": "ai_task.gemini",
        "retry_calendar": "calendar.family", "retry_owner": "user-1",
    }


async def test_private_source_is_saved_before_provider_and_503_survives_restart(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    activity = await store.async_begin_submission(**source_kwargs())
    job = store.get_retry_job(activity)
    assert job is not None and job.state == "processing" and job.attempts == 1
    assert backend.saved[-1]["retry_jobs"][0]["source_text"] == source_kwargs()["retry_source_text"]
    assert store.is_source_duplicate("manual-one")
    assert "source_text" not in job.summary()

    await store.async_record_parse_failure(activity, provider_error_code="provider_unavailable")
    assert store.get_retry_job(activity).state == "waiting"
    assert store.get_retry_job(activity).last_failure_code == "provider_unavailable"

    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert restored.get_retry_job(activity) == store.get_retry_job(activity)
    assert restored.is_source_duplicate("manual-one")


async def test_interrupted_retry_processing_is_recovered_without_losing_source(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    activity = await store.async_begin_submission(**source_kwargs())
    backend.load_result = backend.saved[-1]
    restarted = make_store(monkeypatch, backend)
    await restarted.async_load()
    assert restarted.get_retry_job(activity).state == "waiting"
    assert restarted.get_retry_job(activity).claim is None
    assert restarted.get_retry_job(activity).source_text == source_kwargs()["retry_source_text"]


@pytest.mark.parametrize("events", [(), (draft(),)])
async def test_completed_parse_removes_private_retry_source_in_same_transaction(monkeypatch, events):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    activity = await store.async_begin_submission(**source_kwargs())
    result = await store.async_add(
        source_text=source_kwargs()["retry_source_text"],
        source_id="manual-one", calendar_entity="calendar.family",
        events=events, activity_id=activity,
    )
    assert (result.pending is not None) is bool(events)
    assert store.get_retry_job(activity) is None
    assert "retry_jobs" not in backend.saved[-1]
    assert store.is_source_duplicate("manual-one") is bool(events)
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert restored.get_retry_job(activity) is None


async def test_failed_storage_commit_keeps_private_source_and_identity(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    activity = await store.async_begin_submission(**source_kwargs())
    backend.save_error = OSError("disk full")
    with pytest.raises(OSError, match="disk full"):
        await store.async_add(source_text="Christmas dinner", source_id="manual-one",
                              events=[draft()], activity_id=activity)
    assert store.get_retry_job(activity) is not None
    assert store.is_source_duplicate("manual-one")


def test_bad_retry_record_fails_closed():
    valid = RetryJob.create(
        activity_id="activity", source_text="private event", source_id="source",
        received_at=datetime.now(UTC), provider_entity_id="ai_task.gemini",
        calendar_entity="calendar.family", owner_user_id="user-1",
    ).as_dict()
    for field, value in (("state", "approved"), ("attempts", True),
                         ("received_at", "2026-11-18T20:35:00"),
                         ("source_kind", "email"), ("source_text", None)):
        corrupt = {**valid, field: value}
        with pytest.raises(ValueError):
            RetryJob.from_dict(corrupt)
    with pytest.raises(ValueError, match="orphaned retry job"):
        _decode_storage_data({"items": [], "retry_jobs": [valid]})

@pytest.mark.parametrize(("overrides", "message"), [
    ({"unsupported": "field"}, "fields"),
    ({"id": ""}, "identity"),
    ({"source_id": 3}, "identity"),
    ({"provider_entity_id": "x" * 2049}, "identity"),
    ({"received_at": "not a timestamp"}, "timestamp"),
    ({"next_attempt_at": "not a timestamp"}, "timestamp"),
    ({"next_attempt_at": "2026-11-18T00:00:00"}, "timezone"),
    ({"claim": None}, "claim"),
    ({"claim": 42}, "claim"),
    ({"claim": "x" * 129}, "claim"),
    ({"last_failure_code": "unsupported"}, "failure"),
    ({"last_failure_code": 503}, "failure"),
    ({"owner_user_id": 9}, "metadata"),
    ({"source_title": "x" * 2049}, "metadata"),
    ({"routing_unresolved": 1}, "flags"),
    ({"reimport_duplicates": 0}, "flags"),
    ({"attachment_size": 0}, "size"),
    ({"attachment_size": True}, "size"),
    ({"attachment_size": 10 * 1024 * 1024 + 1}, "size"),
    ({"source_text": " "}, "payload"),
    ({"attachment_path": "/tmp/source"}, "payload"),
    ({"source_kind": "pdf", "attachment_path": "/tmp/source"}, "metadata"),
    ({"source_kind": "image", "attachment_sha256": "abcd"}, "metadata"),
    ({"source_kind": "image", "attachment_media_type": "image/png"}, "metadata"),
    ({"source_kind": "pdf", "attachment_size": 100}, "metadata"),
    ({"state": "processing", "attempts": -1}, "lifecycle"),
    ({"state": "waiting", "attempts": 1001}, "lifecycle"),
])
def test_retry_snapshot_rejects_corrupt_individual_fields(overrides, message):
    raw = RetryJob.create(
        activity_id="activity", source_text="private event", source_id="source",
        received_at=datetime.now(UTC), provider_entity_id="ai_task.gemini",
        calendar_entity="calendar.family", owner_user_id="user-1",
    ).as_dict()
    raw.update(overrides)
    with pytest.raises(ValueError, match=message):
        RetryJob.from_dict(raw)


def test_retry_snapshot_rejects_wrong_container_and_accepts_valid_file_metadata():
    with pytest.raises(ValueError, match="fields"):
        RetryJob.from_dict("not a dict")
    valid = RetryJob.create(
        activity_id="pdf", source_text="private extracted PDF text", source_id="source",
        received_at=datetime.now(UTC), provider_entity_id="ai_task.gemini",
        calendar_entity="calendar.family", owner_user_id=None,
        source_kind="pdf", attachment_path="/tmp/spool/file",
        attachment_sha256="a" * 64, attachment_media_type="application/pdf",
        attachment_size=128,
    )
    assert RetryJob.from_dict(valid.as_dict()) == valid


async def test_retry_storage_admission_without_drain_and_invalid_configuration(monkeypatch):
    """Until retry/cancel exists, retained failures must never permanently block new imports."""
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    with pytest.raises(ValueError, match="Missing retry source configuration"):
        await store.async_begin_submission(
            source_kind="manual_text", source_title=None, received_at=datetime.now(UTC),
            retry_source_text="private",
        )

    for index in range(100):
        job_id = await store.async_begin_submission(
            **{**source_kwargs(), "retry_source_id": f"source-{index}"}
        )
        await store.async_record_parse_failure(
            job_id, provider_error_code="provider_unavailable"
        )
    assert len(store.list_retry_jobs()) == 100

    admitted = await store.async_begin_submission(**source_kwargs())
    assert len(store.list_retry_jobs()) == 101
    assert store.get_retry_job(admitted).source_id == "manual-one"
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert len(restored.list_retry_jobs()) == 101
    assert restored.get_retry_job(admitted).state == "waiting"


# These contracts are intentionally explicit: they protect public snapshot fields,
# accepted provider error codes, and inclusive validation limits against mutation.
def valid_retry_raw():
    return RetryJob.create(
        activity_id="activity", source_text="private event", source_id="source",
        received_at=datetime(2026, 11, 18, 20, 35, tzinfo=UTC),
        provider_entity_id="ai_task.gemini", calendar_entity="calendar.family",
        owner_user_id="user-1",
    ).as_dict()


def test_retry_job_create_defaults_and_processing_claim_are_valid():
    from uuid import UUID

    job = RetryJob.create(
        activity_id="activity", source_text="private event", source_id="source",
        received_at=datetime(2026, 11, 18, 20, 35, tzinfo=UTC),
        provider_entity_id="ai_task.gemini", calendar_entity="calendar.family",
        owner_user_id="user-1",
    )
    assert job.routing_unresolved is False
    assert job.reimport_duplicates is False
    assert job.state == "processing"
    assert job.attempts == 1
    assert job.claim is not None
    assert UUID(job.claim).version == 4
    assert str(UUID(job.claim)) == job.claim


def test_retry_job_summary_is_exact_and_excludes_private_details():
    job = RetryJob.from_dict({
        **valid_retry_raw(),
        "source_kind": "image",
        "source_title": "School flyer",
        "source_text": "private description",
        "state": "waiting",
        "claim": None,
        "attempts": 4,
        "last_failure_code": "provider_timeout",
        "next_attempt_at": "2026-11-19T01:00:00+00:00",
        "attachment_path": "/private/flyer",
        "attachment_sha256": "a" * 64,
        "attachment_media_type": "image/png",
        "attachment_size": 128,
    })
    assert job.summary() == {
        "id": "activity",
        "source_kind": "image",
        "source_title": "School flyer",
        "received_at": "2026-11-18T20:35:00+00:00",
        "state": "waiting",
        "attempts": 4,
        "next_attempt_at": "2026-11-19T01:00:00+00:00",
        "last_failure_code": "provider_timeout",
    }


@pytest.mark.parametrize("code", [
    "provider_timeout",
    "provider_rate_limited",
    "provider_unavailable",
    "provider_error",
    "provider_authentication",
    "provider_invalid_response",
])
def test_retry_job_accepts_all_documented_provider_failure_codes(code):
    job = RetryJob.from_dict({
        **valid_retry_raw(),
        "state": "paused",
        "claim": None,
        "last_failure_code": code,
    })
    assert job.last_failure_code == code
    assert RetryJob.from_dict(job.as_dict()) == job


@pytest.mark.parametrize(("overrides", "error"), [
    ({"unknown": True}, "invalid retry job fields"),
    ({"id": ""}, "invalid retry job identity"),
    ({"source_text": None}, "invalid retry source text"),
    ({"received_at": "broken"}, "invalid retry timestamp"),
    ({"received_at": "2026-11-18T20:35:00"}, "retry timestamp must be timezone aware"),
    ({"state": "approved"}, "invalid retry lifecycle"),
    ({"claim": None}, "processing retry must have a claim"),
    ({"claim": 123}, "invalid retry claim"),
    ({"last_failure_code": "missing"}, "invalid retry failure"),
    ({"source_kind": "email"}, "unsupported retry source kind"),
    ({"owner_user_id": 12}, "invalid retry metadata"),
    ({"routing_unresolved": "yes"}, "invalid retry flags"),
    ({"attachment_size": 0}, "invalid retry attachment size"),
    ({"source_text": " "}, "invalid text retry payload"),
    ({"source_kind": "pdf", "attachment_path": "/private/file"},
     "invalid attachment retry metadata"),
])
def test_retry_snapshot_reports_precise_validation_error(overrides, error):
    with pytest.raises(ValueError) as exc:
        RetryJob.from_dict({**valid_retry_raw(), **overrides})
    assert str(exc.value) == error


@pytest.mark.parametrize(("overrides", "expected"), [
    ({"source_id": "s" * 2048}, "source_id"),
    ({"source_text": "x" * 1_000_000}, "source_text"),
    ({"claim": "c" * 128}, "claim"),
    ({"state": "paused", "claim": None, "attempts": 1000}, "attempts"),
    ({"owner_user_id": "o" * 2048}, "owner_user_id"),
    ({"attachment_path": "p" * 2048, "source_kind": "pdf",
      "attachment_sha256": "a" * 64, "attachment_media_type": "application/pdf",
      "attachment_size": 1}, "attachment_size"),
    ({"attachment_path": "/private/file", "source_kind": "pdf",
      "attachment_sha256": "a" * 64, "attachment_media_type": "application/pdf",
      "attachment_size": 10 * 1024 * 1024}, "attachment_size"),
])
def test_retry_snapshot_accepts_inclusive_limits(overrides, expected):
    job = RetryJob.from_dict({**valid_retry_raw(), **overrides})
    assert getattr(job, expected) == overrides[expected]


@pytest.mark.parametrize(("code", "state"), [
    ("provider_unavailable", "waiting"),
    ("provider_timeout", "waiting"),
    ("provider_rate_limited", "waiting"),
    ("provider_error", "paused"),
    ("provider_authentication", "paused"),
    ("provider_invalid_response", "paused"),
    (None, "paused"),
])
def test_retry_failure_releases_claim_and_preserves_requested_delay(code, state):
    original = RetryJob.from_dict(valid_retry_raw())
    scheduled = "2026-11-19T03:45:00+00:00"
    failed = original.after_failure(code, next_attempt_at=scheduled)
    assert failed == RetryJob.from_dict({
        **original.as_dict(),
        "state": state,
        "claim": None,
        "last_failure_code": code,
        "next_attempt_at": scheduled,
    })
    assert original.state == "processing"
    assert original.claim is not None


@pytest.mark.parametrize("invalid", [
    {"retry_provider": None},
    {"retry_calendar": None},
    {"source_kind": "pdf"},
])
async def test_retry_submission_rejects_each_missing_or_unsupported_config(monkeypatch, invalid):
    store = make_store(monkeypatch, FakeStoreBackend())
    with pytest.raises(ValueError) as exc:
        await store.async_begin_submission(**{**source_kwargs(), **invalid})
    assert str(exc.value) == "Missing retry source configuration"
    assert store.list_retry_jobs() == ()
    assert store.list_activity() == ()


async def test_retry_submission_preserves_owner_and_reimport_preference(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    activity = await store.async_begin_submission(**{
        **source_kwargs(), "retry_reimport_duplicates": True,
    })
    job = store.get_retry_job(activity)
    assert job is not None
    assert job.owner_user_id == "user-1"
    assert job.reimport_duplicates is True
    assert job.routing_unresolved is False


async def test_retry_submission_defaults_to_no_duplicate_reimport(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    activity = await store.async_begin_submission(**source_kwargs())
    assert store.get_retry_job(activity).reimport_duplicates is False


async def test_retry_failure_default_guidance_is_stable(monkeypatch):
    """Protect the fallback advice when no provider-specific guidance was supplied."""
    store = make_store(monkeypatch, FakeStoreBackend())
    activity = await store.async_begin_submission(**source_kwargs())
    async with store._lock:
        await store._async_record_parse_failure_locked(activity)
    record = store.get_activity(activity)
    assert record is not None
    assert record["status"] == "failed"
    assert record["guidance"] == (
        "Parsing failed. Check the configured AI Task and submit the source again."
    )


async def test_retry_failure_only_tags_its_source_and_clears_its_release(monkeypatch):
    """A provider failure must not contaminate concurrent source activity."""
    store = make_store(monkeypatch, FakeStoreBackend())
    failed_id = await store.async_begin_submission(**source_kwargs())
    other_id = await store.async_begin_submission(**{
        **source_kwargs(), "retry_source_id": "manual-two",
    })
    store._source_claim_releases.update((failed_id, other_id))
    await store.async_record_parse_failure(
        failed_id, provider_error_code="provider_unavailable",
    )
    failed = store.get_activity(failed_id)
    other = store.get_activity(other_id)
    assert failed is not None and other is not None
    assert failed["status"] == "failed"
    assert failed["failure_code"] == "provider_unavailable"
    assert other["status"] == "processing"
    assert "failure_code" not in other
    assert failed_id not in store._source_claim_releases
    assert other_id in store._source_claim_releases
