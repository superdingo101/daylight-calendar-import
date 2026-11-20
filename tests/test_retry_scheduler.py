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


@pytest.mark.parametrize(("enabled", "delay", "attempts", "days"), [
    (False, 15, 1, 1), (True, 30, 5, 30),
    (True, 60, 3, 14), (True, 15, 2, 7),
])
def test_retry_policy_all_allowed_boundary_values_round_trip(enabled, delay, attempts, days):
    """The documented option boundaries are valid, including 1 and 30."""
    raw = {"enabled": enabled, "initial_delay_minutes": delay,
           "max_automatic_retries": attempts, "retention_days": days}
    assert normalize_retry_settings(raw).as_dict() == raw


@pytest.mark.parametrize(("invalid", "message"), [
    ({"unexpected": True}, "Invalid retry settings"),
    ({"enabled": 12}, "Choose a valid retry setting"),
    ({"initial_delay_minutes": "30"}, "Choose a valid retry setting"),
    ({"max_automatic_retries": 0}, "Choose between one and five automatic retries"),
    ({"max_automatic_retries": 6}, "Choose between one and five automatic retries"),
    ({"retention_days": 0}, "Choose a supported retention period"),
    ({"retention_days": 3}, "Choose a supported retention period"),
])
def test_retry_policy_validation_errors_are_precise_and_not_user_input(invalid, message):
    with pytest.raises(RetrySettingsError) as exc:
        normalize_retry_settings(invalid)
    assert str(exc.value) == message


@pytest.mark.parametrize(("code", "delay", "automatic_attempts"), [
    ("provider_unavailable", 15, 0),
    ("provider_timeout", 30, 1),
    ("provider_rate_limited", 15, 0),
    ("provider_rate_limited", 60, 0),
    ("provider_timeout", 60, 5),
    ("provider_unavailable", 15, 5),
])
def test_retry_due_exact_deterministic_backoff_including_jitter_and_cap(
    code, delay, automatic_attempts,
):
    """Independent expected clock catches changes to cap, jitter, hash and units."""
    from hashlib import sha256

    now = datetime(2026, 11, 19, 9, 30, tzinfo=UTC)
    failed = replace(make_job().after_failure(code),
                     attempts=automatic_attempts + 1,
                     automatic_attempts=automatic_attempts)
    policy = RetrySettings(enabled=True, initial_delay_minutes=delay,
                           max_automatic_retries=10)
    minutes = min(1440, max(delay, 30) * 2**automatic_attempts
                  if code == "provider_rate_limited" else delay * 2**automatic_attempts)
    # The two-byte big-endian prefix is deliberately stable between restarts.
    jitter = int.from_bytes(sha256(failed.id.encode()).digest()[:2], byteorder="big") % (minutes * 3)
    assert next_retry_at(failed, policy, now) == (
        now + timedelta(minutes=minutes, seconds=jitter)
    ).isoformat()
    assert next_retry_at(failed, policy, now) == next_retry_at(failed, policy, now)


def test_retry_backoff_naive_clock_reports_exact_validation_error():
    failed = make_job().after_failure("provider_timeout")
    with pytest.raises(ValueError) as exc:
        next_retry_at(failed, RetrySettings(enabled=True), datetime(2026, 11, 20))
    assert str(exc.value) == "retry clock must include a timezone"


async def test_scheduler_subscription_is_ha_callback_on_event_loop(monkeypatch):
    """Timer callback must be tagged for HA to keep task creation on its loop."""
    from homeassistant.core import callback as ha_callback

    subscriptions = []
    def subscribe(hass, handler, interval):
        subscriptions.append((hass, handler, interval))
        return Mock()

    monkeypatch.setattr(retry_scheduler, "async_track_time_interval", subscribe)
    hass = scheduler_hass()
    task_name = "Daylight scheduled AI source retry"
    due = replace(make_job().after_failure("provider_unavailable"),
                  next_attempt_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    store = SimpleNamespace(accepting_services=True, list_retry_jobs=Mock(return_value=()))
    execute = AsyncMock(return_value={"status": "saved_for_retry"})
    scheduler = RetryScheduler(hass, store, lambda: RetrySettings(enabled=True), execute)
    assert scheduler._last_retention is None
    assert scheduler._stopped is False
    scheduler.start()
    assert len(subscriptions) == 1
    supplied_hass, handler, interval = subscriptions[0]
    assert supplied_hass is hass
    assert interval == timedelta(minutes=1)
    assert getattr(handler, "__homeassistant_callback", False) is True
    assert ha_callback(handler) is handler
    assert hass.tasks == []

    # Emit an actual event-loop tick, not a direct synchronous call.
    store.list_retry_jobs.return_value = (due,)
    asyncio.get_running_loop().call_soon(handler, datetime.now(UTC))
    await asyncio.sleep(0)
    assert len(hass.tasks) == 1
    assert hass.tasks[0].get_name() == task_name
    await hass.tasks[0]
    execute.assert_awaited_once_with(due.id)
    await scheduler.async_stop()


async def test_scheduler_defaults_to_closed_when_service_admission_is_unknown():
    hass = scheduler_hass()
    execute = AsyncMock()
    scheduler = RetryScheduler(
        hass, SimpleNamespace(list_retry_jobs=lambda: ()),
        lambda: RetrySettings(enabled=True), execute,
    )
    scheduler.kick()
    assert hass.tasks == []
    execute.assert_not_awaited()


async def test_scheduler_exact_retention_tick_and_disabled_policy(monkeypatch):
    """Exactly one hour is due, even when automatic retries are opted out."""
    fixed = datetime(2026, 11, 20, 12, tzinfo=UTC)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return fixed

    monkeypatch.setattr(retry_scheduler, "datetime", Clock)
    hass = scheduler_hass()
    store = SimpleNamespace(
        accepting_services=True, list_retry_jobs=lambda: (),
        async_apply_retention=AsyncMock(),
    )
    scheduler = RetryScheduler(hass, store, lambda: RetrySettings(enabled=False), AsyncMock())
    scheduler._last_retention = fixed - timedelta(hours=1)
    scheduler.kick()
    assert len(hass.tasks) == 1
    await hass.tasks[0]
    store.async_apply_retention.assert_awaited_once_with()
    assert scheduler._last_retention == fixed
    scheduler.kick()
    assert len(hass.tasks) == 1


@pytest.mark.parametrize("status", ["not_due", "not_found", "paused"])
async def test_scheduler_skips_every_rejected_claim_status(status):
    now = datetime.now(UTC) - timedelta(minutes=1)
    first = replace(make_job().after_failure("provider_unavailable"),
                    next_attempt_at=now.isoformat())
    second = replace(first, id="next-after-rejection")
    store = SimpleNamespace(
        accepting_services=True, list_retry_jobs=lambda: (first, second),
    )
    run = AsyncMock(side_effect=[
        {"status": status, "job_id": first.id},
        {"status": "saved_for_retry", "job_id": second.id},
    ])
    scheduler = RetryScheduler(scheduler_hass(), store, lambda: RetrySettings(enabled=True), run)
    await scheduler._async_run_one()
    assert [call.args[0] for call in run.await_args_list] == [first.id, second.id]


async def test_scheduler_stops_after_success_status_even_with_later_due_job():
    now = datetime.now(UTC) - timedelta(minutes=1)
    one = replace(make_job().after_failure("provider_unavailable"),
                  next_attempt_at=now.isoformat())
    two = replace(one, id="second")
    store = SimpleNamespace(
        accepting_services=True, list_retry_jobs=lambda: (one, two),
    )
    run = AsyncMock(return_value={"status": "pending_review", "activity_id": one.id})
    await RetryScheduler(scheduler_hass(), store, lambda: RetrySettings(enabled=True), run)._async_run_one()
    run.assert_awaited_once_with(one.id)


async def test_scheduler_skips_invalid_due_jobs_but_runs_next_eligible():
    now = datetime.now(UTC)
    due = replace(make_job().after_failure("provider_unavailable"),
                  next_attempt_at=(now - timedelta(minutes=1)).isoformat())
    invalid = (
        replace(due, id="paused", state="paused"),
        replace(due, id="no_timestamp", next_attempt_at=None),
        replace(due, id="spent_budget", attempts=3, automatic_attempts=2),
        replace(due, id="future", next_attempt_at=(now + timedelta(minutes=10)).isoformat()),
    )
    good = replace(due, id="eligible")
    store = SimpleNamespace(accepting_services=True, list_retry_jobs=lambda: (*invalid, good))
    run = AsyncMock(return_value={"status": "saved_for_retry"})
    scheduler = RetryScheduler(scheduler_hass(), store,
                               lambda: RetrySettings(enabled=True, max_automatic_retries=2), run)
    await scheduler._async_run_one()
    run.assert_awaited_once_with(good.id)


@pytest.mark.parametrize(("cleanup_fails", "expected"), [
    (True, "Durable retry retention cleanup could not finish"),
    (False, "Scheduled Daylight AI source retry could not finish"),
])
async def test_scheduler_logs_only_sanitized_stable_error_message(
    monkeypatch, cleanup_fails, expected,
):
    warning = Mock()
    monkeypatch.setattr(retry_scheduler._LOGGER, "warning", warning)
    now = datetime.now(UTC)
    due = replace(make_job().after_failure("provider_unavailable"),
                  next_attempt_at=(now - timedelta(minutes=1)).isoformat())
    store = SimpleNamespace(
        accepting_services=True, list_retry_jobs=lambda: (due,),
        async_apply_retention=AsyncMock(side_effect=OSError("private source") if cleanup_fails else None),
    )
    execute = AsyncMock(side_effect=RuntimeError("private provider response"))
    scheduler = RetryScheduler(scheduler_hass(), store, lambda: RetrySettings(enabled=True), execute)
    scheduler._last_retention = now - timedelta(hours=2) if cleanup_fails else now
    await scheduler._async_run_one()
    if cleanup_fails:
        assert [call.args for call in warning.call_args_list] == [
            ("Durable retry retention cleanup could not finish",),
            ("Scheduled Daylight AI source retry could not finish",),
        ]
    else:
        warning.assert_called_once_with(expected)


async def test_stale_scheduler_pause_cannot_invalidate_newer_manual_claim(monkeypatch):
    """A permission check delayed on the lock must not cancel a new parser."""
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    waiting = store.get_retry_job(job_id)
    assert waiting.state == "waiting"
    manual = await store.async_claim_retry(job_id)
    assert manual.state == "processing"
    assert manual.claim != waiting.claim
    saves = len(backend.saved)
    assert await store.async_pause_retry(job_id, expected_job=waiting) == manual
    assert store.get_retry_job(job_id) == manual
    assert len(backend.saved) == saves
    result = await store.async_add(
        source_text=manual.source_text, source_id=manual.source_id,
        calendar_entity=manual.calendar_entity,
        events=(), activity_id=manual.id, retry_claim=manual.claim,
    )
    assert result.pending is None
    assert store.get_retry_job(job_id) is None


async def test_matching_scheduler_pause_is_persisted_and_idempotent(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    waiting = store.get_retry_job(job_id)
    paused = await store.async_pause_retry(job_id, expected_job=waiting)
    assert paused.state == "paused"
    assert paused.claim is None
    assert paused.next_attempt_at is None
    assert store.get_retry_job(job_id) == paused
    assert [item["state"] for item in backend.saved[-1]["retry_jobs"]
            if item["id"] == job_id] == ["paused"]
    # The stale waiting snapshot must not overwrite the updated policy state.
    saved = len(backend.saved)
    assert await store.async_pause_retry(job_id, expected_job=waiting) == paused
    assert len(backend.saved) == saved


async def test_storage_cancellation_finalizes_only_owned_claim_and_survives_restart(monkeypatch):
    """The cancellation path uses the same durable claim-checked failure commit."""
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    store.retry_preferences = lambda: RetrySettings(enabled=True)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_timeout")
    waiting = store.get_retry_job(job_id)
    store._retry_jobs[job_id] = replace(waiting,
        next_attempt_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    active = await store.async_claim_retry(job_id, automatic=True)
    assert store.get_activity(job_id)["status"] == "processing"

    await store.async_record_parse_failure(
        job_id, provider_error_code=active.last_failure_code,
        retry_claim=active.claim,
    )
    released = store.get_retry_job(job_id)
    assert released.state == "waiting"
    assert released.claim is None
    assert released.automatic_attempts == active.automatic_attempts
    assert datetime.fromisoformat(released.next_attempt_at) > datetime.now(UTC)
    assert store.get_activity(job_id)["status"] == "failed"
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    await restored.async_load()
    assert restored.get_retry_job(job_id) == released
    # A late attempt after a manual claim or deletion cannot release it.
    claimed = await store.async_claim_retry(job_id)
    await store.async_record_parse_failure(
        job_id, provider_error_code="provider_timeout", retry_claim=active.claim,
    )
    assert store.get_retry_job(job_id) == claimed


async def test_rearm_skips_nonwaiting_record_and_schedules_later_waiting_record(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    first = await store.async_begin_submission(**source_kwargs())
    second = await store.async_begin_submission(**{
        **source_kwargs(), "retry_source_id": "another-import",
    })
    await store.async_record_parse_failure(second, provider_error_code="provider_timeout")
    assert store.get_retry_job(first).state == "processing"
    assert store.get_retry_job(second).next_attempt_at is None
    store.retry_preferences = lambda: RetrySettings(enabled=True)
    await store.async_rearm_retry_jobs()
    assert store.get_retry_job(first).state == "processing"
    scheduled = store.get_retry_job(second)
    assert scheduled.state == "waiting"
    assert scheduled.next_attempt_at is not None
    assert [item["next_attempt_at"] for item in backend.saved[-1]["retry_jobs"]
            if item["id"] == second] == [scheduled.next_attempt_at]


@pytest.mark.parametrize(("kind", "expected"), [
    ("notfound", "Retry is unavailable or already processing"),
    ("processing", "Retry is unavailable or already processing"),
    ("disabled", "Automatic retry is not due"),
    ("paused", "Automatic retry is not due"),
    ("no_timestamp", "Automatic retry is not due"),
    ("budget", "Automatic retry is not due"),
    ("review", "Events are already awaiting review"),
])
async def test_claim_retry_refuses_ineligible_without_mutating_state(
    monkeypatch, kind, expected,
):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    store.retry_preferences = lambda: RetrySettings(enabled=kind != "disabled", max_automatic_retries=2)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    waiting = store.get_retry_job(job_id)
    prepared = replace(waiting, next_attempt_at=(datetime.now(UTC) - timedelta(minutes=1)).isoformat())
    if kind == "notfound":
        target = "unknown-job"
    else:
        target = job_id
        if kind == "processing":
            prepared = replace(prepared, state="processing", claim="claimed")
        if kind == "paused":
            prepared = replace(prepared, state="paused")
        if kind == "no_timestamp":
            prepared = replace(prepared, next_attempt_at=None)
        if kind == "budget":
            prepared = replace(prepared, automatic_attempts=2, attempts=3)
        if kind == "review":
            from tests.test_storage import draft
            from custom_components.daylight_calendar_import.storage import PendingImport
            store._items[job_id] = PendingImport.create(
                source_text="review", events=[draft()],
            )
        store._retry_jobs[job_id] = prepared
    saved = len(backend.saved)
    with pytest.raises(RetryJobConflict) as exc:
        await store.async_claim_retry(target, automatic=kind not in ("notfound", "processing", "review"))
    assert str(exc.value) == expected
    if kind != "notfound":
        assert store.get_retry_job(job_id) == prepared
    assert len(backend.saved) == saved


async def test_automatic_claim_is_due_at_exact_stored_timestamp(monkeypatch):
    import custom_components.daylight_calendar_import.storage as storage_module

    moment = datetime(2026, 11, 20, 12, tzinfo=UTC)
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    store.retry_preferences = lambda: RetrySettings(enabled=True, max_automatic_retries=1)
    job_id = await store.async_begin_submission(**source_kwargs())
    await store.async_record_parse_failure(job_id, provider_error_code="provider_unavailable")
    job = store.get_retry_job(job_id)
    store._retry_jobs[job_id] = replace(job, next_attempt_at=moment.isoformat())

    class FixedClock(datetime):
        @classmethod
        def now(cls, tz=None):
            return moment

    monkeypatch.setattr(storage_module, "datetime", FixedClock)
    claimed = await store.async_claim_retry(job_id, automatic=True)
    assert claimed.state == "processing"
    assert claimed.automatic_attempts == 1
    assert claimed.attempts == 2
    assert claimed.next_attempt_at is None
    assert claimed.claim is not None
    assert store.get_activity(job_id)["status"] == "processing"
    await store.async_record_parse_failure(
        job_id, provider_error_code="provider_unavailable", retry_claim=claimed.claim,
    )
    assert store.get_retry_job(job_id).state == "exhausted"
    with pytest.raises(RetryJobConflict) as exc:
        await store.async_claim_retry(job_id, automatic=True)
    assert str(exc.value) == "Automatic retry is not due"


async def test_interrupted_processing_restart_sets_failure_code_and_durable_backoff(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    job_id = await store.async_begin_submission(**source_kwargs())
    backend.load_result = backend.saved[-1]
    restored = make_store(monkeypatch, backend)
    restored.retry_preferences = lambda: RetrySettings(enabled=True)
    await restored.async_load()
    job = restored.get_retry_job(job_id)
    assert job.state == "waiting"
    assert job.claim is None
    assert job.last_failure_code == "provider_unavailable"
    assert job.automatic_attempts == 0
    assert datetime.fromisoformat(job.next_attempt_at) > datetime.now(UTC)
    assert restored.get_activity(job_id)["status"] == "failed"
    assert [item["last_failure_code"] for item in backend.saved[-1]["retry_jobs"]
            if item["id"] == job_id] == ["provider_unavailable"]
