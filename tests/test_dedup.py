"""Tests for deduplication fingerprints."""

import pytest

from custom_components.daylight_calendar_import.dedup import (
    event_fingerprint,
    source_fingerprint,
)
from custom_components.daylight_calendar_import.models import EventDraft


def draft(
    *,
    title="Soccer Practice",
    start="2026-10-08T17:30:00-07:00",
    end="2026-10-08T18:30:00-07:00",
    all_day=False,
    location="Rancho Bernardo Community Park",
    description="Bring water",
    confidence=0.9,
):
    return EventDraft(
        title=title,
        start=start,
        end=end,
        all_day=all_day,
        location=location,
        description=description,
        confidence=confidence,
    )


def test_source_fingerprint_is_stable_private_and_case_sensitive():
    first = source_fingerprint("  Message-ID:ABC  ")

    assert first == source_fingerprint("Message-ID:ABC")
    assert first != source_fingerprint("Message-ID:abc")
    assert "Message-ID" not in first
    assert first.startswith("v1:source:")


def test_source_fingerprint_rejects_blank_id():
    with pytest.raises(ValueError, match="source_id must be a non-empty string"):
        source_fingerprint("   ")


def test_event_fingerprint_normalizes_text_timezone_and_confidence():
    first = draft()
    equivalent = draft(
        title="  SOCCER   practice ",
        start="2026-10-09T00:30:00+00:00",
        end="2026-10-09T01:30:00+00:00",
        location="rancho bernardo\u00a0community park",
        description="BRING   WATER",
        confidence=0.1,
    )

    assert event_fingerprint(first) == event_fingerprint(equivalent)
    assert event_fingerprint(first).startswith("v1:event:")


def test_event_fingerprint_keeps_meaningful_event_differences():
    baseline = event_fingerprint(draft())

    assert baseline != event_fingerprint(draft(title="Soccer Game"))
    assert baseline != event_fingerprint(draft(location="Different Park"))
    assert baseline != event_fingerprint(draft(description="Bring cleats"))
    assert baseline != event_fingerprint(
        draft(
            start="2026-10-08T18:30:00-07:00",
            end="2026-10-08T19:30:00-07:00",
        )
    )


def test_event_fingerprint_handles_all_day_dates():
    first = draft(
        title="Picture Day",
        start="2026-10-09",
        end="2026-10-10",
        all_day=True,
        location=None,
        description=None,
    )
    second = draft(
        title="picture day",
        start="2026-10-09",
        end="2026-10-10",
        all_day=True,
        location="",
        description="",
    )

    assert event_fingerprint(first) == event_fingerprint(second)


def test_event_fingerprint_v1_wire_format_is_stable():
    """Persisted v1 fingerprints must not change without a version bump."""
    timed_event = draft(
        title="Café Practice",
        location="Parque",
        description="Bring crème brûlée",
    )
    assert event_fingerprint(timed_event) == (
        "v1:event:1126c90f3cb7609ce8a8c602a273b60c64e1c9a5ad220fc285a44dd55b992c54"
    )

    all_day_event = draft(
        title="Picture Day",
        start="2026-10-09",
        end="2026-10-10",
        all_day=True,
        location=None,
        description=None,
    )
    assert event_fingerprint(all_day_event) == (
        "v1:event:235ce1fe9f1b079d695417d776dc46a2d1c6a58e8c20d3de110c5c9d93c8a693"
    )
