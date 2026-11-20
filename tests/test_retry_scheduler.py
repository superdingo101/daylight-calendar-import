"""Retry policy, scheduling, restart and retention regression tests."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from custom_components.daylight_calendar_import import retry_scheduler
from custom_components.daylight_calendar_import.retry_jobs import RetryJob
from custom_components.daylight_calendar_import.retry_settings import (
    RetrySettings, RetrySettingsError, next_retry_at, normalize_retry_settings,
)
from custom_components.daylight_calendar_import.retry_scheduler import RetryScheduler
from custom_components.daylight_calendar_import.storage import RetryJobConflict
from tests.test_retry_jobs import source_kwargs
from tests.test_storage import FakeStoreBackend, make_store


def make_job():
    return RetryJob.create(
        activity_id="retry-job", source_text="family dinner", source_id="source",
        received_at=datetime(2026, 11, 18, tzinfo=UTC),
        provider_entity_id="ai_task.gemini", calendar_entity="calendar.family",
        owner_user_id="owner",
    )


def test_default_retry_policy_is_off_and_bounded():
    default = RetrySettings()
    assert default.as_dict() == {
        "enabled": False, "initial_delay_minutes": 15,
        "max_automatic_retries": 3, "retention_days": 7,
    }
    assert normalize_retry_settings({}) == default
    assert normalize_retry_settings(default.as_dict()) == default
    assert normalize_retry_settings({"enabled": True, "initial_delay_minutes": 60,
        "max_automatic_retries": 5, "retention_days": 30}).enabled


@pytest.mark.parametrize(("settings", "message"), [
    (None, "Invalid retry settings"),
    ("abc", "Invalid retry settings"),
    ({"unexpected": True}, "Invalid retry settings"),
    ({"enabled": 1}, "Choose a valid retry setting"),
    ({"enabled": "true"}, "Choose a valid retry setting"),
    ({"initial_delay_minutes": 5}, "Choose a valid retry setting"),
    ({"initial_delay_minutes": True}, "Choose a valid retry setting"),
    ({"initial_delay_minutes": "15"}, "Choose a valid retry setting"),
    ({"max_automatic_retries": 0}, "between one and five"),
    ({"max_automatic_retries": 6}, "between one and five"),
    ({"max_automatic_retries": True}, "between one and five"),
    ({"retention_days": 0}, "retention period"),
    ({"retention_days": 31}, "retention period"),
    ({"retention_days": False}, "retention period"),
])
def test_retry_policy_rejects_invalid_configuration(settings, message):
    with pytest.raises(RetrySettingsError, match=message):
        normalize_retry_settings(settings)


def test_retry_schedule_is_explicit_exponential_and_preserves_timezone():
    now = datetime(2026, 11, 18, 20, 0, tzinfo=UTC)
    job = make_job()
    policy = RetrySettings(enabled=True, initial_delay_minutes=15)
    assert next_retry_at(job, policy, now) is None  # not failed yet
    failed = job.after_failure("provider_unavailable")
    assert next_retry_at(failed, RetrySettings(), now) is None
    first = datetime.fromisoformat(next_retry_at(failed, policy, now))
    assert now + timedelta(minutes=15) <= first < now + timedelta(minutes=18)
    assert next_retry_at(failed, policy, now) == first.isoformat()
    second = datetime.fromisoformat(next_retry_at(
        replace(failed, automatic_attempts=1), policy, now))
    assert now + timedelta(minutes=30) <= second < now + timedelta(minutes=36)
    assert next_retry_at(replace(failed, automatic_attempts=3), policy, now) is None
    limited = datetime.fromisoformat(next_retry_at(
        replace(failed, last_failure_code="provider_rate_limited"), policy, now))
    assert limited >= now + timedelta(minutes=30)
    assert next_retry_at(failed.after_failure("provider_authentication"), policy, now) is None
    with pytest.raises(ValueError, match="timezone"):
        next_retry_at(failed, policy, datetime(2026, 11, 18))


async def test_auto_retry_is_persisted_only_after_due_and_budgeted(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    policy = RetrySettings(enabled=True, initial_delay_minutes=15, max_automatic_retries=2)
    store.retry_preferences = lambda: policy
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    job = store.get_retry_job(job_id)
    assert job.state == "waiting" and job.next_attempt_at
    with pytest.raises(RetryJobConflict, match="not due"):
        await store.async_claim_retry(job_id, automatic=True)
    # Simulate the due time without waiting; the durable timestamp is authoritative.
    from dataclasses import replace
    store._retry_jobs[job_id] = replace(job, next_attempt_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    active = await store.async_claim_retry(job_id, automatic=True)
    assert active.automatic_attempts == 1
    assert active.attempts == 2
    assert store.get_activity(job_id)["status"] == "processing"
    await store.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    scheduled = store.get_retry_job(job_id)
    assert scheduled.state == "waiting" and scheduled.next_attempt_at
    store._retry_jobs[job_id] = replace(scheduled, next_attempt_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    await store.async_claim_retry(job_id, automatic=True)
    await store.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    assert store.get_retry_job(job_id).state == "exhausted"
    assert store.get_retry_job(job_id).next_attempt_at is None


async def test_rearm_saved_disabled_jobs_when_enabled(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_timeout")
    assert store.get_retry_job(job_id).next_attempt_at is None
    await store.async_rearm_retry_jobs()
    assert store.get_retry_job(job_id).next_attempt_at is None
    store.retry_preferences = lambda: RetrySettings(enabled=True)
    await store.async_rearm_retry_jobs()
    first = store.get_retry_job(job_id).next_attempt_at
    assert first is not None
    await store.async_rearm_retry_jobs()
    assert store.get_retry_job(job_id).next_attempt_at == first


async def test_expired_source_retention_never_drops_processing_work(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    store.retry_preferences = lambda: RetrySettings(enabled=False, retention_days=1)
    job_id = await store.async_begin_submission(**source_kwargs())
    original = store.get_retry_job(job_id)
    store._retry_jobs[job_id] = replace(
        original, state="waiting", claim=None, received_at="2026-01-01T00:00:00+00:00",
    )
    await store.async_apply_retention()
    assert store.get_retry_job(job_id) is None
    job_id2 = await store.async_begin_submission(**source_kwargs())
    processing = store.get_retry_job(job_id2)
    store._retry_jobs[job_id2] = replace(processing, received_at="2026-01-01T00:00:00+00:00")
    await store.async_apply_retention()
    assert store.get_retry_job(job_id2) is not None


def scheduler_hass():
    tasks = []
    def create(coro, name, eager_start=False):
        task = asyncio.create_task(coro, name=name)
        tasks.append(task)
        return task
    return SimpleNamespace(async_create_task=create, tasks=tasks)


async def test_scheduler_runs_one_due_job_without_overlapping(monkeypatch):
    unsub = Mock()
    callbacks = []
    monkeypatch.setattr(retry_scheduler, "async_track_time_interval",
                        lambda _hass, cb, _interval: callbacks.append(cb) or unsub)
    hass = scheduler_hass()
    store = SimpleNamespace(accepting_services=True, list_retry_jobs=Mock(return_value=()))
    run = AsyncMock()
    policy = lambda: RetrySettings(enabled=True)
    scheduler = RetryScheduler(hass, store, policy, run)
    scheduler.start()
    scheduler.start()
    assert len(callbacks) == 1
    assert len(hass.tasks) == 0
    due = replace(make_job().after_failure("provider_unavailable"),
                  next_attempt_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    later = replace(due, id="later", next_attempt_at=(datetime.now(UTC) + timedelta(minutes=10)).isoformat())
    store.list_retry_jobs.return_value = (later, due, replace(due, id="due2"))
    callbacks[0](datetime.now(UTC))
    assert len(hass.tasks) == 1
    scheduler.kick()
    assert len(hass.tasks) == 1
    await hass.tasks[0]
    run.assert_awaited_once_with(due.id)
    await scheduler.async_stop()
    unsub.assert_called_once()
    scheduler.kick()
    assert len(hass.tasks) == 1
    await scheduler.async_stop()


async def test_scheduler_rejects_not_due_paused_or_disabled_and_sanitizes_errors(monkeypatch):
    unsub = Mock()
    monkeypatch.setattr(retry_scheduler, "async_track_time_interval",
                        lambda *_args: unsub)
    hass = scheduler_hass()
    due = replace(make_job().after_failure("provider_unavailable"),
                  next_attempt_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    store = SimpleNamespace(accepting_services=False, list_retry_jobs=Mock(return_value=(due,)))
    run = AsyncMock(side_effect=RuntimeError("private provider response"))
    settings = [RetrySettings(enabled=False)]
    scheduler = RetryScheduler(hass, store, lambda: settings[0], run)
    scheduler.start()
    scheduler.kick()
    assert not hass.tasks
    settings[0] = RetrySettings(enabled=True)
    scheduler.kick()
    assert not hass.tasks
    store.accepting_services = True
    scheduler.kick()
    await hass.tasks[0]
    run.assert_awaited_once_with(due.id)
    store.list_retry_jobs.return_value = (replace(due, state="paused"),)
    scheduler.kick()
    assert len(hass.tasks) == 1
    await scheduler.async_stop()


async def test_scheduler_stop_cancels_inflight_and_cleanup(monkeypatch):
    unsub = Mock()
    monkeypatch.setattr(retry_scheduler, "async_track_time_interval",
                        lambda *_args: unsub)
    hass = scheduler_hass()
    due = replace(make_job().after_failure("provider_unavailable"),
                  next_attempt_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    store = SimpleNamespace(accepting_services=True, list_retry_jobs=lambda: (due,))
    started = asyncio.Event()
    async def execute(_job_id):
        started.set()
        await asyncio.sleep(3600)
    scheduler = RetryScheduler(hass, store, lambda: RetrySettings(enabled=True), execute)
    scheduler.start()
    await started.wait()
    await scheduler.async_stop()
    assert hass.tasks[0].cancelled()
    unsub.assert_called_once()

async def test_hourly_retention_runs_even_when_background_retries_disabled(monkeypatch):
    unsub = Mock()
    monkeypatch.setattr(retry_scheduler, "async_track_time_interval",
                        lambda *_args: unsub)
    hass = scheduler_hass()
    store = SimpleNamespace(
        accepting_services=True, list_retry_jobs=lambda: (),
        async_apply_retention=AsyncMock(),
    )
    clean = AsyncMock()
    scheduler = RetryScheduler(hass, store, lambda: RetrySettings(enabled=False),
                               AsyncMock(), cleanup=clean)
    scheduler.start()
    assert hass.tasks == []
    scheduler._last_retention = datetime.now(UTC) - timedelta(hours=2)
    scheduler.kick()
    await hass.tasks[-1]
    store.async_apply_retention.assert_awaited_once()
    clean.assert_awaited_once()
    count = len(hass.tasks)
    scheduler.kick()
    assert len(hass.tasks) == count
    await scheduler.async_stop()


async def test_retention_failure_does_not_expose_exception_and_retries_next_tick(monkeypatch):
    monkeypatch.setattr(retry_scheduler, "async_track_time_interval",
                        lambda *_args: Mock())
    hass = scheduler_hass()
    store = SimpleNamespace(
        accepting_services=True, list_retry_jobs=lambda: (),
        async_apply_retention=AsyncMock(side_effect=OSError("private source data")),
    )
    scheduler = RetryScheduler(hass, store, lambda: RetrySettings(), AsyncMock())
    scheduler.start()
    scheduler._last_retention = datetime.now(UTC) - timedelta(hours=2)
    scheduler.kick()
    await hass.tasks[-1]
    assert store.async_apply_retention.await_count == 1
    scheduler.kick()
    await hass.tasks[-1]
    assert store.async_apply_retention.await_count == 2
    await scheduler.async_stop()


async def test_scheduler_cancellation_propagates_without_resuming_cleanup(monkeypatch):
    monkeypatch.setattr(retry_scheduler, "async_track_time_interval",
                        lambda *_args: Mock())
    hass = scheduler_hass()
    store = SimpleNamespace(
        accepting_services=True, list_retry_jobs=lambda: (),
        async_apply_retention=AsyncMock(side_effect=asyncio.CancelledError()),
    )
    scheduler = RetryScheduler(hass, store, lambda: RetrySettings(), AsyncMock())
    scheduler.start()
    scheduler._last_retention = datetime.now(UTC) - timedelta(hours=2)
    scheduler.kick()
    with pytest.raises(asyncio.CancelledError):
        await hass.tasks[-1]
    await scheduler.async_stop()


async def test_retry_delay_saturates_at_one_day():
    policy = RetrySettings(enabled=True, max_automatic_retries=10, initial_delay_minutes=60)
    failed = replace(make_job().after_failure("provider_timeout"), automatic_attempts=6,
                     attempts=7)
    now = datetime(2026, 11, 18, tzinfo=UTC)
    due = datetime.fromisoformat(next_retry_at(failed, policy, now))
    assert timedelta(hours=24) <= due - now < timedelta(hours=29)


async def test_lowered_retry_budget_retires_an_already_due_job_atomically(monkeypatch):
    """A stale waiting job cannot monopolize the queue after policy changes."""
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    policy = [RetrySettings(enabled=True, max_automatic_retries=3)]
    store.retry_preferences = lambda: policy[0]
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_timeout")
    first = store.get_retry_job(job_id)
    store._retry_jobs[job_id] = replace(
        first, next_attempt_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat(),
    )
    claimed = await store.async_claim_retry(job_id, automatic=True)
    assert claimed.automatic_attempts == 1
    await store.async_record_parse_failure(job_id, provider_error_code="provider_timeout")
    assert store.get_retry_job(job_id).state == "waiting"
    assert store.get_retry_job(job_id).next_attempt_at is not None

    policy[0] = RetrySettings(enabled=True, max_automatic_retries=1)
    await store.async_rearm_retry_jobs()
    exhausted = store.get_retry_job(job_id)
    assert exhausted.state == "exhausted"
    assert exhausted.automatic_attempts == 1
    assert exhausted.next_attempt_at is None
    assert [record["state"] for record in backend.saved[-1]["retry_jobs"] if record["id"] == job_id] == ["exhausted"]
    with pytest.raises(RetryJobConflict, match="Automatic retry"):
        await store.async_claim_retry(job_id, automatic=True)
    # A reload/rearm must never revive a retired job just because the budget
    # changes again. Users can still explicitly request a manual retry.
    policy[0] = RetrySettings(enabled=True, max_automatic_retries=5)
    await store.async_rearm_retry_jobs()
    assert store.get_retry_job(job_id).state == "exhausted"


async def test_scheduler_skips_stale_budget_and_non_claimed_sources(monkeypatch):
    unsub = Mock()
    monkeypatch.setattr(retry_scheduler, "async_track_time_interval",
                        lambda *_args: unsub)
    now = datetime.now(UTC) - timedelta(minutes=1)
    failed = replace(make_job().after_failure("provider_unavailable"),
                     next_attempt_at=now.isoformat())
    stale = replace(failed, id="stale", attempts=3, automatic_attempts=2)
    first = replace(failed, id="rejected")
    second = replace(failed, id="accepted")
    store = SimpleNamespace(
        accepting_services=True, list_retry_jobs=lambda: (stale, first, second),
    )
    execute = AsyncMock(side_effect=[
        {"status": "not_due", "job_id": first.id},
        {"status": "saved_for_retry", "job_id": second.id},
    ])
    hass = scheduler_hass()
    scheduler = RetryScheduler(
        hass, store, lambda: RetrySettings(enabled=True, max_automatic_retries=2),
        execute,
    )
    scheduler.start()
    await hass.tasks[-1]
    assert [call.args[0] for call in execute.await_args_list] == [
        first.id, second.id,
    ]
    await scheduler.async_stop()
    unsub.assert_called_once()


async def test_scheduler_restarts_its_subscription_after_explicit_stop(monkeypatch):
    unsubscribers = []
    monkeypatch.setattr(
        retry_scheduler, "async_track_time_interval",
        lambda *_args: unsubscribers.append(Mock()) or unsubscribers[-1],
    )
    due = replace(make_job().after_failure("provider_unavailable"),
                  next_attempt_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    store = SimpleNamespace(accepting_services=True, list_retry_jobs=lambda: (due,))
    execute = AsyncMock(return_value={"status": "saved_for_retry"})
    hass = scheduler_hass()
    scheduler = RetryScheduler(hass, store, lambda: RetrySettings(enabled=True), execute)
    scheduler.start()
    await hass.tasks[-1]
    await scheduler.async_stop()
    assert scheduler._stopped is True
    assert unsubscribers[0].call_count == 1
    scheduler.kick()
    assert len(hass.tasks) == 1
    scheduler.start()
    assert scheduler._stopped is False
    assert len(unsubscribers) == 2
    await hass.tasks[-1]
    assert execute.await_count == 2
    await scheduler.async_stop()
    assert unsubscribers[1].call_count == 1


async def test_retention_without_cleanup_hook_handles_empty_queue_and_stopped_runtime(monkeypatch):
    """Cleanup and stopped-task branches are safe even without a user callback."""
    unsubscribe = Mock()
    monkeypatch.setattr(
        retry_scheduler, "async_track_time_interval", lambda *_args: unsubscribe,
    )
    hass = scheduler_hass()
    store = SimpleNamespace(
        accepting_services=True, list_retry_jobs=Mock(return_value=()),
        async_apply_retention=AsyncMock(),
    )
    execute = AsyncMock()
    scheduler = RetryScheduler(hass, store, lambda: RetrySettings(enabled=True), execute)
    scheduler.start()
    assert hass.tasks == []
    scheduler._last_retention = datetime.now(UTC) - timedelta(hours=2)
    scheduler.kick()
    assert len(hass.tasks) == 1
    await hass.tasks[0]
    store.async_apply_retention.assert_awaited_once_with()
    execute.assert_not_awaited()
    assert datetime.now(UTC) - scheduler._last_retention < timedelta(seconds=30)
    await scheduler.async_stop()
    unsubscribe.assert_called_once_with()
    await scheduler._async_run_one()
    store.async_apply_retention.assert_awaited_once_with()
    execute.assert_not_awaited()


async def test_rearm_does_not_schedule_a_nonretryable_legacy_waiting_failure(monkeypatch):
    """Opt-in must never promote an authentication failure into automatic parsing."""
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_authentication")
    stopped = store.get_retry_job(job_id)
    assert stopped.state == "paused"
    legacy_waiting = replace(stopped, state="waiting", next_attempt_at=None)
    store._retry_jobs[job_id] = legacy_waiting
    store.retry_preferences = lambda: RetrySettings(enabled=True)
    saved_before = len(backend.saved)
    await store.async_rearm_retry_jobs()
    assert store.get_retry_job(job_id) == legacy_waiting
    assert store.get_retry_job(job_id).next_attempt_at is None
    assert len(backend.saved) == saved_before


async def test_retry_attempt_ceiling_rejects_before_claim_and_preserves_storage(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    original = store.get_retry_job(job_id)
    ceiling = replace(original, state="waiting", claim=None, attempts=1000)
    store._retry_jobs[job_id] = ceiling
    saved_before = len(backend.saved)
    with pytest.raises(RetryJobConflict, match="Retry attempt limit reached"):
        await store.async_claim_retry(job_id)
    assert store.get_retry_job(job_id) == ceiling
    assert len(backend.saved) == saved_before


async def test_pause_missing_and_cancelled_retry_is_noop_without_write(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    saved_before = len(backend.saved)
    assert await store.async_pause_retry("missing-retry") is None
    assert len(backend.saved) == saved_before

    job_id = await store.async_begin_submission(**source_kwargs())
    cancelled = await store.async_cancel_retry(job_id)
    assert cancelled.state == "cancelled"
    saved_before = len(backend.saved)
    assert await store.async_pause_retry(job_id) == cancelled
    assert store.get_retry_job(job_id) == cancelled
    assert len(backend.saved) == saved_before
