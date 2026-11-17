"""Synthetic provider-contract replays; these are NOT observed live AI outputs.

The invitation transcription preserves the visible source, which omits a year
and writes the range as '10 - 1 P.M.'. The trusted November 2026 reference and
user-confirmed intended year/range define the expected candidate in this test.
The private invitation image/address is deliberately not checked into git.
"""

from datetime import datetime
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.daylight_calendar_import import parser, providers
from custom_components.daylight_calendar_import.sources import SourceAttachment, SourceDocument, SourceKind
from custom_components.daylight_calendar_import.storage import PendingImport

INVITATION = """JACKSON IS TURNING 6
LET'S PLAY OUTSIDE TOGETHER
SAT | DEC. 5 | 10 - 1 P.M.
[street address omitted]
ENCINITAS, 92024"""
SUPPORT = (
    "The event is in Encinitas, California, USA. Use America/Los_Angeles "
    "(Pacific Standard Time, UTC-08:00) for both the start and end times. "
    "December 5, 2026, 10:00 AM to 1:00 PM."
)
BASE = {
    "title": "JACKSON IS TURNING 6", "all_day": False,
    "start": "2026-12-05T10:00:00-08:00", "end": "2026-12-05T13:00:00-08:00",
    "location": "Encinitas, CA 92024", "description": "LET'S PLAY OUTSIDE TOGETHER",
    "confidence": 0.95, "assumptions": [], "uses_reference_time_zone": True,
    "start_time_zone": "", "end_time_zone": "",
}


async def replay(monkeypatch, event, *, zone="America/Los_Angeles", support=""):
    """Exercise the shipped HA provider, prompt, parser and draft model."""
    seed = SourceDocument(
        id="invitation", kind=SourceKind.IMAGE,
        received_at="2026-11-17T09:00:00-08:00", text=support,
        attachments=(SourceAttachment("image", "image/png", 128, "media-source://test/invitation"),),
    )
    generate = AsyncMock(return_value=SimpleNamespace(data={
        "events": [event], "unsupported_recurrence": False,
    }))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    entity = SimpleNamespace(supported_features=providers.AITaskEntityFeature.SUPPORT_ATTACHMENTS)
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone=zone),
        data={providers.DATA_COMPONENT: SimpleNamespace(get_entity=lambda _: entity)},
    )
    result = await parser.async_parse_source(
        hass, source=seed, ai_task_entity="ai_task.test",
        reference_datetime=datetime(2026, 11, 17, 9, tzinfo=ZoneInfo(zone)),
    )
    request = generate.await_args.kwargs
    assert request["attachments"][0]["media_content_id"] == seed.attachments[0].content_ref
    assert json.dumps(support, ensure_ascii=False) in request["instructions"]
    assert "venue address or city is geographic context" in request["instructions"]
    assert "never a copy of the configured zone or an inferred location zone" in request["instructions"]
    return result


@pytest.mark.parametrize("named", [False, True])
@pytest.mark.parametrize("explicit", [False, True])
async def test_invitation_with_and_without_support_reaches_durable_review(monkeypatch, named, explicit):
    event = {**BASE, "uses_reference_time_zone": not explicit}
    if named or explicit:
        event.update(start_time_zone="America/Los_Angeles", end_time_zone="America/Los_Angeles")
    result = await replay(monkeypatch, event, support=SUPPORT if explicit else "")
    assert len(result.events) == 1
    assert result.events[0].start == BASE["start"]
    assert result.events[0].end == BASE["end"]
    pending = PendingImport.create(
        source_text=INVITATION, source_kind="image", events=result.events,
        event_assumptions=result.event_assumptions, warnings=result.warnings,
    )
    restored = PendingImport.from_dict(pending.as_dict())
    assert restored == pending
    assert restored.events[0].draft == result.events[0]
    if named and not explicit:
        assert "confirm the source time zone" in restored.events[0].date_time_assumptions[0]
        assert result.warnings


@pytest.mark.parametrize("source", ["Meeting 10 AM to 1 PM", INVITATION, "Meeting in New York 10 AM to 1 PM"])
async def test_geography_is_not_explicit_timezone_evidence(monkeypatch, source):
    # Contract-correct response: location alone cannot set explicit-zone fields.
    event = {**BASE, "assumptions": ["No explicit zone; using HA timezone. Confirm venue timezone."]}
    result = await replay(monkeypatch, event, support=source)
    assert result.events[0].start == BASE["start"]
    assert "Confirm venue" in result.event_assumptions[0][0]


@pytest.mark.parametrize("named", ["America/Los_Angeles", "US/Pacific", "PST8PDT"])
@pytest.mark.parametrize(("day", "offset"), [("2026-12-05", "-08:00"), ("2026-07-05", "-07:00")])
async def test_compatible_zone_representations_winter_and_summer(monkeypatch, named, day, offset):
    event = {**BASE, "start": f"{day}T10:00:00{offset}", "end": f"{day}T13:00:00{offset}",
             "start_time_zone": named, "end_time_zone": named}
    result = await replay(monkeypatch, event)
    assert (result.events[0].start, result.events[0].end) == (event["start"], event["end"])


@pytest.mark.parametrize("named", ["", "America/Los_Angeles"])
async def test_explicit_pacific_preserved_when_ha_differs(monkeypatch, named):
    event = {**BASE, "uses_reference_time_zone": False, "start_time_zone": named, "end_time_zone": named}
    result = await replay(monkeypatch, event, zone="America/New_York", support=SUPPORT)
    assert result.events[0].start == BASE["start"]
    assert not result.warnings


@pytest.mark.parametrize(("changes", "message"), [
    ({"start_time_zone": "Unknown/Zone"}, "unavailable"),
    ({"start_time_zone": "../etc/passwd"}, "IANA time zone name"),
    ({"start_time_zone": "PDT"}, "unavailable"),
    ({"start_time_zone": "CST"}, "unavailable"),
    ({"start_time_zone": "America/New_York"}, "offset does not match"),
    ({"start_time_zone": "America/Los_Angeles", "end_time_zone": "America/New_York",
      "uses_reference_time_zone": False}, "offset does not match"),
    ({"start_time_zone": "America/Los_Angeles", "start": "2026-12-05T10:00:00-07:00"}, "offset does not match"),
    ({"start_time_zone": "America/Los_Angeles", "end": "2026-12-05T13:00:00-07:00"}, "offset does not match"),
    ({"start_time_zone": "America/Los_Angeles", "start": "2026-03-08T02:15:00-08:00",
      "end": "2026-03-08T03:30:00-07:00"}, "does not exist"),
    ({"start_time_zone": "America/Los_Angeles", "start": "2026-12-05T23:00:00-08:00",
      "end": "2026-12-05T01:00:00-08:00"}, "end must be after start"),
])
async def test_conflicts_invalid_metadata_and_gaps_remain_rejected(monkeypatch, changes, message):
    result = await replay(monkeypatch, {**BASE, **changes})
    assert not result.events
    assert any(message in warning for warning in result.warnings)


@pytest.mark.parametrize("endpoint", ["start_time_zone", "end_time_zone"])
async def test_named_zone_matching_one_fold_is_not_equivalent_to_ha(monkeypatch, endpoint):
    # Phoenix shares -07 with LA's first occurrence but has no second fold.
    event = {**BASE, "start": "2026-11-01T01:15:00-07:00", "end": "2026-11-01T01:45:00-07:00",
             endpoint: "America/Phoenix"}
    result = await replay(monkeypatch, event)
    assert not result.events
    assert "Specify the event's time zone in supporting text" in result.warnings[0]


@pytest.mark.parametrize("zones", [("America/Los_Angeles", ""), ("", "America/Los_Angeles")])
async def test_repeated_hour_and_missing_named_endpoint_still_need_review(monkeypatch, zones):
    event = {**BASE, "start": "2026-11-01T01:15:00-08:00", "end": "2026-11-01T01:45:00-08:00",
             "start_time_zone": zones[0], "end_time_zone": zones[1]}
    result = await replay(monkeypatch, event)
    assert result.events[0].start == event["start"]
    assert parser._FALL_BACK_REVIEW_NOTE in result.event_assumptions[0]


async def test_cross_midnight_named_inferred_metadata_preserves_wall_dates(monkeypatch):
    event = {**BASE, "start": "2026-12-05T23:00:00-08:00", "end": "2026-12-06T01:00:00-08:00",
             "end_time_zone": "America/Los_Angeles"}
    result = await replay(monkeypatch, event)
    assert result.events[0].start == event["start"]
    assert result.events[0].end == event["end"]


@pytest.mark.parametrize("zone", [None, "Unknown/Zone", "../invalid"])
def test_named_inferred_metadata_requires_available_ha_zone(zone):
    result = parser.parse_ai_data({"events": [{**BASE, "start_time_zone": "America/Los_Angeles"}]}, time_zone=zone)
    assert not result.events
    assert "Home Assistant time zone is unavailable" in result.warnings[0]


def test_review_disclosure_survives_full_assumptions_list_and_private_diagnostics(caplog):
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        result = parser.parse_ai_data({"events": [{
            **BASE, "start_time_zone": "America/Los_Angeles", "assumptions": [f"note {i}" for i in range(8)],
        }]}, time_zone="America/Los_Angeles")
    assert len(result.events) == 1
    assert len(result.event_assumptions[0]) == 8
    assert "confirm the source time zone" in result.warnings[0]
    assert "origin=provider_named zone=America/Los_Angeles" in caplog.text
    assert "origin=ha_fallback zone=America/Los_Angeles" in caplog.text
    assert "JACKSON" not in caplog.text
    assert "Encinitas" not in caplog.text
