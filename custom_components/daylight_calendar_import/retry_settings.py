"""Conservative, provider-neutral automatic retry policy."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from hashlib import sha256
from typing import Mapping

from .retry_jobs import RetryJob, RETRYABLE_PROVIDER_CODES


class RetrySettingsError(ValueError):
    """Retry preferences must be explicitly bounded and opt-in."""


@dataclass(frozen=True, slots=True)
class RetrySettings:
    enabled: bool = False
    initial_delay_minutes: int = 15
    max_automatic_retries: int = 3
    retention_days: int = 7

    def as_dict(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "initial_delay_minutes": self.initial_delay_minutes,
            "max_automatic_retries": self.max_automatic_retries,
            "retention_days": self.retention_days,
        }


def normalize_retry_settings(raw: Mapping[str, object]) -> RetrySettings:
    if not isinstance(raw, Mapping) or set(raw) - {
        "enabled", "initial_delay_minutes", "max_automatic_retries", "retention_days",
    }:
        raise RetrySettingsError("Invalid retry settings")
    enabled = raw.get("enabled", False)
    delay = raw.get("initial_delay_minutes", 15)
    attempts = raw.get("max_automatic_retries", 3)
    days = raw.get("retention_days", 7)
    if type(enabled) is not bool or type(delay) is not int or delay not in (15, 30, 60):
        raise RetrySettingsError("Choose a valid retry setting")
    if type(attempts) is not int or not 1 <= attempts <= 5:
        raise RetrySettingsError("Choose between one and five automatic retries")
    if type(days) is not int or days not in (1, 7, 14, 30):
        raise RetrySettingsError("Choose a supported retention period")
    return RetrySettings(enabled, delay, attempts, days)


def next_retry_at(job: RetryJob, preferences: RetrySettings, now: datetime | None = None) -> str | None:
    """Backoff for transient errors; automatic attempt budget excludes manual tries."""
    if (not preferences.enabled or job.last_failure_code not in RETRYABLE_PROVIDER_CODES
            or job.automatic_attempts >= preferences.max_automatic_retries):
        return None
    current = now or datetime.now(UTC)
    if current.tzinfo is None:
        raise ValueError("retry clock must include a timezone")
    base_minutes = preferences.initial_delay_minutes
    if job.last_failure_code == "provider_rate_limited":
        base_minutes = max(30, base_minutes)
    minutes = min(24 * 60, base_minutes * (2 ** job.automatic_attempts))
    # Stable, tiny jitter avoids synchronized retries after widespread outages.
    jitter_seconds = int.from_bytes(sha256(job.id.encode()).digest()[:2], "big") % max(1, minutes * 3)
    return (current + timedelta(minutes=minutes, seconds=jitter_seconds)).isoformat()
