"""Default-deny namespace receipts for the supported pre-delivery storage contract.

This adapter recognizes the actual v2 local/BYO store. It does not assert that
an arbitrary M4 journal is empty, and must be replaced when delivery is added.
"""

from __future__ import annotations

import re
from typing import Any

from . import storage
from .sources import SourceKind

LOCAL_ONLY_CONTRACT = "pending-v2-local-only"
_ERROR = "Hosted namespace reconciliation is required; preserve existing journals."
_KINDS = {"manual_text", "email", "image", "pdf"}
_ROOT = {"items", "activity", "notifications", "source_claims", "seen_source_fingerprints", "seen_event_fingerprints"}
_ITEM = {"id", "created_at", "source_text", "events", "source_fingerprint", "source_kind", "source_title", "source_sender", "warnings", "duplicate_events"}
_EVENT = {"id", "draft", "status", "calendar_entity", "write_attempt", "routing_unresolved", "date_time_assumptions"}
_DRAFT = {"title", "start", "end", "all_day", "location", "description", "confidence"}
_ACTIVITY = {"id", "created_at", "source_kind", "source_title", "title", "created_count", "rejected_count", "status", "transitions", "notification_delivery_failed", "guidance"}
_TRANSITION = {"type", "at", "event_id", "write_attempt"}
_NOTIFICATION = {"import_id", "transition_type", "occurred_at", "event_id", "target", "status", "attempts", "next_attempt_at", "failure", "write_attempt", "recovered", "conflict_revision", "terminal_at"}
_IDENTIFIER = re.compile(r"[A-Za-z0-9_-]{16,128}")


class NamespaceReconciliationError(RuntimeError):
    """The local format cannot prove a supported namespace transition."""


def _closed(value: Any, fields: set[str]) -> None:
    if type(value) is not dict or set(value) - fields:
        raise NamespaceReconciliationError(_ERROR)


def _local_data(data: dict[str, Any]) -> None:
    """Pin every identity-bearing collection rather than accepting future fields."""
    _closed(data, _ROOT)
    storage._decode_storage_data(data)
    for item in data["items"]:
        _closed(item, _ITEM)
        if item.get("source_kind", "manual_text") not in _KINDS:
            raise NamespaceReconciliationError(_ERROR)
        for event in item["events"]:
            _closed(event, _EVENT)
            _closed(event["draft"], _DRAFT)
    for activity in data.get("activity", ()):
        _closed(activity, _ACTIVITY)
        if activity.get("source_kind") not in _KINDS:
            raise NamespaceReconciliationError(_ERROR)
        for transition in activity["transitions"]:
            _closed(transition, _TRANSITION)
    for notification in data.get("notifications", ()):
        _closed(notification, _NOTIFICATION)


def _read_contract(path: str) -> dict[str, Any] | None:
    envelope = storage._read_pending_storage(path)
    if envelope is None:
        return None
    if (set(envelope) != {"version", "minor_version", "key", "data"}
            or envelope["version"] != 2 or envelope["minor_version"] != 1
            or envelope["key"] != storage.STORAGE_KEY):
        raise NamespaceReconciliationError(_ERROR)
    _local_data(envelope["data"])
    return envelope["data"]


class LocalOnlyNamespaceReconciler:
    """Recognize a loaded, certain local-only store; never clear or copy it.

    M4 must replace this adapter with real journal reconciliation. A new source
    kind, schema/version, unknown metadata, blocked owner or queued write denies
    the receipt. A browser cannot supply the receipt or an empty-ledger flag.
    """

    def __init__(self, pending: storage.PendingImportStore) -> None:
        self._pending = pending

    async def async_receipt(self, prior_namespace: tuple[str, str] | None) -> str:
        try:
            if prior_namespace is not None and (
                type(prior_namespace) is not tuple or len(prior_namespace) != 2
                or any(type(value) is not str or _IDENTIFIER.fullmatch(value) is None for value in prior_namespace)
            ):
                raise NamespaceReconciliationError(_ERROR)
            pending = self._pending
            if (type(pending) is not storage.PendingImportStore
                    or type(pending._store) is not storage._PendingStore
                    or storage.STORAGE_VERSION != 2 or set(SourceKind) != _KINDS):
                raise NamespaceReconciliationError(_ERROR)
            async with pending._lock, pending._store._write_lock:
                if pending._storage_blocked or getattr(pending, "accepting_services", False) is not True or pending._store._data is not None:
                    raise NamespaceReconciliationError(_ERROR)
                # Pin the loaded state too: a missing file cannot erase evidence
                # that the current owner has already loaded durable local data.
                loaded = {
                    "items": [item.as_dict() for item in pending._items.values()],
                    "activity": list(pending._activity),
                    "notifications": [record.as_dict() for record in pending._notifications],
                    "source_claims": pending._source_claims,
                    "seen_source_fingerprints": pending._seen_source_fingerprints,
                    "seen_event_fingerprints": pending._seen_event_fingerprints,
                }
                _local_data(loaded)
                durable = await pending._store.hass.async_add_executor_job(_read_contract, pending._store.path)
                if pending._storage_blocked or getattr(pending, "accepting_services", False) is not True or pending._store._data is not None:
                    raise NamespaceReconciliationError(_ERROR)
                if durable is None and any(loaded.values()):
                    raise NamespaceReconciliationError(_ERROR)
            return LOCAL_ONLY_CONTRACT
        except Exception:  # noqa: BLE001 -- never expose source contents or private paths
            raise NamespaceReconciliationError(_ERROR) from None
