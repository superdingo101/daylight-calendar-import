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

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.pending_imports"
DEDUP_HISTORY_LIMIT = 10_000
_STORAGE_ITEMS = "items"
_STORAGE_SEEN_SOURCES = "seen_source_fingerprints"
_STORAGE_SEEN_EVENTS = "seen_event_fingerprints"


class PendingImportApprovalUncertainError(RuntimeError):
    """Raised when retrying an import with an uncertain prior approval outcome."""


@dataclass(frozen=True, slots=True)
class PendingImport:
    """A parsed import waiting for an explicit user decision."""

    id: str
    created_at: str
    source_text: str
    events: tuple[EventDraft, ...]
    approval_in_flight: bool = False
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

        event_tuple = tuple(events)
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
            events=tuple(EventDraft.from_mapping(event) for event in raw["events"]),
            approval_in_flight=raw.get("approval_in_flight", False),
            source_fingerprint=raw.get("source_fingerprint"),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable storage representation."""
        result = {
            "id": self.id,
            "created_at": self.created_at,
            "source_text": self.source_text,
            "events": [event.as_dict() for event in self.events],
            "approval_in_flight": self.approval_in_flight,
        }
        if self.source_fingerprint is not None:
            result["source_fingerprint"] = self.source_fingerprint
        return result


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
        self._store: Store[dict[str, Any]] = Store(
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

    def list(self) -> tuple[PendingImport, ...]:
        """Return pending imports in insertion order."""
        return tuple(self._items.values())

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
                (event_fingerprint(event) for event in pending.events),
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
            while True:
                event = pending.events[0]
                in_flight = PendingImport(
                    id=pending.id,
                    created_at=pending.created_at,
                    source_text=pending.source_text,
                    events=pending.events,
                    approval_in_flight=True,
                    source_fingerprint=pending.source_fingerprint,
                )
                items = dict(self._items)
                items[pending_id] = in_flight
                await self._async_save(items)
                self._items = items
                pending = in_flight

                await processor(event)

                remaining = pending.events[1:]
                items = dict(self._items)
                seen_sources = self._seen_source_fingerprints
                seen_events = _remember_fingerprints(
                    self._seen_event_fingerprints,
                    (event_fingerprint(event),),
                )
                if remaining:
                    pending = PendingImport(
                        id=pending.id,
                        created_at=pending.created_at,
                        source_text=pending.source_text,
                        events=remaining,
                        source_fingerprint=pending.source_fingerprint,
                    )
                    items[pending_id] = pending
                else:
                    del items[pending_id]
                    if pending.source_fingerprint is not None:
                        seen_sources = _remember_fingerprints(
                            seen_sources,
                            (pending.source_fingerprint,),
                        )

                await self._async_save(
                    items,
                    seen_source_fingerprints=seen_sources,
                    seen_event_fingerprints=seen_events,
                )
                self._items = items
                self._seen_source_fingerprints = seen_sources
                self._seen_event_fingerprints = seen_events
                if not remaining:
                    break

            return original

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
            event_fingerprint(event)
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
