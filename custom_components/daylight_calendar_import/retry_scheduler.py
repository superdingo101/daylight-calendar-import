"""Home Assistant-managed, single-flight scheduler for durable AI retries."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta

from homeassistant.helpers.event import async_track_time_interval

from .retry_settings import RetrySettings
from .storage import PendingImportStore

_LOGGER = logging.getLogger(__name__)


class RetryScheduler:
    """Run at most one due retry per entry; all attempts are claimed in storage."""

    def __init__(
        self, hass, store: PendingImportStore,
        settings: Callable[[], RetrySettings],
        execute: Callable[[str], Awaitable[None]],
        cleanup: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._hass = hass
        self._store = store
        self._settings = settings
        self._execute = execute
        self._cleanup = cleanup
        self._last_retention: datetime | None = None
        self._unsubscribe: Callable[[], None] | None = None
        self._task: asyncio.Task | None = None
        self._stopped = False

    def start(self) -> None:
        if self._unsubscribe is not None:
            return
        self._unsubscribe = async_track_time_interval(
            self._hass, lambda _now: self.kick(), timedelta(minutes=1),
        )
        self.kick()

    def kick(self) -> None:
        if self._stopped or not getattr(self._store, "accepting_services", False):
            return
        if self._task is not None and not self._task.done():
            return
        now = datetime.now(UTC)
        cleanup_due = (self._last_retention is None
                       or now - self._last_retention >= timedelta(hours=1))
        retry_due = self._settings().enabled and any(
            job.state == "waiting" and job.next_attempt_at is not None
            and datetime.fromisoformat(job.next_attempt_at) <= now
            for job in self._store.list_retry_jobs()
        )
        if not cleanup_due and not retry_due:
            return
        self._task = self._hass.async_create_task(
            self._async_run_one(), "Daylight scheduled AI source retry", eager_start=False,
        )

    async def _async_run_one(self) -> None:
        if self._stopped:
            return
        now = datetime.now(UTC)
        if (self._last_retention is None
                or now - self._last_retention >= timedelta(hours=1)):
            try:
                await self._store.async_apply_retention()
                if self._cleanup is not None:
                    await self._cleanup()
                self._last_retention = now
            except asyncio.CancelledError:
                raise
            except Exception:
                _LOGGER.warning("Durable retry retention cleanup could not finish")
        if self._stopped or not self._settings().enabled:
            return
        for job in self._store.list_retry_jobs():
            if (job.state != "waiting" or job.next_attempt_at is None
                    or datetime.fromisoformat(job.next_attempt_at) > now):
                continue
            try:
                await self._execute(job.id)
            except asyncio.CancelledError:
                raise
            except Exception:
                # Never log raw source, provider exception, or credentials.
                _LOGGER.warning("Scheduled Daylight AI source retry could not finish")
            return

    async def async_stop(self) -> None:
        self._stopped = True
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        if self._task is not None and not self._task.done():
            self._task.cancel()
            await asyncio.gather(self._task, return_exceptions=True)
        self._task = None
