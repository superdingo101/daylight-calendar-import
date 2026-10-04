"""Persistent storage for pending calendar imports."""

from __future__ import annotations

import asyncio
from copy import deepcopy
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store

from .const import DOMAIN
from .dedup import (
    event_fingerprint,
    source_fingerprint as build_source_fingerprint,
)
from .models import EventDraft

STORAGE_VERSION = 2
STORAGE_KEY = f"{DOMAIN}.pending_imports"
DEDUP_HISTORY_LIMIT = 10_000
ACTIVITY_LIMIT = 500
ACTIVITY_TRANSITIONS_PER_IMPORT = 32
_STORAGE_ITEMS = "items"
_STORAGE_ACTIVITY = "activity"
_STORAGE_SOURCE_CLAIMS = "source_claims"
_STORAGE_SEEN_SOURCES = "seen_source_fingerprints"
_STORAGE_SEEN_EVENTS = "seen_event_fingerprints"


class PendingImportApprovalUncertainError(RuntimeError):
    """Raised when retrying an import with an uncertain prior approval outcome."""


class PendingEventEditError(ValueError):
    """Raised when an event cannot safely be edited."""


class PendingEventResolutionError(ValueError):
    """Raised when resolving an event without an uncertain write."""


@dataclass(frozen=True, slots=True)
class PendingEvent:
    """A reviewable event with a stable identity."""

    id: str
    draft: EventDraft
    status: str = "pending"
    calendar_entity: str | None = None
    write_attempt: str | None = None

    @classmethod
    def create(cls, draft: EventDraft, calendar_entity: str | None = None) -> PendingEvent:
        return cls(str(uuid4()), draft, calendar_entity=calendar_entity)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PendingEvent:
        if raw["status"] not in ("pending", "write_uncertain"):
            raise ValueError("invalid pending event status")
        return cls(raw["id"], EventDraft.from_mapping(raw["draft"]), raw["status"],
                   raw.get("calendar_entity"), raw.get("write_attempt"))

    def as_dict(self) -> dict[str, Any]:
        result = {"id": self.id, "draft": self.draft.as_dict(), "status": self.status,
                  "calendar_entity": self.calendar_entity}
        if self.write_attempt is not None:
            result["write_attempt"] = self.write_attempt
        return result

    def as_service_dict(self) -> dict[str, Any]:
        """Expose the stable ID alongside the existing flat draft fields."""
        result = {**self.draft.as_dict(), "id": self.id, "status": self.status,
                  "calendar_entity": self.calendar_entity}
        if self.write_attempt is not None:
            result["write_attempt"] = self.write_attempt
        return result


def _migrate_v1(data: dict[str, Any]) -> dict[str, Any]:
    """Convert v1 items, retaining source and handled-event fingerprints."""
    result = dict(data)
    items = []
    for raw in data[_STORAGE_ITEMS]:
        item = dict(raw)
        item["events"] = [
            PendingEvent(
                str(uuid4()), EventDraft.from_mapping(event),
                "write_uncertain" if index == 0 and raw.get("approval_in_flight") else "pending",
            ).as_dict()
            for index, event in enumerate(raw["events"])
        ]
        item.pop("approval_in_flight", None)
        items.append(item)
    result[_STORAGE_ITEMS] = items
    return result


class _PendingStore(Store[dict[str, Any]]):
    """Migrate existing Home Assistant Store data before it is loaded."""

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version != 1:
            raise ValueError(f"Unsupported pending storage version: {old_major_version}")
        return _migrate_v1(old_data)


@dataclass(frozen=True, slots=True)
class PendingImport:
    """A parsed import waiting for an explicit user decision."""

    id: str
    created_at: str
    source_text: str
    events: tuple[PendingEvent, ...]
    source_fingerprint: str | None = None
    source_kind: str = "manual_text"
    source_title: str | None = None
    warnings: tuple[str, ...] = ()
    duplicate_events: int = 0

    @classmethod
    def create(
        cls,
        *,
        source_text: str,
        events: Iterable[EventDraft],
        source_fingerprint: str | None = None,
        calendar_entity: str | None = None,
        source_kind: str = "manual_text",
        source_title: str | None = None,
        warnings: Iterable[str] = (),
        duplicate_events: int = 0,
        activity_id: str | None = None,
        received_at: str | None = None,
    ) -> PendingImport:
        """Create a new pending import with stable persisted metadata."""
        source_text = source_text.strip()
        if not source_text:
            raise ValueError("source_text must be a non-empty string")

        event_tuple = tuple(PendingEvent.create(event, calendar_entity) for event in events)
        if not event_tuple:
            raise ValueError("pending import must contain at least one event")

        return cls(
            id=activity_id or str(uuid4()),
            created_at=received_at or datetime.now(UTC).isoformat(),
            source_text=source_text,
            events=event_tuple,
            source_fingerprint=source_fingerprint,
            source_kind=source_kind,
            source_title=source_title,
            warnings=tuple(warnings),
            duplicate_events=duplicate_events,
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PendingImport:
        """Restore a pending import from Home Assistant storage."""
        return cls(
            id=raw["id"],
            created_at=raw["created_at"],
            source_text=raw["source_text"],
            events=tuple(PendingEvent.from_dict(event) for event in raw["events"]),
            source_fingerprint=raw.get("source_fingerprint"),
            source_kind=raw.get("source_kind", "manual_text"),
            source_title=raw.get("source_title"),
            warnings=tuple(raw.get("warnings", ())),
            duplicate_events=raw.get("duplicate_events", 0),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable storage representation."""
        result = {
            "id": self.id,
            "created_at": self.created_at,
            "source_text": self.source_text,
            "events": [event.as_dict() for event in self.events],
        }
        if self.source_kind != "manual_text":
            result["source_kind"] = self.source_kind
        if self.source_title is not None:
            result["source_title"] = self.source_title
        if self.warnings:
            result["warnings"] = list(self.warnings)
        if self.duplicate_events:
            result["duplicate_events"] = self.duplicate_events
        if self.source_fingerprint is not None:
            result["source_fingerprint"] = self.source_fingerprint
        return result

    def as_service_dict(self) -> dict[str, Any]:
        """Keep the existing submit response fields while exposing event IDs."""
        result = self.as_dict()
        result.update(source_kind=self.source_kind, source_title=self.source_title,
                      warnings=list(self.warnings), duplicate_events=self.duplicate_events)
        result["events"] = [event.as_service_dict() for event in self.events]
        result["approval_in_flight"] = self.approval_in_flight
        return result

    @property
    def approval_in_flight(self) -> bool:
        """Report an uncertain write anywhere in the import."""
        return any(event.status == "write_uncertain" for event in self.events)


@dataclass(frozen=True, slots=True)
class PendingImportAddResult:
    """Result of adding a possibly duplicate import."""

    pending: PendingImport | None
    duplicate_source: bool
    duplicate_events: int


class PendingImportStore:
    """Persistent collection of imports awaiting review."""

    def __init__(self, hass: HomeAssistant) -> None:
        """Initialize the pending-import store."""
        self._store: Store[dict[str, Any]] = _PendingStore(
            hass,
            STORAGE_VERSION,
            STORAGE_KEY,
            private=True,
        )
        self._items: dict[str, PendingImport] = {}
        self._activity: tuple[dict[str, Any], ...] = ()
        self._seen_source_fingerprints: tuple[str, ...] = ()
        self._seen_event_fingerprints: tuple[str, ...] = ()
        self._source_claims: dict[str, str] = {}
        self._source_claim_releases: set[str] = set()
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load pending imports and deduplication history."""
        data = await self._store.async_load()
        if data is None:
            self._items = {}
            self._activity = ()
            self._seen_source_fingerprints = ()
            self._seen_event_fingerprints = ()
            self._source_claims = {}
            self._source_claim_releases = set()
            return

        items = (
            PendingImport.from_dict(raw) for raw in data[_STORAGE_ITEMS]
        )
        self._items = {item.id: item for item in items}
        self._activity = tuple(data.get(_STORAGE_ACTIVITY, ()))
        self._seen_source_fingerprints = tuple(
            data.get(_STORAGE_SEEN_SOURCES, ())
        )
        self._seen_event_fingerprints = tuple(
            data.get(_STORAGE_SEEN_EVENTS, ())
        )
        self._source_claims = dict(data.get(_STORAGE_SOURCE_CLAIMS, {}))
        self._source_claim_releases = set()
        interrupted = [
            item["id"]
            for item in self._activity
            if item["status"] in ("discovered", "processing")
        ]
        for activity_id in interrupted:
            self._activity = self._finish_submission(
                activity_id, "failed", "Processing was interrupted. Check the source and submit it again."
            )
        if interrupted or self._source_claims:
            await self._async_save(
                self._items,
                activity=self._activity,
                source_claims={},
            )
            self._source_claims = {}

    def get(self, pending_id: str) -> PendingImport | None:
        """Return one pending import by ID."""
        return self._items.get(pending_id)

    def get_event(self, pending_id: str, event_id: str) -> PendingEvent | None:
        """Look up an event by its stable ID, independent of list position."""
        pending = self.get(pending_id)
        if pending is None:
            return None
        return next((event for event in pending.events if event.id == event_id), None)

    def list(self) -> tuple[PendingImport, ...]:
        """Return pending imports in insertion order."""
        return tuple(self._items.values())

    def list_activity(self) -> tuple[dict[str, Any], ...]:
        """Return bounded lifecycle summaries, newest first, without source text."""
        return tuple(deepcopy(item) for item in reversed(self._activity))

    def get_activity(self, pending_id: str) -> dict[str, Any] | None:
        """Return a durable activity record even after an import leaves review."""
        return next((deepcopy(item) for item in self._activity if item["id"] == pending_id), None)

    def _protected_activity_ids(self) -> set[str]:
        """Keep pending imports and live source checkpoints outside the history cap."""
        return set(self._items) | {
            item["id"]
            for item in self._activity
            if item["status"] in ("discovered", "processing")
        }

    def _propose_activity(
        self,
        *,
        source_kind: str,
        source_title: str | None,
        received_at: datetime,
        initial_status: str,
    ) -> tuple[str, tuple[dict[str, Any], ...]]:
        """Build a live activity record without mutating durable state."""
        identifier = str(uuid4())
        received = received_at.isoformat()
        transitioned = datetime.now(UTC).isoformat()
        record = {
            "id": identifier, "created_at": received, "source_kind": source_kind,
            "source_title": source_title, "title": source_title or "Submission",
            "created_count": 0, "rejected_count": 0, "status": initial_status,
            "transitions": [{"type": "received", "at": received, "event_id": None},
                            {"type": initial_status, "at": transitioned, "event_id": None}],
        }
        history = list(self._activity) + [record]
        active_ids = self._protected_activity_ids()
        active_ids.add(identifier)
        while sum(item["id"] not in active_ids for item in history) > ACTIVITY_LIMIT:
            history.pop(next(index for index, item in enumerate(history)
                             if item["id"] not in active_ids))
        return identifier, tuple(history)

    def _propose_submission_activity(
        self,
        *,
        source_kind: str,
        source_title: str | None,
        received_at: datetime,
    ) -> tuple[str, tuple[dict[str, Any], ...]]:
        """Build a processing activity record without mutating durable state."""
        return self._propose_activity(
            source_kind=source_kind,
            source_title=source_title,
            received_at=received_at,
            initial_status="processing",
        )

    async def async_begin_source_discovery(
        self,
        *,
        source_kind: str,
        source_title: str | None,
        received_at: datetime,
    ) -> str:
        """Durably record a source before normalization or policy evaluation."""
        result, cancelled = await self._async_complete_transaction(
            self._async_begin_source_discovery_transaction(
                source_kind=source_kind,
                source_title=source_title,
                received_at=received_at,
            )
        )
        activity_id = result
        if cancelled:
            try:
                await self.async_record_source_failure(
                    activity_id,
                    "Source discovery was interrupted. Check the source and try again.",
                )
            except (asyncio.CancelledError, Exception):
                pass
            raise asyncio.CancelledError
        return activity_id

    async def _async_begin_source_discovery_transaction(
        self,
        *,
        source_kind: str,
        source_title: str | None,
        received_at: datetime,
    ) -> str:
        """Persist one discovery checkpoint in an uncancelled transaction."""
        async with self._lock:
            identifier, activity = self._propose_activity(
                source_kind=source_kind,
                source_title=source_title,
                received_at=received_at,
                initial_status="discovered",
            )
            await self._async_save(self._items, activity=activity)
            self._activity = activity
            return identifier

    async def async_begin_submission(self, *, source_kind: str, source_title: str | None,
                                     received_at: datetime) -> str:
        """Checkpoint a private source summary before invoking the parser."""
        async with self._lock:
            identifier, activity = self._propose_submission_activity(
                source_kind=source_kind,
                source_title=source_title,
                received_at=received_at,
            )
            await self._async_save(self._items, activity=activity)
            self._activity = activity
            return identifier

    async def _async_complete_transaction(
        self,
        operation: Awaitable[Any],
    ) -> tuple[Any, bool]:
        """Let a storage transaction finish before propagating caller cancellation."""
        task = asyncio.create_task(operation)
        cancelled = False
        while not task.done():
            try:
                await asyncio.wait({task})
            except asyncio.CancelledError:
                cancelled = True
                asyncio.current_task().uncancel()  # type: ignore[union-attr]
        if task.cancelled():
            raise asyncio.CancelledError
        try:
            result = task.result()
        except Exception:
            if cancelled:
                raise asyncio.CancelledError from None
            raise
        return result, cancelled

    async def async_claim_source_discovery(
        self,
        activity_id: str,
        *,
        source_id: str,
        source_kind: str,
        source_title: str | None,
    ) -> bool:
        """Promote one discovered source to processing or a terminal duplicate."""
        result, cancelled = await self._async_complete_transaction(
            self._async_claim_source_discovery_transaction(
                activity_id,
                source_id=source_id,
                source_kind=source_kind,
                source_title=source_title,
            )
        )
        claimed = result
        if cancelled:
            try:
                await self.async_record_source_failure(
                    activity_id,
                    "Source processing was interrupted. Check the source and try again.",
                )
            except (asyncio.CancelledError, Exception):
                pass
            raise asyncio.CancelledError
        return claimed

    async def _async_claim_source_discovery_transaction(
        self,
        activity_id: str,
        *,
        source_id: str,
        source_kind: str,
        source_title: str | None,
    ) -> bool:
        """Claim a discovered source identity without creating a second activity."""
        fingerprint = build_source_fingerprint(source_id)
        async with self._lock:
            await self._async_retry_source_claim_releases_locked()
            record = self.get_activity(activity_id)
            if record is None or record["status"] != "discovered":
                raise ValueError("activity must reference a discovered source")

            record["source_kind"] = source_kind
            record["source_title"] = source_title
            if source_title:
                record["title"] = source_title

            if self._source_fingerprint_exists(fingerprint):
                record["status"] = "duplicate"
                record["transitions"].append({
                    "type": "duplicate",
                    "at": datetime.now(UTC).isoformat(),
                    "event_id": None,
                })
                activity = tuple(
                    record if item["id"] == activity_id else item
                    for item in self._activity
                )
                await self._async_save(self._items, activity=activity)
                self._activity = activity
                return False

            record["status"] = "processing"
            record["transitions"].append({
                "type": "processing",
                "at": datetime.now(UTC).isoformat(),
                "event_id": None,
            })
            activity = tuple(
                record if item["id"] == activity_id else item
                for item in self._activity
            )
            source_claims = dict(self._source_claims)
            source_claims[activity_id] = fingerprint
            await self._async_save(
                self._items,
                activity=activity,
                source_claims=source_claims,
            )
            self._activity = activity
            self._source_claims = source_claims
            return True

    async def async_begin_source_submission(
        self,
        *,
        source_id: str,
        source_kind: str,
        source_title: str | None,
        received_at: datetime,
    ) -> str | None:
        """Atomically reserve a source identity before an expensive parser call."""
        result, cancelled = await self._async_complete_transaction(
            self._async_begin_source_submission_transaction(
                source_id=source_id,
                source_kind=source_kind,
                source_title=source_title,
                received_at=received_at,
            )
        )
        activity_id = result
        if cancelled:
            if activity_id is not None:
                try:
                    await self.async_record_parse_failure(activity_id)
                except (asyncio.CancelledError, Exception):
                    pass
            raise asyncio.CancelledError
        return activity_id

    async def _async_begin_source_submission_transaction(
        self,
        *,
        source_id: str,
        source_kind: str,
        source_title: str | None,
        received_at: datetime,
    ) -> str | None:
        """Reserve a source identity in one uncancelled storage transaction."""
        fingerprint = build_source_fingerprint(source_id)
        async with self._lock:
            await self._async_retry_source_claim_releases_locked()
            if self._source_fingerprint_exists(fingerprint):
                return None
            identifier, activity = self._propose_submission_activity(
                source_kind=source_kind,
                source_title=source_title,
                received_at=received_at,
            )
            source_claims = dict(self._source_claims)
            source_claims[identifier] = fingerprint
            await self._async_save(
                self._items,
                activity=activity,
                source_claims=source_claims,
            )
            self._activity = activity
            self._source_claims = source_claims
            return identifier

    async def async_record_parse_failure(self, activity_id: str) -> None:
        """Finalize a failed parse while keeping the original source private."""
        await self.async_record_source_failure(
            activity_id,
            "Parsing failed. Check the configured AI Task and submit the source again.",
        )

    async def async_record_source_failure(
        self,
        activity_id: str,
        guidance: str,
    ) -> None:
        """Finalize a discovered or processing source as failed."""
        _, cancelled = await self._async_complete_transaction(
            self._async_record_source_failure_transaction(
                activity_id,
                guidance,
            )
        )
        if cancelled:
            raise asyncio.CancelledError

    async def _async_record_source_failure_transaction(
        self,
        activity_id: str,
        guidance: str,
    ) -> None:
        """Release one source claim in an uncancelled storage transaction."""
        async with self._lock:
            activity = self.get_activity(activity_id)
            if (
                activity is not None
                and activity["status"] not in ("discovered", "processing")
            ):
                return
            if activity_id in self._source_claims:
                self._source_claim_releases.add(activity_id)
            await self._async_record_parse_failure_locked(
                activity_id,
                guidance=guidance,
            )

    async def async_record_terminal_source_failure(
        self,
        activity_id: str,
        *,
        source_id: str,
        guidance: str,
    ) -> None:
        """Persist a deterministic source failure as durably handled."""
        _, cancelled = await self._async_complete_transaction(
            self._async_record_terminal_source_failure_transaction(
                activity_id,
                source_id=source_id,
                guidance=guidance,
            )
        )
        if cancelled:
            raise asyncio.CancelledError

    async def _async_record_terminal_source_failure_transaction(
        self,
        activity_id: str,
        *,
        source_id: str,
        guidance: str,
    ) -> None:
        """Remember one failed claimed source so unchanged retries are suppressed."""
        fingerprint = build_source_fingerprint(source_id)
        async with self._lock:
            activity = self.get_activity(activity_id)
            if (
                activity is not None
                and activity["status"] not in ("discovered", "processing")
            ):
                return
            if self._source_claims.get(activity_id) != fingerprint:
                raise ValueError("source_id does not match claimed source")

            activity = self._finish_submission(
                activity_id,
                "failed",
                guidance,
            )
            source_claims = dict(self._source_claims)
            source_claims.pop(activity_id, None)
            seen_sources = _remember_fingerprints(
                self._seen_source_fingerprints,
                (fingerprint,),
            )
            # If terminal persistence fails, retry releasing the in-memory claim
            # before the next duplicate check so the unchanged message can be
            # processed again after storage recovers.
            self._source_claim_releases.add(activity_id)
            await self._async_save(
                self._items,
                seen_source_fingerprints=seen_sources,
                activity=activity,
                source_claims=source_claims,
            )
            self._activity = activity
            self._seen_source_fingerprints = seen_sources
            self._source_claims = source_claims
            self._source_claim_releases.discard(activity_id)

    async def _async_record_parse_failure_locked(
        self,
        activity_id: str,
        *,
        guidance: str = (
            "Parsing failed. Check the configured AI Task and submit the source again."
        ),
    ) -> None:
        """Persist one failure transition and release its source claim."""
        activity = self._finish_submission(
            activity_id,
            "failed",
            guidance,
        )
        source_claims = dict(self._source_claims)
        source_claims.pop(activity_id, None)
        await self._async_save(
            self._items,
            activity=activity,
            source_claims=source_claims,
        )
        self._activity = activity
        self._source_claims = source_claims
        self._source_claim_releases.discard(activity_id)

    async def _async_retry_source_claim_releases_locked(self) -> None:
        """Retry claim releases that previously failed to persist."""
        for activity_id in tuple(self._source_claim_releases):
            await self._async_record_parse_failure_locked(activity_id)

    def _finish_submission(self, activity_id: str | None, status: str,
                           guidance: str | None = None) -> tuple[dict[str, Any], ...]:
        """Propose a terminal source status in the next storage transaction."""
        if activity_id is None:
            return self._activity
        record = self.get_activity(activity_id)
        if record is None:
            return self._activity
        record["status"] = status
        if guidance is not None:
            record["guidance"] = guidance
        record["transitions"].append({"type": status, "at": datetime.now(UTC).isoformat(),
                                      "event_id": None})
        history = [item if item["id"] != activity_id else record for item in self._activity]
        active_ids = self._protected_activity_ids()
        active_ids.discard(activity_id)
        while sum(item["id"] not in active_ids for item in history) > ACTIVITY_LIMIT:
            history.pop(next(index for index, item in enumerate(history)
                             if item["id"] not in active_ids))
        return tuple(history)

    def _transition(
        self, pending: PendingImport, kind: str, *, event_id: str | None = None,
        remaining: tuple[PendingEvent, ...] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        """Propose a bounded lifecycle change for the same storage transaction."""
        previous = self.get_activity(pending.id)
        transitions = list(previous["transitions"]) if previous else []
        transitions.append({"type": kind, "at": datetime.now(UTC).isoformat(),
                            "event_id": event_id})
        created = (previous.get("created_count", 0) if previous else 0) + (kind == "calendar_created")
        rejected = (previous.get("rejected_count", 0) if previous else 0) + (
            len(pending.events) if kind == "event_rejected" and event_id is None else
            int(kind == "event_rejected")
        )
        record = {
            "id": pending.id, "created_at": pending.created_at,
            "source_kind": pending.source_kind, "source_title": pending.source_title,
            "title": (remaining[0] if remaining else pending.events[0]).draft.title,
            "created_count": created, "rejected_count": rejected,
            "status": (("calendar_write_uncertain" if any(event.status == "write_uncertain" for event in remaining)
                        else "review_ready") if remaining else
                       "calendar_write_uncertain" if kind == "calendar_write_started" else
                       "mixed" if created and rejected else kind),
            "transitions": transitions[-ACTIVITY_TRANSITIONS_PER_IMPORT:],
        }
        history = list(item for item in self._activity if item["id"] != pending.id) + [record]
        active_ids = self._protected_activity_ids()
        if remaining == ():
            active_ids.discard(pending.id)
        elif kind == "review_ready" or remaining:
            active_ids.add(pending.id)
        while sum(item["id"] not in active_ids for item in history) > ACTIVITY_LIMIT:
            oldest_completed = next((index for index, item in enumerate(history)
                                     if item["id"] not in active_ids))
            history.pop(oldest_completed)
        return tuple(history)

    async def async_edit_event(
        self, pending_id: str, event_id: str, draft: EventDraft,
        calendar_entity: str | None = None,
        expected_event: PendingEvent | None = None,
    ) -> PendingEvent | None:
        """Replace a ready draft atomically while preserving its event ID."""
        async with self._lock:
            pending = self._items.get(pending_id)
            if pending is None:
                return None
            event = next((item for item in pending.events if item.id == event_id), None)
            if event is None:
                return None
            if expected_event is not None and event != expected_event:
                raise PendingEventEditError("Event changed since it was loaded; refresh before editing")
            if event.status != "pending":
                raise PendingEventEditError("Uncertain calendar write must be resolved before editing")
            target = calendar_entity or event.calendar_entity
            if event.draft == draft and target == event.calendar_entity:
                return event

            fingerprint = event_fingerprint(draft)
            other_active = {
                event_fingerprint(item.draft)
                for active in self._items.values()
                for item in active.events
                if active.id != pending_id or item.id != event_id
            }
            if fingerprint in self._seen_event_fingerprints or fingerprint in other_active:
                raise PendingEventEditError("Edited event duplicates a pending or handled event")

            edited = PendingEvent(event.id, draft, event.status, target)
            updated = replace(pending, events=tuple(
                edited if item.id == event_id else item for item in pending.events
            ))
            items = dict(self._items)
            items[pending_id] = updated
            activity = tuple({**record, "title": updated.events[0].draft.title}
                             if record["id"] == pending_id else record
                             for record in self._activity)
            await self._async_save(items, activity=activity)
            self._items = items
            self._activity = activity
            return edited

    def is_source_duplicate(self, source_id: str) -> bool:
        """Return whether a source ID is pending, handled, or being processed."""
        return self._source_fingerprint_exists(build_source_fingerprint(source_id))

    def is_source_durable(self, source_id: str) -> bool:
        """Return whether a source ID is durably pending or already handled."""
        fingerprint = build_source_fingerprint(source_id)
        if fingerprint in self._seen_source_fingerprints:
            return True
        return any(
            item.source_fingerprint == fingerprint
            for item in self._items.values()
        )

    async def async_add(
        self,
        *,
        source_text: str,
        events: Iterable[EventDraft],
        source_id: str | None = None,
        calendar_entity: str | None = None,
        source_kind: str = "manual_text",
        source_title: str | None = None,
        warnings: Iterable[str] = (),
        activity_id: str | None = None,
    ) -> PendingImportAddResult:
        """Persist events, letting the storage transaction finish before cancellation."""
        result, cancelled = await self._async_complete_transaction(
            self._async_add_transaction(
                source_text=source_text,
                events=events,
                source_id=source_id,
                calendar_entity=calendar_entity,
                source_kind=source_kind,
                source_title=source_title,
                warnings=warnings,
                activity_id=activity_id,
            )
        )
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _async_add_transaction(
        self,
        *,
        source_text: str,
        events: Iterable[EventDraft],
        source_id: str | None = None,
        calendar_entity: str | None = None,
        source_kind: str = "manual_text",
        source_title: str | None = None,
        warnings: Iterable[str] = (),
        activity_id: str | None = None,
    ) -> PendingImportAddResult:
        """Persist only events not already pending or handled."""
        source_text = source_text.strip()
        if not source_text:
            raise ValueError("source_text must be a non-empty string")

        event_tuple = tuple(events)
        source_fp = (
            build_source_fingerprint(source_id)
            if source_id is not None
            else None
        )

        async with self._lock:
            source_claims = self._source_claims
            source_was_claimed = (
                activity_id is not None and activity_id in source_claims
            )
            if source_was_claimed:
                if source_fp is None or source_claims[activity_id] != source_fp:
                    raise ValueError("source_id does not match claimed source")
                source_claims = {
                    identifier: fingerprint
                    for identifier, fingerprint in source_claims.items()
                    if identifier != activity_id
                }

            if source_fp is not None and self._source_fingerprint_exists(
                source_fp,
                exclude_claim_id=activity_id,
            ):
                activity = self._finish_submission(activity_id, "duplicate")
                if activity_id is not None:
                    await self._async_save(
                        self._items,
                        activity=activity,
                        source_claims=source_claims,
                    )
                    self._activity = activity
                    self._source_claims = source_claims
                return PendingImportAddResult(
                    pending=None,
                    duplicate_source=True,
                    duplicate_events=0,
                )

            known_events = set(self._seen_event_fingerprints)
            known_events.update(self._active_event_fingerprints())
            accepted_events: list[EventDraft] = []
            accepted_fingerprints: set[str] = set()
            duplicate_events = 0

            for event in event_tuple:
                fingerprint = event_fingerprint(event)
                if (
                    fingerprint in known_events
                    or fingerprint in accepted_fingerprints
                ):
                    duplicate_events += 1
                    continue
                accepted_events.append(event)
                accepted_fingerprints.add(fingerprint)

            if not accepted_events:
                status = (
                    "duplicate"
                    if duplicate_events
                    else "no_events"
                    if source_was_claimed
                    else "failed"
                )
                guidance = (
                    "No reviewable events were found. Check the source and submit it again."
                    if status == "failed"
                    else None
                )
                activity = self._finish_submission(activity_id, status, guidance)
                if source_fp is not None and (
                    duplicate_events or source_was_claimed
                ):
                    seen_sources = _remember_fingerprints(
                        self._seen_source_fingerprints,
                        (source_fp,),
                    )
                    await self._async_save(
                        self._items,
                        seen_source_fingerprints=seen_sources,
                        activity=activity,
                        source_claims=source_claims,
                    )
                    self._seen_source_fingerprints = seen_sources
                    self._source_claims = source_claims
                elif activity_id is not None:
                    await self._async_save(
                        self._items,
                        activity=activity,
                        source_claims=source_claims,
                    )
                    self._source_claims = source_claims
                self._activity = activity
                return PendingImportAddResult(
                    pending=None,
                    duplicate_source=False,
                    duplicate_events=duplicate_events,
                )

            pending = PendingImport.create(
                source_text=source_text,
                events=accepted_events,
                source_fingerprint=source_fp,
                calendar_entity=calendar_entity,
                source_kind=source_kind,
                source_title=source_title,
                warnings=warnings,
                duplicate_events=duplicate_events,
                activity_id=activity_id,
                received_at=self.get_activity(activity_id)["created_at"] if activity_id else None,
            )
            items = dict(self._items)
            items[pending.id] = pending
            activity = self._transition(pending, "review_ready")
            await self._async_save(
                items,
                activity=activity,
                source_claims=source_claims,
            )
            self._items = items
            self._activity = activity
            self._source_claims = source_claims
            return PendingImportAddResult(
                pending=pending,
                duplicate_source=False,
                duplicate_events=duplicate_events,
            )

    async def async_remove(self, pending_id: str) -> bool:
        """Reject and remember a pending import if it exists."""
        async with self._lock:
            pending = self._items.get(pending_id)
            if pending is None:
                return False
            if pending.approval_in_flight:
                raise PendingImportApprovalUncertainError(pending_id)

            items = dict(self._items)
            del items[pending_id]
            seen_sources = self._seen_source_fingerprints
            if pending.source_fingerprint is not None:
                seen_sources = _remember_fingerprints(
                    seen_sources,
                    (pending.source_fingerprint,),
                )
            seen_events = _remember_fingerprints(
                self._seen_event_fingerprints,
                (event_fingerprint(event.draft) for event in pending.events),
            )

            activity = self._transition(pending, "event_rejected", remaining=())
            await self._async_save(
                items,
                seen_source_fingerprints=seen_sources,
                seen_event_fingerprints=seen_events,
                activity=activity,
            )
            self._items = items
            self._activity = activity
            self._seen_source_fingerprints = seen_sources
            self._seen_event_fingerprints = seen_events
        return True

    async def async_reject_event(
        self, pending_id: str, event_id: str,
        expected_event: PendingEvent | None = None,
    ) -> bool:
        """Reject one ready event without discarding its siblings."""
        async with self._lock:
            pending = self._items.get(pending_id)
            if pending is None:
                return False
            event = next((item for item in pending.events if item.id == event_id), None)
            if event is None:
                return False
            if expected_event is not None and event != expected_event:
                raise PendingEventEditError("Event changed since it was loaded; refresh before deciding")
            if event.status == "write_uncertain":
                raise PendingImportApprovalUncertainError(pending_id)

            remaining = tuple(item for item in pending.events if item.id != event_id)
            items = dict(self._items)
            seen_sources = self._seen_source_fingerprints
            if remaining:
                items[pending_id] = replace(pending, events=remaining)
            else:
                del items[pending_id]
                if pending.source_fingerprint is not None:
                    seen_sources = _remember_fingerprints(
                        seen_sources, (pending.source_fingerprint,)
                    )
            seen_events = _remember_fingerprints(
                self._seen_event_fingerprints, (event_fingerprint(event.draft),)
            )
            activity = self._transition(pending, "event_rejected", event_id=event_id,
                                        remaining=remaining)
            await self._async_save(
                items,
                seen_source_fingerprints=seen_sources,
                seen_event_fingerprints=seen_events,
                activity=activity,
            )
            self._items = items
            self._activity = activity
            self._seen_source_fingerprints = seen_sources
            self._seen_event_fingerprints = seen_events
            return True

    async def async_resolve_uncertain(
        self, pending_id: str, event_id: str, resolution: str,
        expected_event: PendingEvent | None = None,
    ) -> bool:
        """Apply an explicit user-confirmed outcome to one uncertain write."""
        if resolution not in ("created", "not_created", "discard"):
            raise ValueError("invalid uncertain-write resolution")
        async with self._lock:
            pending = self._items.get(pending_id)
            if pending is None:
                return False
            event = next((item for item in pending.events if item.id == event_id), None)
            if event is None:
                return False
            if expected_event is not None and event != expected_event:
                raise PendingEventResolutionError("Event changed since it was loaded; refresh before resolving")
            if event.status != "write_uncertain":
                raise PendingEventResolutionError("Event has no uncertain calendar write")

            items = dict(self._items)
            seen_sources = self._seen_source_fingerprints
            seen_events = self._seen_event_fingerprints
            if resolution == "not_created":
                remaining = tuple(
                    PendingEvent(item.id, item.draft, "pending", item.calendar_entity)
                    if item.id == event_id else item for item in pending.events
                )
            else:
                remaining = tuple(item for item in pending.events if item.id != event_id)
                seen_events = _remember_fingerprints(
                    seen_events, (event_fingerprint(event.draft),)
                )

            if remaining:
                items[pending_id] = replace(pending, events=remaining)
            else:
                del items[pending_id]
                if pending.source_fingerprint is not None:
                    seen_sources = _remember_fingerprints(
                        seen_sources, (pending.source_fingerprint,)
                    )
            transition = {"created": "calendar_created", "not_created": "review_ready",
                          "discard": "event_rejected"}[resolution]
            activity = self._transition(pending, transition, event_id=event_id,
                                        remaining=remaining)
            await self._async_save(
                items, seen_source_fingerprints=seen_sources,
                seen_event_fingerprints=seen_events,
                activity=activity,
            )
            self._items = items
            self._activity = activity
            self._seen_source_fingerprints = seen_sources
            self._seen_event_fingerprints = seen_events
            return True

    async def async_process_events(
        self,
        pending_id: str,
        processor: Callable[[PendingEvent], Awaitable[None]],
    ) -> PendingImport | None:
        """Process pending events with durable checkpoints around each side effect."""
        async with self._lock:
            pending = self._items.get(pending_id)
            if pending is None:
                return None
            if pending.approval_in_flight:
                raise PendingImportApprovalUncertainError(pending_id)

            original = pending
            while pending is not None:
                event = pending.events[0]
                pending = await self._async_approve_event_locked(pending, event, processor)

            return original

    async def async_approve_event(
        self, pending_id: str, event_id: str,
        processor: Callable[[PendingEvent], Awaitable[None]],
        expected_event: PendingEvent | None = None,
    ) -> PendingEvent | None:
        """Approve one ready event without approving its siblings."""
        async with self._lock:
            pending = self._items.get(pending_id)
            if pending is None:
                return None
            event = next((item for item in pending.events if item.id == event_id), None)
            if event is None:
                return None
            if expected_event is not None and event != expected_event:
                raise PendingEventEditError("Event changed since it was loaded; refresh before deciding")
            if event.status == "write_uncertain":
                raise PendingImportApprovalUncertainError(pending_id)
            await self._async_approve_event_locked(pending, event, processor)
            return event

    async def _async_approve_event_locked(
        self, pending: PendingImport, event: PendingEvent,
        processor: Callable[[PendingEvent], Awaitable[None]],
    ) -> PendingImport | None:
        """Checkpoint the selected event around its external calendar write."""
        in_flight = replace(pending, events=tuple(
                PendingEvent(item.id, item.draft, "write_uncertain", item.calendar_entity,
                             str(uuid4()))
                if item.id == event.id else item
                for item in pending.events
            ))
        items = dict(self._items)
        items[pending.id] = in_flight
        activity = self._transition(pending, "calendar_write_started", event_id=event.id)
        await self._async_save(items, activity=activity)
        self._items = items
        self._activity = activity

        try:
            await processor(event)
        except Exception:
            uncertain = self._transition(pending, "calendar_write_uncertain", event_id=event.id)
            try:
                await self._async_save(self._items, activity=uncertain)
            except Exception:
                # The write-started checkpoint already records an uncertain status.
                pass
            else:
                self._activity = uncertain
            raise

        remaining = tuple(item for item in pending.events if item.id != event.id)
        items = dict(self._items)
        seen_sources = self._seen_source_fingerprints
        seen_events = _remember_fingerprints(
            self._seen_event_fingerprints, (event_fingerprint(event.draft),)
        )
        if remaining:
            updated = replace(pending, events=remaining)
            items[pending.id] = updated
        else:
            updated = None
            del items[pending.id]
            if pending.source_fingerprint is not None:
                seen_sources = _remember_fingerprints(
                    seen_sources, (pending.source_fingerprint,)
                )

        activity = self._transition(pending, "calendar_created", event_id=event.id,
                                    remaining=remaining)
        await self._async_save(
            items,
            seen_source_fingerprints=seen_sources,
            seen_event_fingerprints=seen_events,
            activity=activity,
        )
        self._items = items
        self._activity = activity
        self._seen_source_fingerprints = seen_sources
        self._seen_event_fingerprints = seen_events
        return updated

    async def async_remove_storage(self) -> None:
        """Remove the backing storage file."""
        await self._store.async_remove()

    def _source_fingerprint_exists(
        self,
        fingerprint: str,
        *,
        exclude_claim_id: str | None = None,
    ) -> bool:
        if fingerprint in self._seen_source_fingerprints:
            return True
        if any(
            item.source_fingerprint == fingerprint
            for item in self._items.values()
        ):
            return True
        return any(
            claim_fingerprint == fingerprint and claim_id != exclude_claim_id
            for claim_id, claim_fingerprint in self._source_claims.items()
        )

    def _active_event_fingerprints(self) -> set[str]:
        return {
            event_fingerprint(event.draft)
            for item in self._items.values()
            for event in item.events
        }

    async def _async_save(
        self,
        items: dict[str, PendingImport],
        *,
        seen_source_fingerprints: tuple[str, ...] | None = None,
        seen_event_fingerprints: tuple[str, ...] | None = None,
        activity: tuple[dict[str, Any], ...] | None = None,
        source_claims: dict[str, str] | None = None,
    ) -> None:
        """Persist a proposed collection and deduplication history."""
        seen_sources = (
            self._seen_source_fingerprints
            if seen_source_fingerprints is None
            else seen_source_fingerprints
        )
        seen_events = (
            self._seen_event_fingerprints
            if seen_event_fingerprints is None
            else seen_event_fingerprints
        )
        data: dict[str, Any] = {
            _STORAGE_ITEMS: [item.as_dict() for item in items.values()]
        }
        if seen_sources:
            data[_STORAGE_SEEN_SOURCES] = list(seen_sources)
        if seen_events:
            data[_STORAGE_SEEN_EVENTS] = list(seen_events)
        claims = self._source_claims if source_claims is None else source_claims
        if claims:
            data[_STORAGE_SOURCE_CLAIMS] = dict(claims)
        history = self._activity if activity is None else activity
        if history:
            data[_STORAGE_ACTIVITY] = list(history)
        await self._store.async_save(data)


def _remember_fingerprints(
    existing: tuple[str, ...],
    additions: Iterable[str],
) -> tuple[str, ...]:
    """Append unique fingerprints and keep only the bounded newest history."""
    new_values = tuple(dict.fromkeys(additions))
    if not new_values:
        return existing

    new_set = set(new_values)
    combined = tuple(
        fingerprint
        for fingerprint in existing
        if fingerprint not in new_set
    ) + new_values
    return combined[-DEDUP_HISTORY_LIMIT:]
