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
from .models import EventDraft

STORAGE_VERSION = 1
STORAGE_KEY = f"{DOMAIN}.pending_imports"
_STORAGE_ITEMS = "items"


@dataclass(frozen=True, slots=True)
class PendingImport:
    """A parsed import waiting for an explicit user decision."""

    id: str
    created_at: str
    source_text: str
    events: tuple[EventDraft, ...]

    @classmethod
    def create(
        cls,
        *,
        source_text: str,
        events: Iterable[EventDraft],
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
        )

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> PendingImport:
        """Restore a pending import from Home Assistant storage."""
        return cls(
            id=raw["id"],
            created_at=raw["created_at"],
            source_text=raw["source_text"],
            events=tuple(EventDraft.from_mapping(event) for event in raw["events"]),
        )

    def as_dict(self) -> dict[str, Any]:
        """Return the JSON-serializable storage representation."""
        return {
            "id": self.id,
            "created_at": self.created_at,
            "source_text": self.source_text,
            "events": [event.as_dict() for event in self.events],
        }


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
        self._lock = asyncio.Lock()

    async def async_load(self) -> None:
        """Load pending imports from Home Assistant storage."""
        data = await self._store.async_load()
        if data is None:
            self._items = {}
            return

        items = (
            PendingImport.from_dict(raw) for raw in data[_STORAGE_ITEMS]
        )
        self._items = {item.id: item for item in items}

    def get(self, pending_id: str) -> PendingImport | None:
        """Return one pending import by ID."""
        return self._items.get(pending_id)

    def list(self) -> tuple[PendingImport, ...]:
        """Return pending imports in insertion order."""
        return tuple(self._items.values())

    async def async_add(
        self,
        *,
        source_text: str,
        events: Iterable[EventDraft],
    ) -> PendingImport:
        """Create and persist a pending import."""
        pending = PendingImport.create(source_text=source_text, events=events)
        async with self._lock:
            items = dict(self._items)
            items[pending.id] = pending
            await self._async_save(items)
            self._items = items
        return pending

    async def async_remove(self, pending_id: str) -> bool:
        """Remove and persist a pending import if it exists."""
        async with self._lock:
            if pending_id not in self._items:
                return False
            items = dict(self._items)
            del items[pending_id]
            await self._async_save(items)
            self._items = items
        return True

    async def async_process(
        self,
        pending_id: str,
        processor: Callable[[PendingImport], Awaitable[None]],
    ) -> PendingImport | None:
        """Process one pending import and remove it only after success."""
        async with self._lock:
            pending = self._items.get(pending_id)
            if pending is None:
                return None

            await processor(pending)

            items = dict(self._items)
            del items[pending_id]
            await self._async_save(items)
            self._items = items
            return pending

    async def async_remove_storage(self) -> None:
        """Remove the backing storage file."""
        await self._store.async_remove()

    async def _async_save(self, items: dict[str, PendingImport]) -> None:
        """Persist a proposed collection."""
        await self._store.async_save(
            {_STORAGE_ITEMS: [item.as_dict() for item in items.values()]}
        )
