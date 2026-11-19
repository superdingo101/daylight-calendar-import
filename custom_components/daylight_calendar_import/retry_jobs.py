"""Private, validated snapshots of sources awaiting a parser retry.

A retry job is not an event draft. No source in this collection can be approved
or written to a calendar without passing the ordinary extraction/review path.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from .const import MAX_SOURCE_TEXT_CHARS

RETRYABLE_PROVIDER_CODES = frozenset({
    "provider_unavailable", "provider_timeout", "provider_rate_limited",
})
RETRY_STATES = frozenset({"processing", "waiting", "paused", "exhausted", "cancelled"})
MAX_RETRY_JOBS = 100


@dataclass(frozen=True, slots=True)
class RetryJob:
    """A private, restart-safe parser source and its processing identity."""

    id: str
    source_text: str
    source_id: str
    received_at: str
    provider_entity_id: str
    calendar_entity: str
    owner_user_id: str | None
    state: str = "processing"
    attempts: int = 1
    automatic_attempts: int = 0
    claim: str | None = None
    last_failure_code: str | None = None
    next_attempt_at: str | None = None
    source_kind: str = "manual_text"
    source_title: str | None = None
    attachment_path: str | None = None
    attachment_sha256: str | None = None
    attachment_media_type: str | None = None
    attachment_size: int | None = None
    attachment_in_ai: bool = True
    routing_unresolved: bool = False
    reimport_duplicates: bool = False

    @classmethod
    def create(
        cls, *, activity_id: str, source_text: str, source_id: str,
        received_at: datetime, provider_entity_id: str, calendar_entity: str,
        owner_user_id: str | None, source_kind: str = "manual_text",
        source_title: str | None = None, attachment_path: str | None = None,
        attachment_sha256: str | None = None, attachment_media_type: str | None = None,
        attachment_size: int | None = None, attachment_in_ai: bool = True,
        routing_unresolved: bool = False,
        reimport_duplicates: bool = False,
    ) -> "RetryJob":
        return cls.from_dict({
            "id": activity_id, "source_text": source_text, "source_id": source_id,
            "received_at": received_at.isoformat(), "provider_entity_id": provider_entity_id,
            "calendar_entity": calendar_entity, "owner_user_id": owner_user_id,
            "source_kind": source_kind, "source_title": source_title,
            "attachment_path": attachment_path, "attachment_sha256": attachment_sha256,
            "attachment_media_type": attachment_media_type, "attachment_size": attachment_size,
            "attachment_in_ai": attachment_in_ai,
            "routing_unresolved": routing_unresolved,
            "reimport_duplicates": reimport_duplicates,
            "claim": str(uuid4()), "state": "processing", "attempts": 1,
        })

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> "RetryJob":
        if not isinstance(raw, dict) or set(raw) - set(cls.__dataclass_fields__):
            raise ValueError("invalid retry job fields")
        job = cls(**raw)
        for value in (job.id, job.source_id, job.received_at,
                      job.provider_entity_id, job.calendar_entity):
            if not isinstance(value, str) or not value.strip() or len(value) > 2048:
                raise ValueError("invalid retry job identity")
        if not isinstance(job.source_text, str) or len(job.source_text) > MAX_SOURCE_TEXT_CHARS:
            raise ValueError("invalid retry source text")
        try:
            received = datetime.fromisoformat(job.received_at)
            next_attempt = (datetime.fromisoformat(job.next_attempt_at)
                            if job.next_attempt_at is not None else None)
        except (TypeError, ValueError) as err:
            raise ValueError("invalid retry timestamp") from err
        if received.tzinfo is None or (next_attempt is not None and next_attempt.tzinfo is None):
            raise ValueError("retry timestamp must be timezone aware")
        if job.state not in RETRY_STATES or type(job.attempts) is not int or not 1 <= job.attempts <= 1000:
            raise ValueError("invalid retry lifecycle")
        if (type(job.automatic_attempts) is not int
                or not 0 <= job.automatic_attempts <= job.attempts):
            raise ValueError("invalid automatic retry count")
        if job.state == "processing" and not job.claim:
            raise ValueError("processing retry must have a claim")
        if job.claim is not None and (not isinstance(job.claim, str) or len(job.claim) > 128):
            raise ValueError("invalid retry claim")
        if job.last_failure_code is not None and (
            not isinstance(job.last_failure_code, str)
            or job.last_failure_code not in {
                "provider_timeout", "provider_rate_limited", "provider_unavailable",
                "provider_error", "provider_authentication", "provider_invalid_response",
            }
        ):
            raise ValueError("invalid retry failure")
        if job.source_kind not in ("manual_text", "image", "pdf"):
            raise ValueError("unsupported retry source kind")
        for value in (job.owner_user_id, job.source_title, job.attachment_path,
                      job.attachment_sha256, job.attachment_media_type):
            if value is not None and (not isinstance(value, str) or len(value) > 2048):
                raise ValueError("invalid retry metadata")
        if (type(job.routing_unresolved) is not bool or type(job.reimport_duplicates) is not bool
                or type(job.attachment_in_ai) is not bool):
            raise ValueError("invalid retry flags")
        if job.attachment_size is not None and (
            type(job.attachment_size) is not int or not 1 <= job.attachment_size <= 10 * 1024 * 1024
        ):
            raise ValueError("invalid retry attachment size")
        if job.source_kind == "manual_text" and (not job.source_text.strip() or job.attachment_path is not None):
            raise ValueError("invalid text retry payload")
        if job.source_kind != "manual_text" and (
            bool(job.attachment_path) != bool(job.attachment_sha256)
            or bool(job.attachment_path) != bool(job.attachment_media_type)
            or bool(job.attachment_path) != bool(job.attachment_size)
        ):
            raise ValueError("invalid attachment retry metadata")
        return job

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def summary(self) -> dict[str, Any]:
        """Safe for authenticated status lists; never expose raw input or file paths."""
        return {
            "id": self.id, "source_kind": self.source_kind, "source_title": self.source_title,
            "received_at": self.received_at, "state": self.state,
            "attempts": self.attempts, "automatic_attempts": self.automatic_attempts,
            "next_attempt_at": self.next_attempt_at,
            "last_failure_code": self.last_failure_code,
        }

    def after_failure(self, code: str | None, *, next_attempt_at: str | None = None) -> "RetryJob":
        return replace(
            self, state="waiting" if code in RETRYABLE_PROVIDER_CODES else "paused",
            claim=None, last_failure_code=code, next_attempt_at=next_attempt_at,
        )
