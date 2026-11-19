"""Manual retry, cancellation and late-response regression tests."""

import asyncio
from datetime import UTC, datetime

import pytest

from custom_components.daylight_calendar_import.storage import RetryJobConflict
from tests.test_storage import FakeStoreBackend, make_store, draft
from tests.test_retry_jobs import source_kwargs


async def test_retry_claim_is_single_owner_and_cancel_blocks_late_commit(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    claimed = await store.async_claim_retry(job_id)
    assert claimed.state == "processing" and claimed.attempts == 2
    with pytest.raises(RetryJobConflict, match="already processing"):
        await store.async_claim_retry(job_id)
    cancelled = await store.async_cancel_retry(job_id)
    assert cancelled.state == "cancelled"
    assert cancelled.claim is None
    with pytest.raises(RetryJobConflict, match="cancelled"):
        await store.async_add(
            source_text=claimed.source_text, events=[draft()], source_id=claimed.source_id,
            activity_id=job_id, retry_claim=claimed.claim,
        )
    assert store.get(job_id) is None
    assert store.get_retry_job(job_id).state == "cancelled"
    assert await store.async_cancel_retry(job_id) == cancelled
    resumed = await store.async_claim_retry(job_id)
    assert resumed.attempts == 3
    assert resumed.claim != claimed.claim
    outcome = await store.async_add(
        source_text=resumed.source_text, events=[draft()], source_id=resumed.source_id,
        activity_id=job_id, retry_claim=resumed.claim,
    )
    assert outcome.pending is not None
    assert store.get_retry_job(job_id) is None
    assert store.get(job_id) is not None


async def test_deleted_retry_source_blocks_late_response(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    claimed = store.get_retry_job(job_id)
    assert await store.async_delete_retry(job_id)
    assert not await store.async_delete_retry(job_id)
    with pytest.raises(RetryJobConflict, match="superseded"):
        await store.async_add(
            source_text=claimed.source_text, events=[draft()], source_id=claimed.source_id,
            activity_id=job_id, retry_claim=claimed.claim,
        )
    with pytest.raises(RetryJobConflict, match="not found"):
        await store.async_cancel_retry(job_id)
    with pytest.raises(RetryJobConflict, match="unavailable"):
        await store.async_claim_retry(job_id)


async def test_cancel_and_retry_are_atomic_when_storage_fails(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    original = store.get_retry_job(job_id)
    backend.save_error = OSError("disk failure")
    with pytest.raises(OSError):
        await store.async_cancel_retry(job_id)
    assert store.get_retry_job(job_id) == original
    with pytest.raises(OSError):
        await store.async_claim_retry(job_id)
    assert store.get_retry_job(job_id) == original
    with pytest.raises(OSError):
        await store.async_delete_retry(job_id)
    assert store.get_retry_job(job_id) == original


async def test_cancelled_attempt_survives_restart(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_cancel_retry(job_id)
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert restored.get_retry_job(job_id).state == "cancelled"
    # A late provider exception must not revive an explicitly cancelled job.
    await restored.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    assert restored.get_retry_job(job_id).state == "cancelled"

async def test_repeat_provider_failure_after_claim_remains_retriable(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    assert store.get_activity(job_id)["status"] == "failed"
    await store.async_claim_retry(job_id)
    assert store.get_activity(job_id)["status"] == "processing"
    await store.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    assert store.get_activity(job_id)["status"] == "failed"
    assert store.get_retry_job(job_id).state == "waiting"
    assert store.get_retry_job(job_id).attempts == 2


@pytest.mark.parametrize("failure_code", [None, "provider_unavailable"])
@pytest.mark.parametrize("change", ["cancel", "delete", "new_claim"])
async def test_old_parser_failure_cannot_override_cancel_delete_or_new_retry(
    monkeypatch, failure_code, change,
):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    original = store.get_retry_job(job_id)
    await store.async_cancel_retry(job_id)
    if change == "delete":
        assert await store.async_delete_retry(job_id)
    elif change == "new_claim":
        claimed = await store.async_claim_retry(job_id)
        assert claimed.claim != original.claim
    await store.async_record_parse_failure(
        job_id, provider_error_code=failure_code, retry_claim=original.claim,
    )
    latest = store.get_retry_job(job_id)
    if change == "delete":
        assert latest is None
    elif change == "cancel":
        assert latest.state == "cancelled"
    else:
        assert latest == claimed
        assert store.get_activity(job_id)["status"] == "processing"
        await store.async_record_parse_failure(
            job_id, provider_error_code=failure_code, retry_claim=claimed.claim,
        )
        assert store.get_retry_job(job_id).state == "waiting"
        assert store.get_activity(job_id)["status"] == "failed"


@pytest.mark.parametrize("state", ["waiting", "paused", "exhausted", "cancelled"])
async def test_retry_claim_restarts_each_eligible_state_and_clears_schedule(
    monkeypatch, state,
):
    """A manual retry must reclaim any stopped job, not just waiting jobs."""
    from dataclasses import replace

    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    original = store.get_retry_job(job_id)
    if state != "cancelled":
        await store.async_record_parse_failure(
            job_id, provider_error_code="provider_unavailable",
            retry_claim=original.claim,
        )
    else:
        await store.async_cancel_retry(job_id)
    stopped = replace(
        store.get_retry_job(job_id), state=state, claim=None,
        next_attempt_at=datetime(2030, 1, 1, tzinfo=UTC).isoformat(),
    )
    store._retry_jobs[job_id] = stopped
    claimed = await store.async_claim_retry(job_id)
    assert claimed.id == job_id
    assert claimed.state == "processing"
    assert claimed.claim is not None
    assert claimed.claim != original.claim
    assert claimed.next_attempt_at is None
    assert claimed.attempts == stopped.attempts + 1
    assert claimed.source_text == stopped.source_text
    assert store.get_activity(job_id)["status"] == "processing"
    assert store.get_activity(job_id)["transitions"][-1]["type"] == "processing"
    assert backend.saved[-1]["retry_jobs"][0]["claim"] == claimed.claim
    assert backend.saved[-1]["retry_jobs"][0]["next_attempt_at"] is None


async def test_pending_events_block_claim_without_modifying_retry_job(monkeypatch):
    """Even an otherwise eligible job cannot admit another pending draft."""
    from custom_components.daylight_calendar_import.storage import PendingImport

    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(
        job_id, provider_error_code="provider_timeout",
    )
    previous = store.get_retry_job(job_id)
    store._items[job_id] = PendingImport.create(
        source_text="existing review", events=[draft()],
    )
    before = backend.save_attempts
    with pytest.raises(RetryJobConflict, match="awaiting review"):
        await store.async_claim_retry(job_id)
    assert store.get_retry_job(job_id) == previous
    assert backend.save_attempts == before


async def test_cancel_clears_schedule_idempotently_and_delete_is_scoped(monkeypatch):
    """Cancel keeps input, whereas delete discards only the selected input."""
    from dataclasses import replace

    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    first = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(
        first, provider_error_code="provider_rate_limited",
    )
    store._retry_jobs[first] = replace(
        store.get_retry_job(first),
        next_attempt_at=datetime(2030, 1, 1, tzinfo=UTC).isoformat(),
    )
    second_kwargs = {
        **source_kwargs(),
        "retry_source_id": "manual-two",
        "retry_source_text": "Second private event",
    }
    second = await store.async_begin_submission(**second_kwargs)
    second_job = store.get_retry_job(second)
    cancelled = await store.async_cancel_retry(first)
    assert cancelled.state == "cancelled"
    assert cancelled.next_attempt_at is None
    assert cancelled.claim is None
    assert cancelled.source_text == source_kwargs()["retry_source_text"]
    previous_saves = backend.save_attempts
    assert await store.async_cancel_retry(first) == cancelled
    assert backend.save_attempts == previous_saves
    assert store.get_retry_job(second) == second_job

    assert await store.async_delete_retry(first)
    assert store.get_retry_job(first) is None
    assert store.get_retry_job(second) == second_job
    assert [job.id for job in store.list_retry_jobs()] == [second]
    assert [raw["id"] for raw in backend.saved[-1]["retry_jobs"]] == [second]
