"""Persistent storage for pending calendar imports."""

from __future__ import annotations

import asyncio
import logging
import json
import re
from hashlib import sha256
from copy import deepcopy
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any
from pathlib import Path
from uuid import uuid4

from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store
from homeassistant.util.json import json_loads

from .const import DOMAIN
from .dedup import (
    event_fingerprint,
    source_fingerprint as build_source_fingerprint,
)
from .models import EventDraft, normalize_date_time_assumptions
from .notification_journal import NotificationRecord, notification_record
from .notification_preferences import NotificationPreferences, DELIVERABLE_NOTIFICATION_CLASSES
from .source_routing import LEGACY_UNRESOLVED_WARNINGS
from .provider_failures import PROVIDER_FAILURE_GUIDANCE

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 2
STORAGE_KEY = f"{DOMAIN}.pending_imports"
DEDUP_HISTORY_LIMIT = 10_000
ACTIVITY_LIMIT = 500
ACTIVITY_TRANSITIONS_PER_IMPORT = 32
COMPLETED_HISTORY_DAYS = 30
TERMINAL_NOTIFICATION_LIMIT = 1_000
_STORAGE_ITEMS = "items"
_STORAGE_ACTIVITY = "activity"
_STORAGE_NOTIFICATIONS = "notifications"
_STORAGE_SOURCE_CLAIMS = "source_claims"
_STORAGE_SEEN_SOURCES = "seen_source_fingerprints"
_STORAGE_SEEN_EVENTS = "seen_event_fingerprints"


class PendingImportStorageError(RuntimeError):
    """Local data needs repair; preserve it and refuse further writes."""


_STORAGE_ERROR = "Daylight import storage needs repair. Preserve the original file and restore a known-good backup with Home Assistant stopped."


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
    routing_unresolved: bool = False
    # Metadata is advisory, not part of the optimistic concurrency identity.
    date_time_assumptions: tuple[str, ...] = field(default=(), compare=False)

    @classmethod
    def create(
        cls, draft: EventDraft, calendar_entity: str | None = None,
        *, routing_unresolved: bool = False,
        date_time_assumptions: tuple[str, ...] = (),
    ) -> PendingEvent:
        return cls(str(uuid4()), draft, calendar_entity=calendar_entity,
                   routing_unresolved=routing_unresolved,
                   date_time_assumptions=date_time_assumptions)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PendingEvent:
        if raw["status"] not in ("pending", "write_uncertain"):
            raise ValueError("invalid pending event status")
        # Optional AI disclosures must never make durable calendar decisions
        # inaccessible after a restart. Only discard malformed advisory data.
        try:
            assumptions = normalize_date_time_assumptions(
                raw.get("date_time_assumptions", ())
            )
        except ValueError:
            assumptions = ()
        return cls(raw["id"], EventDraft.from_mapping(raw["draft"]), raw["status"],
                   raw.get("calendar_entity"), raw.get("write_attempt"),
                   raw.get("routing_unresolved", False), assumptions)

    def as_dict(self) -> dict[str, Any]:
        result = {"id": self.id, "draft": self.draft.as_dict(), "status": self.status,
                  "calendar_entity": self.calendar_entity}
        if self.write_attempt is not None:
            result["write_attempt"] = self.write_attempt
        # Explicit False distinguishes confirmed legacy events from records
        # that predate the per-event routing flag entirely.
        result["routing_unresolved"] = self.routing_unresolved
        if self.date_time_assumptions:
            result["date_time_assumptions"] = list(self.date_time_assumptions)
        return result

    def as_service_dict(self) -> dict[str, Any]:
        """Expose the stable ID alongside the existing flat draft fields."""
        result = {**self.draft.as_dict(), "id": self.id, "status": self.status,
                  "calendar_entity": self.calendar_entity}
        if self.write_attempt is not None:
            result["write_attempt"] = self.write_attempt
        if self.routing_unresolved:
            result["routing_unresolved"] = True
        if self.date_time_assumptions:
            result["date_time_assumptions"] = list(self.date_time_assumptions)
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


def _read_pending_storage(path: str) -> dict[str, Any] | None:
    """Validate original bytes before HA can rename corrupt JSON and load empty."""
    storage_path = Path(path)
    try:
        content = storage_path.read_bytes()
    except FileNotFoundError:
        if any(storage_path.parent.glob(f"{storage_path.name}.corrupt.*")):
            raise PendingImportStorageError(_STORAGE_ERROR) from None
        return None

    def unique_object(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate storage key")
            result[key] = value
        return result

    envelope = json.loads(content, object_pairs_hook=unique_object)
    # HA's strict parser also rejects non-finite numbers and invalid Unicode.
    json_loads(content)
    return _validate_pending_envelope(envelope)


def _validate_pending_envelope(envelope: Any) -> dict[str, Any]:
    """Validate both HA pending data and original file envelopes."""
    if (not isinstance(envelope, dict) or type(envelope.get("version")) is not int
            or envelope["version"] not in (1, STORAGE_VERSION)
            or type(envelope.get("minor_version", 1)) is not int
            or envelope.get("minor_version", 1) != 1
            or envelope.get("key", STORAGE_KEY) != STORAGE_KEY):
        raise PendingImportStorageError(_STORAGE_ERROR)
    data = envelope["data"]
    _decode_storage_data(_migrate_v1(data) if envelope["version"] == 1 else data)
    return envelope


class _PendingStore(Store[dict[str, Any]]):
    """Migrate existing Home Assistant Store data before it is loaded."""

    async def _async_load_data(self) -> dict[str, Any] | None:
        """Inspect original data under HA's concurrent-load guard."""
        temporary = self._data is None
        envelope = None
        try:
            envelope = (_validate_pending_envelope(deepcopy(self._data)) if not temporary
                        else await self.hass.async_add_executor_job(_read_pending_storage, self.path))
            if envelope is None:
                return None
            # Use validated original bytes for HA's version/migration path.
            # A legitimate pending write already in _data keeps its ownership.
            if temporary:
                self._data = envelope
            data = await super()._async_load_data()
            if data is None:
                raise PendingImportStorageError(_STORAGE_ERROR)
            return data
        except Exception:
            raise PendingImportStorageError(_STORAGE_ERROR) from None
        finally:
            # Do not cache this read across reloads, and never discard a new
            # queued migration/write produced while HA was loading the envelope.
            if temporary and self._data is envelope:
                self._data = None

    async def _async_migrate_func(
        self, old_major_version: int, old_minor_version: int, old_data: dict[str, Any]
    ) -> dict[str, Any]:
        if old_major_version != 1:
            raise ValueError(f"Unsupported pending storage version: {old_major_version}")
        migrated = _migrate_v1(old_data)
        _decode_storage_data(migrated)
        return migrated


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
    source_sender: str | None = None
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
        routing_unresolved: bool = False,
        source_kind: str = "manual_text",
        source_title: str | None = None,
        source_sender: str | None = None,
        warnings: Iterable[str] = (),
        event_assumptions: Iterable[tuple[str, ...]] | None = None,
        duplicate_events: int = 0,
        activity_id: str | None = None,
        received_at: str | None = None,
    ) -> PendingImport:
        """Create a new pending import with stable persisted metadata."""
        source_text = source_text.strip()
        if not source_text:
            raise ValueError("source_text must be a non-empty string")

        drafts = tuple(events)
        assumptions = (tuple(event_assumptions) if event_assumptions is not None
                       else ((),) * len(drafts))
        if len(assumptions) != len(drafts):
            raise ValueError("event assumptions must match the event count")
        event_tuple = tuple(PendingEvent.create(event, calendar_entity,
                                              routing_unresolved=routing_unresolved,
                                              date_time_assumptions=notes)
                            for event, notes in zip(drafts, assumptions, strict=True))
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
            source_sender=source_sender,
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
            events=tuple(
                replace(PendingEvent.from_dict(event), routing_unresolved=True)
                if (
                    "routing_unresolved" not in event
                    and any(warning in LEGACY_UNRESOLVED_WARNINGS
                            for warning in raw.get("warnings", ()))
                ) else PendingEvent.from_dict(event)
                for event in raw["events"]
            ),
            source_fingerprint=raw.get("source_fingerprint"),
            source_kind=raw.get("source_kind", "manual_text"),
            source_title=raw.get("source_title"),
            source_sender=raw.get("source_sender"),
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
        if self.source_sender is not None:
            result["source_sender"] = self.source_sender
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
        result.update(
            source_kind=self.source_kind,
            source_title=self.source_title,
            warnings=list(self.warnings),
            duplicate_events=self.duplicate_events,
        )
        result["events"] = [event.as_service_dict() for event in self.events]
        result["approval_in_flight"] = self.approval_in_flight
        return result

    @property
    def approval_in_flight(self) -> bool:
        """Report an uncertain write anywhere in the import."""
        return any(event.status == "write_uncertain" for event in self.events)


def _decode_storage_data(data: Any) -> tuple:
    """Decode all durable collections before installing any of the snapshot."""
    fields = {_STORAGE_ITEMS, _STORAGE_ACTIVITY, _STORAGE_NOTIFICATIONS,
              _STORAGE_SOURCE_CLAIMS, _STORAGE_SEEN_SOURCES, _STORAGE_SEEN_EVENTS}
    if not isinstance(data, dict) or set(data) - fields:
        raise ValueError("unsupported pending storage envelope")

    def rows(key):
        value = data[key] if key == _STORAGE_ITEMS else data.get(key, ())
        if not isinstance(value, (list, tuple)):
            raise ValueError("invalid storage collection")
        return value

    def text(value):
        if not isinstance(value, str) or not value.strip():
            raise ValueError("invalid storage identity")

    def unique(values):
        if len(values) != len(set(values)):
            raise ValueError("duplicate storage identity")

    def stored_fingerprint(value: str, kind: str) -> None:
        # The only historically persisted format is a v1 SHA-256 fingerprint.
        # Keep this explicit if future formats are introduced so older evidence
        # remains supported; accepting opaque strings would silently defeat dedupe.
        if re.fullmatch(rf"v1:{kind}:[0-9a-f]{{64}}", value) is None:
            raise ValueError("invalid stored fingerprint")

    item_rows = rows(_STORAGE_ITEMS)
    if any(not isinstance(raw.get("warnings", ()), (list, tuple)) for raw in item_rows):
        raise ValueError("invalid import warnings")
    loaded_items = tuple(PendingImport.from_dict(raw) for raw in item_rows)
    unique([item.id for item in loaded_items])
    for item in loaded_items:
        text(item.id)
        text(item.created_at)
        text(item.source_kind)
        if not isinstance(item.source_text, str) or not item.events:
            raise ValueError("invalid pending import")
        if (any(not isinstance(warning, str) for warning in item.warnings)
                or type(item.duplicate_events) is not int or item.duplicate_events < 0):
            raise ValueError("invalid source metadata")
        for value in (item.source_fingerprint, item.source_title, item.source_sender):
            if value is not None and not isinstance(value, str):
                raise ValueError("invalid source metadata")
        if item.source_fingerprint is not None:
            stored_fingerprint(item.source_fingerprint, "source")
        unique([event.id for event in item.events])
        for event in item.events:
            text(event.id)
            if type(event.routing_unresolved) is not bool:
                raise ValueError("invalid routing checkpoint")
            for value in (event.calendar_entity, event.write_attempt):
                if value is not None:
                    text(value)

    activity = deepcopy(rows(_STORAGE_ACTIVITY))
    unique([record["id"] for record in activity])
    for record in activity:
        text(record["id"])
        text(record["status"])
        # Unknown statuses must not become eligible for retention pruning.
        # Each value below is produced by the lifecycle transaction methods.
        if record["status"] not in {
            "discovered", "processing", "failed", "duplicate", "no_events",
            "review_ready", "source_reprocessed", "event_rejected", "mixed",
            "calendar_write_started", "calendar_write_uncertain",
            "calendar_created", "calendar_create_failed",
        }:
            raise ValueError("invalid lifecycle status")
        transitions = record["transitions"]
        if not isinstance(transitions, (list, tuple)) or any(not isinstance(row, dict) for row in transitions):
            raise ValueError("invalid lifecycle history")
        record["transitions"] = list(transitions)
        for name in ("created_count", "rejected_count"):
            if type(record.get(name, 0)) is not int or record.get(name, 0) < 0:
                raise ValueError("invalid lifecycle count")
    journal = tuple(NotificationRecord.from_dict(raw) for raw in rows(_STORAGE_NOTIFICATIONS))
    unique([record.event.idempotency_key for record in journal])
    histories = []
    for key, kind in ((_STORAGE_SEEN_SOURCES, "source"), (_STORAGE_SEEN_EVENTS, "event")):
        fingerprints = rows(key)
        for fingerprint in fingerprints:
            text(fingerprint)
            stored_fingerprint(fingerprint, kind)
        histories.append(tuple(fingerprints))
    claims = data.get(_STORAGE_SOURCE_CLAIMS, {})
    if not isinstance(claims, dict):
        raise ValueError("invalid source claims")
    for identifier, fingerprint in claims.items():
        text(identifier)
        text(fingerprint)
        stored_fingerprint(fingerprint, "source")
    return {item.id: item for item in loaded_items}, tuple(activity), journal, *histories, dict(claims)


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
        self._storage_blocked = False
        self._items: dict[str, PendingImport] = {}
        self._activity: tuple[dict[str, Any], ...] = ()
        self._notifications: tuple[NotificationRecord, ...] = ()
        self.notification_preferences: Callable[[], NotificationPreferences] = NotificationPreferences
        self.notification_delivery_lock = asyncio.Lock()
        self.on_notifications: Callable[[], None] | None = None
        self._seen_source_fingerprints: tuple[str, ...] = ()
        self._seen_event_fingerprints: tuple[str, ...] = ()
        self._source_claims: dict[str, str] = {}
        self._source_claim_releases: set[str] = set()
        self._source_claim_release_provider_codes: dict[str, str] = {}
        self._reprocessing: set[str] = set()
        self.processing_source_fingerprints: dict[asyncio.Task, str] = {}
        self._lock = asyncio.Lock()
        self._subscribers: set[Callable[[], None]] = set()
        self.on_review_ready: Callable[[PendingImport], None] | None = None

    def async_subscribe(self, callback: Callable[[], None]) -> Callable[[], None]:
        """Listen for committed queue snapshots; unsubscribe on entity unload."""
        self._subscribers.add(callback)

        def unsubscribe() -> None:
            self._subscribers.discard(callback)

        return unsubscribe

    def _commit_items(self, items: dict[str, PendingImport]) -> None:
        """Install persisted data and notify consumers only after storage succeeds."""
        self._items = items
        for callback in tuple(self._subscribers):
            try:
                callback()
            except Exception:
                _LOGGER.exception("Pending queue subscriber failed")

    async def async_load(self) -> None:
        """Load pending imports and deduplication history."""
        try:
            data = await self._store.async_load()
            if data is None:
                if any((self._items, self._activity, self._notifications, self._source_claims,
                        self._seen_source_fingerprints, self._seen_event_fingerprints)):
                    raise PendingImportStorageError(_STORAGE_ERROR)
                snapshot = ({}, (), (), (), (), {})
            else:
                snapshot = _decode_storage_data(data)
        except Exception:
            self._storage_blocked = True
            self.accepting_services = False
            raise PendingImportStorageError(_STORAGE_ERROR) from None

        (self._items, self._activity, self._notifications, self._seen_source_fingerprints,
         self._seen_event_fingerprints, self._source_claims) = snapshot
        self._storage_blocked = False
        self._source_claim_releases = set()
        self._source_claim_release_provider_codes = {}
        if data is None:
            return
        interrupted = [
            item["id"]
            for item in self._activity
            if item["status"] in ("discovered", "processing")
        ]
        activity = self._activity
        recovered_failures = []
        for activity_id in interrupted:
            activity = self._finish_submission(
                activity_id, "failed", "Processing was interrupted. Check the source and submit it again.",
                activity=activity,
            )
            # Each newly completed source is newest in the proposed history.
            # Keep its transition even if later recoveries prune that history.
            recovered_failures.append(activity[-1])
        if interrupted or self._source_claims:
            async def commit_recovery() -> None:
                journal = self._journal_for_activity(tuple(recovered_failures))
                await self._async_save(self._items, activity=activity, source_claims={},
                                       notifications=journal)
                self._source_claims = {}

            _, cancelled = await self._async_complete_transaction(commit_recovery())
            if cancelled:
                raise asyncio.CancelledError

        await self._async_recover_uncertain_notifications()
        await self.async_apply_retention()

    async def _async_commit_activity(self, activity: tuple[dict[str, Any], ...]) -> None:
        """Finish outcome persistence/publication before propagating cancellation."""
        async def commit() -> None:
            await self._async_save(self._items, activity=activity)

        _, cancelled = await self._async_complete_transaction(commit())
        if cancelled:
            raise asyncio.CancelledError

    async def _async_recover_uncertain_notifications(self) -> None:
        """Observe durable event checkpoints, independently of prunable history.

        One request per write attempt (including legacy unknown attempts). A
        restart never turns an ambiguous write into a confirmed failure/success.
        This helper runs during load or while the caller owns the store lock.
        """
        policy = self.notification_preferences()
        if not policy.permits("calendar_write_uncertain"):
            return
        journal = self._notifications
        known = {(record.import_id, record.event_id, record.write_attempt)
                 for record in journal if record.transition_type == "calendar_write_uncertain"}
        for pending in self._items.values():
            for event in pending.events:
                identity = (pending.id, event.id, event.write_attempt)
                if event.status != "write_uncertain" or identity in known:
                    continue
                request = NotificationRecord(
                    pending.id, "calendar_write_uncertain", datetime.now(UTC).isoformat(), event.id,
                    target=policy.target, write_attempt=event.write_attempt, recovered=True,
                )
                NotificationRecord.from_dict(request.as_dict())
                journal += (request,)
                known.add(identity)
        if journal != self._notifications:
            await self._async_save(self._items, notifications=journal)

    async def async_apply_retention(self) -> None:
        """Prune completed history atomically, without touching unresolved work."""
        async def commit() -> None:
            async with self._lock:
                history, journal = self._retained_history(
                    self._items, self._activity, self._source_claims, self._notifications,
                )
                if history != self._activity or journal != self._notifications:
                    await self._async_save(self._items, activity=history, notifications=journal)

        _, cancelled = await self._async_complete_transaction(commit())
        if cancelled:
            raise asyncio.CancelledError

    def _retained_history(
        self, items: dict[str, PendingImport], history: tuple[dict[str, Any], ...],
        claims: dict[str, str], journal: tuple[NotificationRecord, ...],
    ) -> tuple[tuple[dict[str, Any], ...], tuple[NotificationRecord, ...]]:
        """Protect active identities; expire only timestamped completed history."""
        now = datetime.now(UTC)
        cutoff = now - timedelta(days=COMPLETED_HISTORY_DAYS)
        active_provider_names = {task.get_name() for task in getattr(self, "notification_tasks", ())
                                 if not task.done()}
        protected = set(items) | set(claims) | self._source_claim_releases | {
            item["id"] for item in history if item.get("status") in ("discovered", "processing")
        } | {record.import_id for record in journal if record.status == "pending"
             or f"Daylight notify {record.event.idempotency_key}" in active_provider_names}

        def expired(timestamp: str | None) -> bool:
            # Malformed legacy metadata must not authorize destructive cleanup.
            try:
                parsed = datetime.fromisoformat(timestamp)
            except (TypeError, ValueError):
                return False
            return parsed.tzinfo is not None and parsed < cutoff

        history = tuple(item for item in history if item["id"] in protected or not expired(
            item["transitions"][-1].get("at") if item.get("transitions") else None,
        ))
        completed = [item["id"] for item in history if item["id"] not in protected]
        remove = set(completed[:max(0, len(completed) - ACTIVITY_LIMIT)])
        history = tuple(item for item in history if item["id"] not in remove)
        journal = tuple(
            replace(record, terminal_at=now.isoformat())
            if record.status != "pending" and record.terminal_at is None else record
            for record in journal
        )
        journal = tuple(record for record in journal if record.import_id in protected
                        or record.status == "pending" or not expired(record.terminal_at))
        completed_records = sorted(
            (record for record in journal
             if record.status != "pending" and record.import_id not in protected),
            key=lambda record: datetime.fromisoformat(record.terminal_at),
        )
        remove_keys = {record.event.idempotency_key for record in completed_records[
            :max(0, len(completed_records) - TERMINAL_NOTIFICATION_LIMIT)]}
        journal = tuple(record for record in journal if record.event.idempotency_key not in remove_keys)
        return history, journal

    def list_notifications(self) -> tuple[NotificationRecord, ...]:
        """Return immutable requests, unaffected by pending/activity pruning."""
        return self._notifications

    async def async_enqueue_notification(self, record: NotificationRecord) -> bool:
        """Persist an identity once; publish it only after the atomic save succeeds.

        Later lifecycle producers use the same envelope to save their outcome
        and proposed journal together. This standalone API does not infer or
        replay notifications from historical activity.
        """
        NotificationRecord.from_dict(record.as_dict())
        result, cancelled = await self._async_complete_transaction(
            self._async_enqueue_notification_transaction(record)
        )
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _async_enqueue_notification_transaction(self, record: NotificationRecord) -> bool:
        """Finish the durable save before caller cancellation is propagated."""
        key = record.event.idempotency_key
        async with self._lock:
            if any(item.event.idempotency_key == key for item in self._notifications):
                return False
            await self._async_save(self._items, notifications=self._notifications + (record,))
            return True

    async def async_update_notification(
        self, expected: NotificationRecord, updated: NotificationRecord,
    ) -> bool:
        """Commit delivery state using an immutable optimistic snapshot."""
        if expected.event.idempotency_key != updated.event.idempotency_key:
            raise ValueError("Notification identity cannot change")
        NotificationRecord.from_dict(updated.as_dict())
        result, cancelled = await self._async_complete_transaction(
            self._async_update_notification_transaction(expected, updated)
        )
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _async_update_notification_transaction(
        self, expected: NotificationRecord, updated: NotificationRecord,
    ) -> bool:
        async with self._lock:
            if expected not in self._notifications:
                return False
            if updated.status != "pending":
                updated = replace(updated, terminal_at=(expected.terminal_at
                    if expected.status != "pending" and expected.terminal_at is not None
                    else datetime.now(UTC).isoformat()))
            journal = tuple(updated if record == expected else record for record in self._notifications)
            activity = self._activity
            if updated.status == "failed" and expected.status != "failed":
                previous = self.get_activity(updated.import_id)
                if previous is not None:
                    previous["transitions"].append({
                        "type": "notification_delivery_failed", "at": updated.terminal_at,
                        "event_id": updated.event_id,
                    })
                    previous["transitions"] = previous["transitions"][-ACTIVITY_TRANSITIONS_PER_IMPORT:]
                    # Preserve the source/calendar outcome. Delivery failure is
                    # advisory and commits atomically with the notification state.
                    activity = tuple(item for item in activity if item["id"] != updated.import_id) + (previous,)
            await self._async_save(self._items, notifications=journal, activity=activity)
            # Log inside the uncancelled transaction: caller cancellation can
            # follow this durable commit before a delivery wrapper resumes.
            if updated.status == "failed" and expected.status != "failed":
                _LOGGER.warning("Daylight notification retry budget exhausted; verify the configured notify entity")
            return True

    async def async_record_conflict(
        self, pending_id: str, expected_event: PendingEvent, observed_calendars: Iterable[str],
    ) -> bool:
        """Record an authorized completed observation without changing lifecycle state.

        The service producer supplies only actionable results, after successful
        permission-checked reads. No observed event details enter storage. Return
        false for an obsolete pending snapshot, including while waiting for the
        storage lock. One request per exact event/scope revision survives reload.
        """
        result, cancelled = await self._async_complete_transaction(
            self._async_record_conflict_transaction(pending_id, expected_event, tuple(observed_calendars))
        )
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _async_record_conflict_transaction(
        self, pending_id: str, expected_event: PendingEvent, observed_calendars: tuple[str, ...],
    ) -> bool:
        async with self._lock:
            if self.get_event(pending_id, expected_event.id) != expected_event:
                return False
            policy = self.notification_preferences()
            if not policy.permits("conflict_detected"):
                return True
            revision = sha256(json.dumps(
                [expected_event.as_dict(), sorted(set(observed_calendars))],
                sort_keys=True, separators=(",", ":"),
            ).encode()).hexdigest()
            if any(record.event.type == "conflict_detected" and record.import_id == pending_id
                   and record.event_id == expected_event.id and record.conflict_revision == revision
                   for record in self._notifications):
                return True
            request = NotificationRecord(
                pending_id, "conflict_detected", datetime.now(UTC).isoformat(), expected_event.id,
                target=policy.target, conflict_revision=revision,
            )
            await self._async_save(self._items, notifications=self._notifications + (request,))
            return True

    def _journal_for_activity(self, activity: tuple[dict[str, Any], ...]) -> tuple[NotificationRecord, ...]:
        """Enqueue newly committed supported outcomes only with live consent."""
        policy = self.notification_preferences()
        journal = self._notifications
        known = {record.event.idempotency_key for record in journal}
        previous = {record["id"]: record for record in self._activity}
        for record in activity:
            transitions = record.get("transitions", ())
            if not transitions:
                continue
            transition = transitions[-1]
            request = notification_record(record, transition)
            if (request is None or request.event.type not in DELIVERABLE_NOTIFICATION_CLASSES
                    or (request.event.type == "review_ready" and request.event_id is not None)
                    or transition in previous.get(record["id"], {}).get("transitions", ())):
                continue
            if request.event.type == "calendar_write_uncertain":
                checkpoint = self.get_event(request.import_id, request.event_id)
                if checkpoint is None or checkpoint.status != "write_uncertain":
                    continue
                request = replace(request, write_attempt=checkpoint.write_attempt)
            if policy.permits(request.event.type):
                key = request.event.idempotency_key
                if key not in known:
                    journal += (replace(request, target=policy.target),)
                    known.add(key)
        return journal

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
            self._source_claims = source_claims
            return identifier

    async def async_record_parse_failure(
        self, activity_id: str, *, provider_error_code: str | None = None,
    ) -> None:
        """Finalize a failed parse while keeping the original source private."""
        if provider_error_code is None:
            await self.async_record_source_failure(
                activity_id, PROVIDER_FAILURE_GUIDANCE["provider_error"],
            )
            return
        code = (provider_error_code if isinstance(provider_error_code, str)
                and provider_error_code in PROVIDER_FAILURE_GUIDANCE
                else "provider_error")
        _, cancelled = await self._async_complete_transaction(
            self._async_record_source_failure_transaction(
                activity_id, PROVIDER_FAILURE_GUIDANCE[code], provider_error_code=code,
            )
        )
        if cancelled:
            raise asyncio.CancelledError

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
        *, provider_error_code: str | None = None,
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
                if provider_error_code is not None:
                    self._source_claim_release_provider_codes.setdefault(
                        activity_id, provider_error_code,
                    )
            await self._async_record_parse_failure_locked(
                activity_id,
                guidance=guidance,
                provider_error_code=provider_error_code,
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
            self._seen_source_fingerprints = seen_sources
            self._source_claims = source_claims
            self._source_claim_releases.discard(activity_id)
            self._source_claim_release_provider_codes.pop(activity_id, None)

    async def _async_record_parse_failure_locked(
        self,
        activity_id: str,
        *,
        guidance: str = (
            "Parsing failed. Check the configured AI Task and submit the source again."
        ),
        provider_error_code: str | None = None,
    ) -> None:
        """Persist one failure transition and release its source claim."""
        activity = self._finish_submission(
            activity_id,
            "failed",
            guidance,
        )
        if provider_error_code is not None:
            activity = tuple(
                {**record, "failure_code": provider_error_code}
                if record["id"] == activity_id else record
                for record in activity
            )
        source_claims = dict(self._source_claims)
        source_claims.pop(activity_id, None)
        await self._async_save(
            self._items,
            activity=activity,
            source_claims=source_claims,
        )
        self._source_claims = source_claims
        self._source_claim_releases.discard(activity_id)
        self._source_claim_release_provider_codes.pop(activity_id, None)

    async def _async_retry_source_claim_releases_locked(self) -> None:
        """Retry claim releases that previously failed to persist."""
        for activity_id in tuple(self._source_claim_releases):
            code = self._source_claim_release_provider_codes.get(activity_id)
            await self._async_record_parse_failure_locked(
                activity_id,
                guidance=PROVIDER_FAILURE_GUIDANCE[code or "provider_error"],
                provider_error_code=code,
            )

    def _finish_submission(self, activity_id: str | None, status: str,
                           guidance: str | None = None, *,
                           activity: tuple[dict[str, Any], ...] | None = None) -> tuple[dict[str, Any], ...]:
        """Propose a terminal source status in the next storage transaction."""
        baseline = self._activity if activity is None else activity
        if activity_id is None:
            return baseline
        record = next((deepcopy(item) for item in baseline if item["id"] == activity_id), None)
        if record is None:
            return baseline
        record["status"] = status
        if guidance is not None:
            record["guidance"] = guidance
        record["transitions"].append({"type": status, "at": datetime.now(UTC).isoformat(),
                                      "event_id": None})
        history = [item for item in baseline if item["id"] != activity_id] + [record]
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
        if kind == "calendar_write_uncertain":
            checkpoint = self.get_event(pending.id, event_id)
            transitions[-1]["write_attempt"] = checkpoint.write_attempt if checkpoint is not None else None
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
        return tuple(history)

    def _check_reset_target_inactive(self, fingerprint: str, *, source: bool) -> None:
        """Never forget the identity of pending or currently processing work."""
        active = (any(item.source_fingerprint == fingerprint for item in self._items.values())
                  or fingerprint in self._source_claims.values()
                  or fingerprint in self.processing_source_fingerprints.values()) if source else (
                      fingerprint in self._active_event_fingerprints())
        if active:
            raise PendingEventEditError("Pending or processing dedupe targets cannot be reset")

    async def async_reset_deduplication(
        self, *, source_id: str | None = None, event: EventDraft | None = None,
    ) -> bool:
        """Explicitly forget one handled identity, retaining every other record."""
        if (source_id is None) == (event is None):
            raise ValueError("Specify exactly one source ID or event")
        source = source_id is not None
        fingerprint = build_source_fingerprint(source_id) if source else event_fingerprint(event)
        # Refuse immediately if an approval currently holds the store lock;
        # waiting for its completion must not turn a pending-target refusal into
        # permission to forget its newly confirmed outcome.
        self._check_reset_target_inactive(fingerprint, source=source)
        result, cancelled = await self._async_complete_transaction(
            self._async_reset_deduplication_transaction(fingerprint, source=source)
        )
        if cancelled:
            raise asyncio.CancelledError
        return result

    async def _async_reset_deduplication_transaction(self, fingerprint: str, *, source: bool) -> bool:
        async with self._lock:
            self._check_reset_target_inactive(fingerprint, source=source)
            baseline = self._seen_source_fingerprints if source else self._seen_event_fingerprints
            if fingerprint not in baseline:
                return False
            updated = tuple(item for item in baseline if item != fingerprint)
            if source:
                await self._async_save(self._items, seen_source_fingerprints=updated)
                self._seen_source_fingerprints = updated
            else:
                await self._async_save(self._items, seen_event_fingerprints=updated)
                self._seen_event_fingerprints = updated
            return True

    async def async_reprocess(
        self,
        pending_id: str,
        parser: Callable[[PendingImport], Awaitable[tuple[tuple[PendingEvent, ...], tuple[str, ...]]]],
    ) -> tuple[PendingImport, int, int]:
        """Reparse retained text, preserving existing drafts and all dedupe history."""
        async with self._lock:
            original = self._items.get(pending_id)
            if original is None:
                raise PendingEventEditError("Pending import not found")
            if original.source_kind != "manual_text":
                raise PendingEventEditError("Original source material is unavailable; submit the source again")
            activity = self.get_activity(pending_id)
            if (any(event.status != "pending" for event in original.events)
                    or (activity and (activity.get("created_count", 0)
                                     or activity.get("rejected_count", 0)))):
                raise PendingEventEditError("Handled or uncertain imports cannot be reprocessed")
            if pending_id in self._reprocessing:
                raise PendingEventEditError("Source reprocessing is already in progress")
            self._reprocessing.add(pending_id)
        try:
            candidates, warnings = await parser(original)
            result, cancelled = await self._async_complete_transaction(
                self._async_commit_reprocessed(original, candidates, warnings)
            )
            if cancelled:
                raise asyncio.CancelledError
            return result
        finally:
            self._reprocessing.discard(pending_id)

    async def _async_commit_reprocessed(
        self, original: PendingImport, candidates: tuple[PendingEvent, ...],
        warnings: tuple[str, ...],
    ) -> tuple[PendingImport, int, int]:
        """Append novel drafts only if the entire original review is unchanged."""
        async with self._lock:
            if self._items.get(original.id) is not original:
                raise PendingEventEditError("Import changed during reprocessing; refresh before retrying")
            known = set(self._seen_event_fingerprints) | self._active_event_fingerprints()
            accepted = []
            duplicates = 0
            for candidate in candidates:
                fingerprint = event_fingerprint(candidate.draft)
                if fingerprint in known:
                    duplicates += 1
                else:
                    known.add(fingerprint)
                    accepted.append(candidate)
            updated = replace(
                original, events=original.events + tuple(accepted),
                warnings=tuple(dict.fromkeys((*original.warnings, *warnings))),
                duplicate_events=original.duplicate_events + duplicates,
            )
            items = dict(self._items)
            items[original.id] = updated
            activity = self._transition(updated, "source_reprocessed", remaining=updated.events)
            await self._async_save(items, activity=activity)
            self._commit_items(items)
            return updated, len(accepted), duplicates

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
            # Only an explicit destination selection resolves an unknown route.
            unresolved = event.routing_unresolved and calendar_entity is None
            if (event.draft == draft and target == event.calendar_entity
                    and unresolved == event.routing_unresolved):
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

            temporal_changed = any((
                event.draft.start != draft.start,
                event.draft.end != draft.end,
                event.draft.all_day != draft.all_day,
            ))
            edited = replace(event, draft=draft, calendar_entity=target,
                             routing_unresolved=unresolved,
                             date_time_assumptions=(() if temporal_changed
                                                    else event.date_time_assumptions))
            updated = replace(pending, events=tuple(
                edited if item.id == event_id else item for item in pending.events
            ))
            items = dict(self._items)
            items[pending_id] = updated
            activity = tuple({**record, "title": updated.events[0].draft.title}
                             if record["id"] == pending_id else record
                             for record in self._activity)
            await self._async_save(items, activity=activity)
            self._commit_items(items)
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
        event_assumptions: Iterable[tuple[str, ...]] | None = None,
        source_id: str | None = None,
        calendar_entity: str | None = None,
        routing_unresolved: bool = False,
        source_kind: str = "manual_text",
        source_title: str | None = None,
        source_sender: str | None = None,
        warnings: Iterable[str] = (),
        activity_id: str | None = None,
    ) -> PendingImportAddResult:
        """Persist events, letting the storage transaction finish before cancellation."""
        result, cancelled = await self._async_complete_transaction(
            self._async_add_transaction(
                source_text=source_text,
                events=events,
                event_assumptions=event_assumptions,
                source_id=source_id,
                calendar_entity=calendar_entity,
                routing_unresolved=routing_unresolved,
                source_kind=source_kind,
                source_title=source_title,
                source_sender=source_sender,
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
        event_assumptions: Iterable[tuple[str, ...]] | None = None,
        source_id: str | None = None,
        calendar_entity: str | None = None,
        routing_unresolved: bool = False,
        source_kind: str = "manual_text",
        source_title: str | None = None,
        source_sender: str | None = None,
        warnings: Iterable[str] = (),
        activity_id: str | None = None,
    ) -> PendingImportAddResult:
        """Persist only events not already pending or handled."""
        source_text = source_text.strip()
        if not source_text:
            raise ValueError("source_text must be a non-empty string")

        event_tuple = tuple(events)
        assumptions = (tuple(event_assumptions) if event_assumptions is not None
                       else ((),) * len(event_tuple))
        if len(assumptions) != len(event_tuple):
            raise ValueError("event assumptions must match the event count")
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
                    self._source_claims = source_claims
                return PendingImportAddResult(
                    pending=None,
                    duplicate_source=True,
                    duplicate_events=0,
                )

            known_events = set(self._seen_event_fingerprints)
            known_events.update(self._active_event_fingerprints())
            accepted_events: list[EventDraft] = []
            accepted_assumptions: list[tuple[str, ...]] = []
            accepted_fingerprints: set[str] = set()
            duplicate_events = 0

            for event, notes in zip(event_tuple, assumptions, strict=True):
                fingerprint = event_fingerprint(event)
                if (
                    fingerprint in known_events
                    or fingerprint in accepted_fingerprints
                ):
                    duplicate_events += 1
                    continue
                accepted_events.append(event)
                accepted_assumptions.append(notes)
                accepted_fingerprints.add(fingerprint)

            if not accepted_events:
                status = "duplicate" if duplicate_events else "no_events"
                activity = self._finish_submission(activity_id, status)
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
                return PendingImportAddResult(
                    pending=None,
                    duplicate_source=False,
                    duplicate_events=duplicate_events,
                )

            pending = PendingImport.create(
                source_text=source_text,
                events=accepted_events,
                event_assumptions=accepted_assumptions,
                source_fingerprint=source_fp,
                calendar_entity=calendar_entity,
                routing_unresolved=routing_unresolved,
                source_kind=source_kind,
                source_title=source_title,
                source_sender=source_sender,
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
            self._commit_items(items)
            self._source_claims = source_claims
            if self.on_review_ready is not None:
                self.on_review_ready(pending)
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
            self._commit_items(items)
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
            self._commit_items(items)
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
                    replace(item, status="pending", write_attempt=None)
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
            self._commit_items(items)
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
            # Reject a mixed-resolution batch before its first external write.
            if any(event.routing_unresolved for event in pending.events):
                raise PendingEventEditError(
                    "Confirm all event destinations in the editor before bulk approval"
                )

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
        if event.routing_unresolved:
            raise PendingEventEditError(
                "Confirm the destination calendar in the event editor before approval"
            )
        # Validate calendar safety while the event remains pending and BEFORE
        # persisting a write-in-flight checkpoint. A read/duplicate rejection
        # is not an ambiguous external calendar write.
        preflight = getattr(processor, "async_preflight", None)
        if preflight is not None:
            try:
                await preflight(event)
            except Exception:
                # This processor has not attempted a write. Keep the event
                # reviewable and report a confirmed non-write only after save.
                failed = self._transition(pending, "calendar_create_failed", event_id=event.id,
                                          remaining=pending.events)
                try:
                    await self._async_commit_activity(failed)
                except Exception:
                    pass
                raise
        in_flight = replace(pending, events=tuple(
                replace(item, status="write_uncertain", write_attempt=str(uuid4()))
                if item.id == event.id else item
                for item in pending.events
            ))
        items = dict(self._items)
        items[pending.id] = in_flight
        activity = self._transition(pending, "calendar_write_started", event_id=event.id)
        await self._async_save(items, activity=activity)
        self._commit_items(items)

        try:
            await processor(event)
        except (Exception, asyncio.CancelledError):
            uncertain = self._transition(pending, "calendar_write_uncertain", event_id=event.id)
            try:
                await self._async_commit_activity(uncertain)
            except Exception:
                # The write-started checkpoint already records an uncertain status.
                pass
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
        async def commit_created() -> None:
            await self._async_save(
                items,
                seen_source_fingerprints=seen_sources,
                seen_event_fingerprints=seen_events,
                activity=activity,
            )
            self._commit_items(items)
            self._seen_source_fingerprints = seen_sources
            self._seen_event_fingerprints = seen_events

        try:
            _, cancelled = await self._async_complete_transaction(commit_created())
        except (Exception, asyncio.CancelledError):
            # Cancellation can mask a failed save inside the transaction.
            # The external call completed, but the accepted outcome did not
            # commit. The durable write checkpoint remains authoritative.
            try:
                await self._async_complete_transaction(self._async_recover_uncertain_notifications())
            except (Exception, asyncio.CancelledError):
                pass
            raise
        if cancelled:
            raise asyncio.CancelledError
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
        notifications: tuple[NotificationRecord, ...] | None = None,
    ) -> None:
        """Persist a proposed collection and deduplication history."""
        if self._storage_blocked:
            raise PendingImportStorageError(_STORAGE_ERROR)
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
        journal = (self._journal_for_activity(activity) if activity is not None else self._notifications
                   ) if notifications is None else notifications
        history, journal = self._retained_history(items, history, claims, journal)
        if history:
            data[_STORAGE_ACTIVITY] = list(history)
        if journal:
            data[_STORAGE_NOTIFICATIONS] = [record.as_dict() for record in journal]
        await self._store.async_save(data)
        self._activity = history
        self._notifications = journal
        if self.on_notifications is not None:
            try:
                self.on_notifications()
            except Exception:
                _LOGGER.warning("Daylight notification scheduling failed; durable request retained")


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
