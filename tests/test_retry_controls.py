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
