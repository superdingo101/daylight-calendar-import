"""Persistent storage for pending calendar imports."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
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
_STORAGE_ITEMS = "items"
_STORAGE_SEEN_SOURCES = "seen_source_fingerprints"
_STORAGE_SEEN_EVENTS = "seen_event_fingerprints"


class PendingImportApprovalUncertainError(RuntimeError):
    """Raised when retrying an import with an uncertain prior approval outcome."""


class PendingEventEditError(ValueError):
    """Raised when an event cannot safely be edited."""


@dataclass(frozen=True, slots=True)
class PendingEvent:
    """A reviewable event with a stable identity."""

    id: str
    draft: EventDraft
    status: str = "pending"

    @classmethod
    def create(cls, draft: EventDraft) -> PendingEvent:
        return cls(str(uuid4()), draft)

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PendingEvent:
        if raw["status"] not in ("pending", "write_uncertain"):
            raise ValueError("invalid pending event status")
        return cls(raw["id"], EventDraft.from_mapping(raw["draft"]), raw["status"])

    def as_dict(self) -> dict[str, Any]:
        return {"id": self.id, "draft": self.draft.as_dict(), "status": self.status}

    def as_service_dict(self) -> dict[str, Any]:
        """Expose the stable ID alongside the existing flat draft fields."""
        return {**self.draft.as_dict(), "id": self.id, "status": self.status}


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

    @classmethod
    def create(
        cls,
        *,
        source_text: str,
        events: Iterable[EventDraft],
        source_fingerprint: str | None = None,
    ) -> PendingImport:
        """Create a new pending import with stable persisted metadata."""
        source_text = source_text.strip()
        if not source_text:
            raise ValueError("source_text must be a non-empty string")

        event_tuple = tuple(PendingEvent.create(event) for event in events)
        if not event_tuple:
            raise ValueError("pending import must contain at least one event")

        return cls(
            id=str(uuid4()),
            created_at=datetime.now(UTC).isoformat(),
            source_text=source_text,
            events=event_tuple,
            source_fingerprint=source_fingerprint,
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
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable storage representation."""
        result = {
            "id": self.id,
            "created_at": self.created_at,
            "source_text": self.source_text,
            "events": [event.as_dict() for event in self.events],
        }
        if self.source_fingerprint is not None:
            result["source_fingerprint"] = self.source_fingerprint
        return result

    def as_service_dict(self) -> dict[str, Any]:
        """Keep the existing submit response fields while exposing event IDs."""
        result = self.as_dict()
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
        self._seen_source_fingerprints: tuple[str, ...] = ()
        self._seen_event_fingerprints: tuple[str, ...] = ()
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load pending imports and deduplication history."""
        data = await self._store.async_load()
        if data is None:
            self._items = {}
            self._seen_source_fingerprints = ()
            self._seen_event_fingerprints = ()
            return

        items = (
            PendingImport.from_dict(raw) for raw in data[_STORAGE_ITEMS]
        )
        self._items = {item.id: item for item in items}
        self._seen_source_fingerprints = tuple(
            data.get(_STORAGE_SEEN_SOURCES, ())
        )
        self._seen_event_fingerprints = tuple(
            data.get(_STORAGE_SEEN_EVENTS, ())
        )

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

    async def async_edit_event(
        self, pending_id: str, event_id: str, draft: EventDraft
    ) -> PendingEvent | None:
        """Replace a ready draft atomically while preserving its event ID."""
        async with self._lock:
            pending = self._items.get(pending_id)
            if pending is None:
                return None
            event = next((item for item in pending.events if item.id == event_id), None)
            if event is None:
                return None
            if event.status != "pending":
                raise PendingEventEditError("Uncertain calendar write must be resolved before editing")
            if event.draft == draft:
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

            edited = PendingEvent(event.id, draft, event.status)
            updated = PendingImport(
                id=pending.id, created_at=pending.created_at,
                source_text=pending.source_text,
                events=tuple(edited if item.id == event_id else item for item in pending.events),
                source_fingerprint=pending.source_fingerprint,
            )
            items = dict(self._items)
            items[pending_id] = updated
            await self._async_save(items)
            self._items = items
            return edited

    def is_source_duplicate(self, source_id: str) -> bool:
        """Return whether a source ID is already pending or handled."""
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
            if source_fp is not None and self._source_fingerprint_exists(source_fp):
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
                if source_fp is not None:
                    seen_sources = _remember_fingerprints(
                        self._seen_source_fingerprints,
                        (source_fp,),
                    )
                    await self._async_save(
                        self._items,
                        seen_source_fingerprints=seen_sources,
                    )
                    self._seen_source_fingerprints = seen_sources
                return PendingImportAddResult(
                    pending=None,
                    duplicate_source=False,
                    duplicate_events=duplicate_events,
                )

            pending = PendingImport.create(
                source_text=source_text,
                events=accepted_events,
                source_fingerprint=source_fp,
            )
            items = dict(self._items)
            items[pending.id] = pending
            await self._async_save(items)
            self._items = items
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

            await self._async_save(
                items,
                seen_source_fingerprints=seen_sources,
                seen_event_fingerprints=seen_events,
            )
            self._items = items
            self._seen_source_fingerprints = seen_sources
            self._seen_event_fingerprints = seen_events
        return True

    async def async_reject_event(self, pending_id: str, event_id: str) -> bool:
        """Reject one ready event without discarding its siblings."""
        async with self._lock:
            pending = self._items.get(pending_id)
            if pending is None:
                return False
            event = next((item for item in pending.events if item.id == event_id), None)
            if event is None:
                return False
            if event.status == "write_uncertain":
                raise PendingImportApprovalUncertainError(pending_id)

            remaining = tuple(item for item in pending.events if item.id != event_id)
            items = dict(self._items)
            seen_sources = self._seen_source_fingerprints
            if remaining:
                items[pending_id] = PendingImport(
                    id=pending.id, created_at=pending.created_at,
                    source_text=pending.source_text, events=remaining,
                    source_fingerprint=pending.source_fingerprint,
                )
            else:
                del items[pending_id]
                if pending.source_fingerprint is not None:
                    seen_sources = _remember_fingerprints(
                        seen_sources, (pending.source_fingerprint,)
                    )
            seen_events = _remember_fingerprints(
                self._seen_event_fingerprints, (event_fingerprint(event.draft),)
            )
            await self._async_save(
                items,
                seen_source_fingerprints=seen_sources,
                seen_event_fingerprints=seen_events,
            )
            self._items = items
            self._seen_source_fingerprints = seen_sources
            self._seen_event_fingerprints = seen_events
            return True

    async def async_process_events(
        self,
        pending_id: str,
        processor: Callable[[EventDraft], Awaitable[None]],
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
        processor: Callable[[EventDraft], Awaitable[None]],
    ) -> PendingEvent | None:
        """Approve one ready event without approving its siblings."""
        async with self._lock:
            pending = self._items.get(pending_id)
            if pending is None:
                return None
            event = next((item for item in pending.events if item.id == event_id), None)
            if event is None:
                return None
            if event.status == "write_uncertain":
                raise PendingImportApprovalUncertainError(pending_id)
            await self._async_approve_event_locked(pending, event, processor)
            return event

    async def _async_approve_event_locked(
        self, pending: PendingImport, event: PendingEvent,
        processor: Callable[[EventDraft], Awaitable[None]],
    ) -> PendingImport | None:
        """Checkpoint the selected event around its external calendar write."""
        in_flight = PendingImport(
            id=pending.id, created_at=pending.created_at,
            source_text=pending.source_text,
            events=tuple(
                PendingEvent(item.id, item.draft, "write_uncertain")
                if item.id == event.id else item
                for item in pending.events
            ),
            source_fingerprint=pending.source_fingerprint,
        )
        items = dict(self._items)
        items[pending.id] = in_flight
        await self._async_save(items)
        self._items = items

        await processor(event.draft)

        remaining = tuple(item for item in pending.events if item.id != event.id)
        items = dict(self._items)
        seen_sources = self._seen_source_fingerprints
        seen_events = _remember_fingerprints(
            self._seen_event_fingerprints, (event_fingerprint(event.draft),)
        )
        if remaining:
            updated = PendingImport(
                id=pending.id, created_at=pending.created_at,
                source_text=pending.source_text, events=remaining,
                source_fingerprint=pending.source_fingerprint,
            )
            items[pending.id] = updated
        else:
            updated = None
            del items[pending.id]
            if pending.source_fingerprint is not None:
                seen_sources = _remember_fingerprints(
                    seen_sources, (pending.source_fingerprint,)
                )

        await self._async_save(
            items,
            seen_source_fingerprints=seen_sources,
            seen_event_fingerprints=seen_events,
        )
        self._items = items
        self._seen_source_fingerprints = seen_sources
        self._seen_event_fingerprints = seen_events
        return updated

    async def async_remove_storage(self) -> None:
        """Remove the backing storage file."""
        await self._store.async_remove()

    def _source_fingerprint_exists(self, fingerprint: str) -> bool:
        if fingerprint in self._seen_source_fingerprints:
            return True
        return any(
            item.source_fingerprint == fingerprint
            for item in self._items.values()
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
