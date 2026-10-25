"""Stable deduplication fingerprints for calendar imports."""

from __future__ import annotations

from datetime import UTC, date, datetime
from hashlib import sha256
import json
import unicodedata

from .models import EventDraft

FINGERPRINT_VERSION = "v1"


def source_fingerprint(source_id: str) -> str:
    """Hash an opaque upstream source identifier without persisting it."""
    source_id = source_id.strip()
    if not source_id:
        raise ValueError("source_id must be a non-empty string")
    return _fingerprint("source", source_id)


def event_fingerprint(event: EventDraft) -> str:
    """Return a normalized fingerprint for a validated event draft."""
    payload = {
        "all_day": event.all_day,
        "title": _normalize_text(event.title),
        "start": _normalize_temporal(event.start, all_day=event.all_day),
        "end": _normalize_temporal(event.end, all_day=event.all_day),
        "location": _normalize_text(event.location or ""),
        "description": _normalize_text(event.description or ""),
    }
    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return _fingerprint("event", serialized)


def _normalize_text(value: str) -> str:
    """Normalize human text while preserving meaningful characters."""
    value = unicodedata.normalize("NFKC", value)
    return " ".join(value.split()).casefold()


def _normalize_temporal(value: str, *, all_day: bool) -> str:
    """Normalize equivalent temporal representations to one value."""
    if all_day:
        return date.fromisoformat(value).isoformat()
    return datetime.fromisoformat(value).astimezone(UTC).isoformat()


def _fingerprint(kind: str, value: str) -> str:
    digest = sha256(value.encode("utf-8")).hexdigest()
    return f"{FINGERPRINT_VERSION}:{kind}:{digest}"
