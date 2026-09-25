"""Domain models for Daylight Calendar Import."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from typing import Any


class DraftValidationError(ValueError):
    """Raised when an AI-produced event draft is not safe to import."""


@dataclass(frozen=True, slots=True)
class EventDraft:
    """A validated calendar-event draft."""

    title: str
    start: str
    end: str
    all_day: bool
    location: str | None = None
    description: str | None = None
    confidence: float = 0.0

    @classmethod
    def from_mapping(cls, raw: dict[str, Any]) -> EventDraft:
        """Validate and normalize an AI-produced event mapping."""
        title = _required_text(raw, "title")
        start = _required_text(raw, "start")
        end = _required_text(raw, "end")
        all_day = raw.get("all_day")
        if not isinstance(all_day, bool):
            raise DraftValidationError("all_day must be a boolean")

        location = _optional_text(raw, "location")
        description = _optional_text(raw, "description")
        confidence = raw.get("confidence", 0.0)
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise DraftValidationError("confidence must be a number")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise DraftValidationError("confidence must be between 0 and 1")

        _validate_temporal_range(start, end, all_day)
        return cls(
            title=title,
            start=start,
            end=end,
            all_day=all_day,
            location=location,
            description=description,
            confidence=confidence,
        )

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return asdict(self)


def _required_text(raw: dict[str, Any], key: str) -> str:
    value = raw.get(key)
    if not isinstance(value, str) or not value.strip():
        raise DraftValidationError(f"{key} must be a non-empty string")
    return value.strip()


def _optional_text(raw: dict[str, Any], key: str) -> str | None:
    value = raw.get(key)
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        raise DraftValidationError(f"{key} must be a string or null")
    value = value.strip()
    return value or None


def _validate_temporal_range(start: str, end: str, all_day: bool) -> None:
    try:
        if all_day:
            start_value = date.fromisoformat(start)
            end_value = date.fromisoformat(end)
        else:
            start_value = datetime.fromisoformat(start)
            end_value = datetime.fromisoformat(end)
            if start_value.tzinfo is None or end_value.tzinfo is None:
                raise DraftValidationError("timed events must include timezone offsets")
    except ValueError as err:
        raise DraftValidationError("start/end must be valid ISO values") from err

    if end_value <= start_value:
        raise DraftValidationError("end must be after start")
