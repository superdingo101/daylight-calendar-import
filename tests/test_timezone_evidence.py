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
        received_at=datetime(2026, 11, 17, 9, tzinfo=ZoneInfo("America/Los_Angeles")), text=support,
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
    assert "A venue timezone differing from HA is not a conflict" in request["instructions"]
    fields = request["structure"].schema["events"].config["fields"]
    assert fields["time_zone_basis"]["required"] is True
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
    # Explicit HA fallback claims must agree on the whole offset candidate set.
    # Phoenix shares -07 with LA's first occurrence but has no second fold.
    event = {**BASE, "start": "2026-11-01T01:15:00-07:00", "end": "2026-11-01T01:45:00-07:00",
             endpoint: "America/Phoenix", "time_zone_basis": "ha_fallback"}
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
    result = parser.parse_ai_data({"events": [{**BASE, "start_time_zone": "America/Los_Angeles", "time_zone_basis": "ha_fallback"}]}, time_zone=zone)
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
    assert "basis=unspecified uses_reference=True" in caplog.text
    assert any("confirm the source time zone" in note for note in result.event_assumptions[0])
    restored = PendingImport.from_dict(PendingImport.create(
        source_text="synthetic candidate", events=result.events,
        event_assumptions=result.event_assumptions, warnings=result.warnings,
    ).as_dict())
    assert restored.events[0].date_time_assumptions == result.event_assumptions[0]
    assert "JACKSON" not in caplog.text
    assert "Encinitas" not in caplog.text


@pytest.mark.parametrize("basis", ["explicit", "geographic", "unspecified"])
@pytest.mark.parametrize("legacy_reference", [False, True])
@pytest.mark.parametrize(("venue", "ha", "offset", "support"), [
    ("America/Los_Angeles", "America/New_York", "-08:00", INVITATION),
    ("America/New_York", "America/Los_Angeles", "-05:00", "December 5, 2026, 10 AM to 1 PM in New York City."),
])
async def test_venue_timezone_differs_from_ha_without_reinterpretation(
    monkeypatch, basis, legacy_reference, venue, ha, offset, support,
):
    # Synthetic metadata: geographic is an inference, explicit is a provider
    # source-evidence claim; neither makes HA the authority over venue clocks.
    if basis == "explicit":
        support += f" Time zone: {venue}."
    event = {**BASE, "start": f"2026-12-05T10:00:00{offset}", "end": f"2026-12-05T13:00:00{offset}",
             "location": "Synthetic venue", "time_zone_basis": basis,
             "uses_reference_time_zone": legacy_reference,
             "start_time_zone": venue, "end_time_zone": venue}
    result = await replay(monkeypatch, event, zone=ha, support=support)
    assert [(draft.start, draft.end) for draft in result.events] == [(event["start"], event["end"])]
    restored = PendingImport.from_dict(PendingImport.create(
        source_text="synthetic venue scenario", events=result.events,
        event_assumptions=result.event_assumptions, warnings=result.warnings,
    ).as_dict())
    notes = restored.events[0].date_time_assumptions
    if basis == "geographic":
        assert any("inferred from venue geography" in note for note in notes)
    elif basis == "unspecified":
        expected = "provenance is uncertain" if legacy_reference else "provenance is unspecified"
        assert any(expected in note for note in notes)
    elif legacy_reference:
        assert any("AI timezone flags disagree" in note for note in notes)
    else:
        assert not result.warnings


@pytest.mark.parametrize("legacy_reference", [False, True])
async def test_ha_fallback_is_used_only_when_no_venue_zone_can_be_resolved(monkeypatch, legacy_reference):
    # The location exists, but the provider cannot reliably resolve its zone.
    # This is deliberately a different response from geographic inference.
    event = {**BASE, "time_zone_basis": "ha_fallback", "uses_reference_time_zone": legacy_reference,
             "start": "2026-12-05T10:00:00-05:00", "end": "2026-12-05T13:00:00-05:00",
             "assumptions": [f"Other context {i}" for i in range(8)]}
    result = await replay(monkeypatch, event, zone="America/New_York", support=INVITATION)
    assert result.events[0].start == event["start"]
    assert result.events[0].end == event["end"]
    assert len(result.event_assumptions[0]) == 8
    assert "Home Assistant fallback" in result.event_assumptions[0][0]
    assert "confirm the venue's local time zone" in result.event_assumptions[0][0]


@pytest.mark.parametrize("basis", ["geographic", "ha_fallback", "unspecified"])
async def test_two_safety_disclosures_survive_eight_provider_assumptions(monkeypatch, basis):
    event = {**BASE, "time_zone_basis": basis,
             "start": "2026-11-01T01:15:00-05:00", "end": "2026-11-01T01:45:00-05:00",
             "start_time_zone": "America/New_York", "end_time_zone": "America/New_York",
             "assumptions": [f"Other context {i}" for i in range(8)]}
    result = await replay(monkeypatch, event, zone="America/New_York")
    notes = result.event_assumptions[0]
    assert len(notes) == 8
    assert parser._FALL_BACK_REVIEW_NOTE in notes
    if basis == "geographic":
        assert any("venue geography" in note for note in notes)
    elif basis == "ha_fallback":
        assert any("Home Assistant fallback" in note for note in notes)
    else:
        assert any("provenance is uncertain" in note for note in notes)
    restored = PendingImport.from_dict(PendingImport.create(
        source_text="synthetic repeated hour", events=result.events,
        event_assumptions=result.event_assumptions, warnings=result.warnings,
    ).as_dict())
    assert restored.events[0].date_time_assumptions == notes


@pytest.mark.parametrize(("venue", "ha", "start", "end", "message"), [
    # NY's gap is invalid even though Arizona has no DST transition.
    ("America/New_York", "America/Phoenix", "2026-03-08T02:15:00-05:00", "2026-03-08T03:30:00-04:00", "does not exist"),
    # Arizona wall clocks must not be validated against HA's LA gap.
    ("America/Phoenix", "America/Los_Angeles", "2026-03-08T02:15:00-07:00", "2026-03-08T02:45:00-07:00", None),
    ("America/New_York", "America/Los_Angeles", "2026-12-05T10:00:00-08:00", "2026-12-05T13:00:00-08:00", "offset does not match"),
    # Winter LA cannot be 'fixed' using HA's summer/AZ -07 offset.
    ("America/Los_Angeles", "America/Phoenix", "2026-12-05T10:00:00-07:00", "2026-12-05T13:00:00-07:00", "offset does not match"),
    ("America/New_York", "America/Los_Angeles", "2026-12-05T23:30:00-05:00", "2026-12-06T01:00:00-05:00", None),
])
@pytest.mark.parametrize("basis", ["explicit", "geographic", "unspecified"])
async def test_strict_venue_validation_uses_venue_rules_not_ha_rules(monkeypatch, venue, ha, start, end, message, basis):
    event = {**BASE, "time_zone_basis": basis, "start_time_zone": venue, "end_time_zone": venue,
             "start": start, "end": end}
    result = await replay(monkeypatch, event, zone=ha)
    if message:
        assert not result.events
        assert any(message in warning for warning in result.warnings)
    else:
        assert [(draft.start, draft.end) for draft in result.events] == [(start, end)]


@pytest.mark.parametrize("basis", [[], None, "address", "", 3])
async def test_malformed_provenance_is_not_used_to_skip_validation(monkeypatch, basis):
    result = await replay(monkeypatch, {**BASE, "time_zone_basis": basis})
    assert not result.events
    assert "time_zone_basis must be" in result.warnings[0]


@pytest.mark.parametrize("zones", [("", ""), ("America/New_York", ""), ("", "America/New_York")])
async def test_geographic_claim_requires_both_named_endpoints(monkeypatch, zones):
    result = await replay(monkeypatch, {**BASE, "time_zone_basis": "geographic",
                                      "start_time_zone": zones[0], "end_time_zone": zones[1]})
    assert not result.events
    assert "requires named zones for both endpoints" in result.warnings[0]


async def test_ha_fallback_with_redundant_compatible_names_is_strict(monkeypatch):
    result = await replay(monkeypatch, {**BASE, "time_zone_basis": "ha_fallback",
                                      "start_time_zone": "US/Pacific", "end_time_zone": "America/Los_Angeles"})
    assert result.events[0].start == BASE["start"]
    assert any("Home Assistant fallback" in note for note in result.event_assumptions[0])


async def test_fixed_source_offset_overrides_conflicting_legacy_reference_flag(monkeypatch):
    # A timezone stated as a fixed offset is not an HA fallback, and cannot be
    # seasonally repaired or checked as if it claimed HA's named DST rules.
    event = {**BASE, "time_zone_basis": "explicit", "start": "2026-03-08T02:15:00-05:00",
             "end": "2026-03-08T02:45:00-05:00"}
    result = await replay(monkeypatch, event, support="March 8, 2026 2:15–2:45 AM UTC-05:00")
    assert result.events[0].start == event["start"]
    assert result.events[0].end == event["end"]
    assert "explicit zone/offset retained" in result.event_assumptions[0][0]


async def test_matched_source_zone_takes_priority_over_provider_fallback_claim(monkeypatch):
    event = {**BASE, "time_zone_basis": "ha_fallback"}
    record = f"{BASE['title']}\nDate: December 5, 2026\nTime: 10:00 AM to 1:00 PM (America/Los_Angeles)"
    result = await replay(monkeypatch, event, zone="America/New_York", support=record)
    assert result.events[0].start == BASE["start"]
    assert not result.warnings


@pytest.mark.parametrize(("value", "expected"), [
    (None, "start/end must be valid ISO values"),
    (42, "start/end must be valid ISO values"),
    ("not-a-date", "start/end must be valid ISO values"),
    ("2026-12-05T10:00:00", "timed events must include timezone offsets"),
    ("2026-03-08T02:15:00-08:00", "does not exist"),
])
def test_mutation_wall_time_rejects_invalid_inputs(value, expected):
    """Assert exact validation boundaries, including the spring DST gap."""
    with pytest.raises(parser.DraftValidationError, match=expected):
        parser._wall_time_candidates(value, ZoneInfo("America/Los_Angeles"))


@pytest.mark.parametrize(("value", "offsets"), [
    ("2026-12-05T10:00:00-08:00", {-8}),
    ("2026-07-05T10:00:00-07:00", {-7}),
    ("2026-11-01T01:15:00-07:00", {-7, -8}),
    ("2026-11-01T01:15:00-08:00", {-7, -8}),
])
def test_mutation_wall_time_candidate_offsets(value, offsets):
    instant, candidates = parser._wall_time_candidates(value, ZoneInfo("America/Los_Angeles"))
    assert instant.isoformat() == value
    assert {int(offset.total_seconds() / 3600) for offset in candidates} == offsets
    assert all(candidate.replace(tzinfo=None) == instant.replace(tzinfo=None)
               for candidate in candidates.values())


@pytest.mark.parametrize(("value", "expected", "ambiguous"), [
    ("2026-12-05T10:00:00-08:00", "2026-12-05T10:00:00-08:00", False),
    ("2026-07-05T10:00:00-08:00", "2026-07-05T10:00:00-07:00", False),
    ("2026-11-01T01:15:00-07:00", "2026-11-01T01:15:00-07:00", True),
    ("2026-11-01T01:15:00-08:00", "2026-11-01T01:15:00-08:00", True),
])
def test_mutation_reference_zone_preserves_wall_clock_and_valid_folds(value, expected, ambiguous):
    actual, repeated = parser._reference_zone_endpoint(value, ZoneInfo("America/Los_Angeles"))
    assert actual == expected
    assert repeated is ambiguous


@pytest.mark.parametrize(("value", "message"), [
    ("2026-12-05T10:00:00+00:00", "UTC-formatted inferred local time"),
    ("2026-12-05T10:00:00+05:00", "unrelated UTC offset"),
])
def test_mutation_reference_zone_rejects_unsafe_offset_reinterpretation(value, message):
    with pytest.raises(parser.DraftValidationError, match=message):
        parser._reference_zone_endpoint(value, ZoneInfo("America/Los_Angeles"))


@pytest.mark.parametrize(("zone", "value", "source_clock", "check_offset", "expected"), [
    ("America/Los_Angeles", "2026-12-05T10:00:00-08:00", None, True, False),
    ("America/Los_Angeles", "2026-11-01T01:15:00-07:00", None, True, True),
    ("America/Los_Angeles", "2026-11-01T01:15:00-08:00", None, True, True),
    ("America/Los_Angeles", "2026-12-05T10:00:00-08:00", "10:00AM", True, False),
    ("America/Los_Angeles", "2026-12-05T13:00:00-08:00", "01:00PM", True, False),
    ("America/Los_Angeles", "2026-12-05T10:00:00-07:00", None, False, False),
])
def test_mutation_named_zone_validates_wall_time_and_offset_policy(
    zone, value, source_clock, check_offset, expected,
):
    assert parser._named_zone_endpoint(value, zone, source_clock, check_offset=check_offset) is expected


@pytest.mark.parametrize(("zone", "value", "clock", "message"), [
    ("../etc/passwd", "2026-12-05T10:00:00-08:00", None, "IANA time zone name"),
    ("Unknown/Zone", "2026-12-05T10:00:00-08:00", None, "unavailable"),
    ("America/Los_Angeles", "2026-12-05T10:00:00-07:00", None, "offset does not match"),
    ("America/Los_Angeles", "2026-12-05T10:00:00-08:00", "11:00AM", "differs from the source"),
    ("America/Los_Angeles", "2026-03-08T02:15:00-08:00", None, "does not exist"),
])
def test_mutation_named_zone_rejects_conflicts(zone, value, clock, message):
    with pytest.raises(parser.DraftValidationError, match=message):
        parser._named_zone_endpoint(value, zone, clock, check_offset=True)


@pytest.mark.parametrize(("zone", "value", "condition"), [
    ("America/Los_Angeles", "2026-03-08T02:15:00-08:00", "nonexistent_wall_time"),
])
def test_mutation_dst_gap_diagnostic_is_specific(caplog, zone, value, condition):
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        with pytest.raises(parser.DraftValidationError):
            parser._wall_time_candidates(value, ZoneInfo(zone))
    assert f"Timezone validation failed: condition={condition} zone={zone}" in caplog.text


@pytest.mark.parametrize(("zone", "value", "expected"), [
    ("../etc/passwd", "malformed_named_identifier", "Source time zone must be an IANA time zone name"),
    ("Unknown/Zone", "unsupported_named_identifier", "Source IANA time zone is unavailable"),
    ("America/Los_Angeles", "named_offset_mismatch", "Extracted UTC offset does not match the source time zone"),
])
def test_mutation_named_zone_failure_diagnostics(caplog, zone, value, expected):
    instant = ("2026-12-05T10:00:00-07:00" if zone == "America/Los_Angeles"
               else "2026-12-05T10:00:00-08:00")
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        with pytest.raises(parser.DraftValidationError) as exc:
            parser._named_zone_endpoint(instant, zone, None, check_offset=True)
    assert str(exc.value).startswith(expected)
    assert f"Timezone validation failed: condition={value}" in caplog.text


@pytest.mark.parametrize(("zone", "value", "offsets"), [
    ("America/Los_Angeles", "2026-12-05T10:00:00-08:00", ("-1 day, 16:00:00",)),
    ("America/Los_Angeles", "2026-11-01T01:15:00-07:00",
     ("-1 day, 17:00:00", "-1 day, 16:00:00")),
])
def test_mutation_named_zone_candidate_diagnostics(caplog, zone, value, offsets):
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        parser._named_zone_endpoint(value, zone, None, check_offset=True)
    assert "Timezone candidate validated: origin=provider_named zone=" + zone in caplog.text
    for offset in offsets:
        assert offset in caplog.text


@pytest.mark.parametrize(("zone", "clock"), [
    ("America/Los_Angeles", "10:00AM"),
    ("America/Los_Angeles", "10:00PM"),
])
def test_mutation_named_zone_clock_matching_is_exact(zone, clock):
    value = "2026-12-05T10:00:00-08:00"
    if clock == "10:00AM":
        assert parser._named_zone_endpoint(value, zone, clock, check_offset=True) is False
    else:
        with pytest.raises(parser.DraftValidationError, match="Extracted clock time differs"):
            parser._named_zone_endpoint(value, zone, clock, check_offset=True)


@pytest.mark.parametrize(("start", "end", "expected_start", "expected_end", "ambiguous"), [
    ("2026-12-05T10:00:00-08:00", "2026-12-05T13:00:00-08:00",
     "2026-12-05T10:00:00-08:00", "2026-12-05T13:00:00-08:00", False),
    ("2026-07-05T10:00:00-08:00", "2026-07-05T13:00:00-08:00",
     "2026-07-05T10:00:00-07:00", "2026-07-05T13:00:00-07:00", False),
    ("2026-11-01T01:15:00-07:00", "2026-11-01T01:45:00-08:00",
     "2026-11-01T01:15:00-07:00", "2026-11-01T01:45:00-08:00", True),
    ("2026-11-01T01:15:00-08:00", "2026-11-01T01:45:00-07:00",
     "2026-11-01T01:15:00-07:00", "2026-11-01T01:45:00-07:00", True),
    ("2026-11-01T01:45:00-07:00", "2026-11-01T01:15:00-07:00",
     "2026-11-01T01:45:00-07:00", "2026-11-01T01:15:00-08:00", True),
])
def test_mutation_inferred_zone_resolves_only_safe_repeated_hour_ranges(
    start, end, expected_start, expected_end, ambiguous,
):
    original = {"start": start, "end": end, "title": "preserved"}
    corrected, is_ambiguous = parser._correct_inferred_zone(original, "America/Los_Angeles")
    assert corrected == {"start": expected_start, "end": expected_end, "title": "preserved"}
    assert is_ambiguous is ambiguous
    assert original == {"start": start, "end": end, "title": "preserved"}


@pytest.mark.parametrize("zone", ["Unknown/Zone", "../etc/passwd", ""])
def test_mutation_inferred_zone_requires_valid_ha_zone(zone):
    with pytest.raises(parser.DraftValidationError, match="Home Assistant time zone is unavailable"):
        parser._correct_inferred_zone({"start": BASE["start"], "end": BASE["end"]}, zone)


@pytest.mark.parametrize(("start", "end", "expected"), [
    ("2026-11-01T01:15:00-07:00", "2026-11-01T01:45:00-07:00",
     ("2026-11-01T01:15:00-07:00", "2026-11-01T01:15:00-08:00")),
    ("2026-12-05T10:00:00-08:00", "2026-12-05T13:00:00-08:00",
     ("2026-12-05T10:00:00-08:00",)),
])
def test_mutation_fallback_candidates_are_exact_real_instants(start, end, expected):
    zone = ZoneInfo("America/Los_Angeles")
    assert parser._fall_back_candidates(start, zone, len(expected) > 1) == expected


@pytest.mark.parametrize(("zones", "expected"), [
    (("America/Los_Angeles", "America/Los_Angeles"), False),
    (("America/Los_Angeles", ""), False),
    (("", "America/Los_Angeles"), False),
])
def test_mutation_explicit_zone_ambiguity_checks_both_endpoints(zones, expected):
    assert parser._explicit_zone_ambiguity(BASE, zones, None, check_offset=True) is expected


@pytest.mark.parametrize(("start", "end", "zones"), [
    ("2026-11-01T01:15:00-07:00", "2026-11-01T01:45:00-07:00",
     ("America/Los_Angeles", "America/Los_Angeles")),
    ("2026-11-01T01:15:00-08:00", "2026-11-01T01:45:00-08:00",
     ("America/Los_Angeles", "")),
])
def test_mutation_explicit_zone_ambiguity_propagates_repeated_hour(start, end, zones):
    raw = {**BASE, "start": start, "end": end}
    assert parser._explicit_zone_ambiguity(raw, zones, None, check_offset=True) is True


@pytest.mark.parametrize(("field", "value"), [
    ("start_time_zone", None), ("end_time_zone", 3),
    ("start_time_zone", ["America/Los_Angeles"]),
])
def test_mutation_explicit_zone_metadata_rejects_non_strings(field, value):
    with pytest.raises(parser.DraftValidationError, match="Source time zone must be a string"):
        parser._explicit_zones({**BASE, field: value})


def test_mutation_explicit_zone_metadata_strips_both_endpoints():
    assert parser._explicit_zones({
        "start_time_zone": "  America/Los_Angeles  ",
        "end_time_zone": "  America/New_York  ",
    }) == ("America/Los_Angeles", "America/New_York")


# Mutation regressions: assert user-visible provenance and diagnostic contracts,
# not merely that extraction returned an event.
@pytest.mark.parametrize(("basis", "use_reference", "start_zone", "end_zone", "expected_note"), [
    ("geographic", False, "America/Los_Angeles", "America/Los_Angeles",
     "Time zone inferred from venue geography; confirm the venue's local time zone before approval."),
    ("ha_fallback", True, "", "",
     "Time zone uses Home Assistant fallback; confirm the venue's local time zone before approval."),
    ("explicit", True, "America/Los_Angeles", "America/Los_Angeles",
     "AI timezone flags disagree; explicit zone/offset retained without changing wall times. Confirm the source time zone."),
    ("unspecified", False, "", "",
     "AI timezone provenance is unspecified; confirm the event's local time zone before approval."),
    ("unspecified", True, "America/Los_Angeles", "",
     "AI named timezone provenance is uncertain; times retained in that zone. Please confirm the source time zone before approval."),
])
def test_mutation_provenance_disclosures_are_exact_and_durable(
    basis, use_reference, start_zone, end_zone, expected_note, caplog,
):
    event = {**BASE, "time_zone_basis": basis,
             "uses_reference_time_zone": use_reference,
             "start_time_zone": start_zone, "end_time_zone": end_zone}
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        outcome = parser.parse_ai_data({"events": [event]}, time_zone="America/Los_Angeles")
    assert len(outcome.events) == 1
    assert outcome.events[0].start == BASE["start"]
    assert outcome.events[0].end == BASE["end"]
    assert outcome.event_assumptions[0][0] == expected_note
    assert outcome.warnings == [f"event 0: {expected_note}"]
    # Legacy inferred metadata with only one named endpoint is deliberately
    # expanded to both endpoints before validation and diagnostic logging.
    normalized_single_zone = basis == "unspecified" and use_reference and bool(start_zone or end_zone)
    expected_start_named = bool(start_zone) or normalized_single_zone
    expected_end_named = bool(end_zone) or normalized_single_zone
    assert ("Timezone evidence: origin=ai_metadata "
            f"basis={basis} uses_reference={use_reference} "
            f"named_start={expected_start_named} named_end={expected_end_named} "
            "matched_source_record=False") in caplog.text


@pytest.mark.parametrize(("updates", "expected"), [
    ({"time_zone_basis": "invalid"}, "time_zone_basis must be explicit, geographic, ha_fallback or unspecified"),
    ({"time_zone_basis": "geographic", "start_time_zone": "", "end_time_zone": ""},
     "Geographic timezone inference requires named zones for both endpoints"),
    ({"time_zone_basis": "geographic", "start_time_zone": "America/Los_Angeles",
      "end_time_zone": ""}, "Geographic timezone inference requires named zones for both endpoints"),
    ({"time_zone_basis": "ha_fallback", "start_time_zone": "America/New_York",
      "end_time_zone": "America/New_York"}, "Extracted UTC offset does not match the source time zone."),
    ({"time_zone_basis": "ha_fallback", "start_time_zone": "America/Los_Angeles",
      "end_time_zone": "America/Los_Angeles", "start": "2026-12-05T10:00:00-07:00"},
     "Extracted UTC offset does not match the source time zone."),
])
def test_mutation_provenance_errors_have_actionable_exact_reason(updates, expected):
    event = {**BASE, **updates}
    result = parser.parse_ai_data({"events": [event]}, time_zone="America/Los_Angeles")
    assert result.events == []
    assert len(result.warnings) == 1
    assert result.warnings[0].startswith("event 0 is invalid: " + expected)


def test_mutation_compatible_named_ha_metadata_diagnostic_and_fold(caplog):
    event = {**BASE, "time_zone_basis": "ha_fallback",
             "start_time_zone": "America/Los_Angeles",
             "end_time_zone": "America/Los_Angeles"}
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        result = parser.parse_ai_data({"events": [event]}, time_zone="America/Los_Angeles")
    assert len(result.events) == 1
    assert result.events[0].start == BASE["start"]
    assert result.warnings == [
        "event 0: Time zone uses Home Assistant fallback; confirm the venue's local time zone before approval."
    ]
    assert ("Timezone resolution: origin=ha_fallback zone=America/Los_Angeles "
            "condition=compatible_named_metadata") in caplog.text
    assert ("Timezone candidate validated: origin=ha_fallback zone=America/Los_Angeles "
            "offsets=('-1 day, 16:00:00',)") in caplog.text


def test_mutation_fallback_resolution_diagnostic_is_exact(caplog):
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        corrected, ambiguous = parser._correct_inferred_zone(
            {"start": BASE["start"], "end": BASE["end"]}, "America/Los_Angeles")
    assert corrected == {"start": BASE["start"], "end": BASE["end"]}
    assert ambiguous is False
    assert "Timezone resolution: origin=ha_fallback zone=America/Los_Angeles" in caplog.text


@pytest.mark.parametrize(("value", "message"), [
    ("2026-12-05T10:00:00+00:00",
     "UTC-formatted inferred local time cannot be safely reinterpreted"),
    ("2026-12-05T10:00:00+05:00",
     "inferred local time has an unrelated UTC offset"),
])
def test_mutation_reinterpretation_error_is_exact(value, message):
    with pytest.raises(parser.DraftValidationError) as raised:
        parser._reference_zone_endpoint(value, ZoneInfo("America/Los_Angeles"))
    assert str(raised.value) == message


def test_mutation_source_record_diagnostic_has_actual_origin(caplog):
    source_text = ("JACKSON IS TURNING 6\nDate: December 5, 2026\n"
                   "Time: 10:00 AM to 01:00 PM (America/Los_Angeles)")
    event = {**BASE, "time_zone_basis": "explicit",
             "uses_reference_time_zone": False}
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        result = parser.parse_ai_data({"events": [event]},
                                      source_text=source_text,
                                      time_zone="America/New_York")
    assert len(result.events) == 1
    assert result.events[0].start == BASE["start"]
    assert ("Timezone evidence: origin=ai_metadata basis=explicit uses_reference=False "
            "named_start=False named_end=False matched_source_record=True") in caplog.text
    assert ("Timezone candidate validated: origin=source_text_record "
            "zone=America/Los_Angeles") in caplog.text


def test_mutation_named_offset_error_complete_guidance_and_diagnostic(caplog):
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        with pytest.raises(parser.DraftValidationError) as raised:
            parser._named_zone_endpoint("2026-12-05T10:00:00-07:00",
                                        "America/Los_Angeles", None, check_offset=True)
    assert str(raised.value) == (
        "Extracted UTC offset does not match the source time zone. "
        "Specify the event's time zone and local times in supporting text and try again."
    )
    assert ("Timezone validation failed: condition=named_offset_mismatch "
            "zone=America/Los_Angeles") in caplog.text


@pytest.mark.parametrize(("zone_name", "offset"), [
    ("America/Phoenix", "-07:00"),
    ("America/New_York", "-05:00"),
])
def test_mutation_named_ha_fallback_rejects_different_valid_venue_zone(
    zone_name, offset, caplog,
):
    event = {**BASE, "time_zone_basis": "ha_fallback",
             "start_time_zone": zone_name, "end_time_zone": zone_name,
             "start": "2026-12-05T10:00:00" + offset,
             "end": "2026-12-05T13:00:00" + offset}
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        result = parser.parse_ai_data({"events": [event]}, time_zone="America/Los_Angeles")
    assert result.events == []
    assert result.warnings == [
        "event 0 is invalid: Named source time zones conflict with inferred time zone. "
        "Specify the event's time zone in supporting text and try again."
    ]
    assert "Timezone validation failed: condition=named_reference_conflict endpoint=start" in caplog.text


def test_mutation_named_identifier_length_boundary():
    # 255 is permitted by the validation boundary, although not a real IANA key.
    with pytest.raises(parser.DraftValidationError) as raised:
        parser._named_zone_endpoint(BASE["start"], "A" * 255, None, check_offset=True)
    assert str(raised.value) == "Source IANA time zone is unavailable"
    with pytest.raises(parser.DraftValidationError) as raised:
        parser._named_zone_endpoint(BASE["start"], "A" * 256, None, check_offset=True)
    assert str(raised.value) == "Source time zone must be an IANA time zone name"


@pytest.mark.parametrize("field", ["start_time_zone", "end_time_zone"])
def test_mutation_zone_metadata_type_rejection_is_exact(field):
    raw = {**BASE, field: []}
    with pytest.raises(parser.DraftValidationError) as raised:
        parser._explicit_zones(raw)
    assert str(raised.value) == "Source time zone must be a string"


def test_mutation_source_record_endpoint_validation_reports_origin_and_both_offsets(caplog):
    raw = {**BASE}
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        ambiguous = parser._explicit_zone_ambiguity(
            raw, ("", ""),
            parser._NAMED_TIME_RECORD.fullmatch(
                "JACKSON IS TURNING 6\nDate: December 5, 2026\n"
                "Time: 10:00 AM to 01:00 PM (America/Los_Angeles)"),
            check_offset=True,
        )
    assert ambiguous is False
    assert caplog.text.count(
        "Timezone candidate validated: origin=source_text_record "
        "zone=America/Los_Angeles offsets=('-1 day, 16:00:00',)"
    ) == 2


@pytest.mark.parametrize(("value", "clock", "error"), [
    ("2026-12-05T10:00:00-08:00", "11:00AM",
     "Extracted clock time differs from the source time"),
    ("2026-03-08T03:15:00-07:00", "02:15AM",
     "uses 2026-03-08 02:15:00 which does not exist in America/Los_Angeles. "
     "Check the source and submit a valid local time."),
])
def test_mutation_named_source_clock_mismatch_distinguishes_gap(value, clock, error):
    with pytest.raises(parser.DraftValidationError) as raised:
        parser._named_zone_endpoint(value, "America/Los_Angeles", clock, check_offset=True)
    assert str(raised.value) == error


def test_mutation_ambiguous_named_zone_validates_both_endpoints(caplog):
    raw = {
        "start": "2026-11-01T01:15:00-07:00",
        "end": "2026-11-01T01:45:00-08:00",
    }
    with caplog.at_level(logging.DEBUG, logger=parser.__name__):
        ambiguous = parser._explicit_zone_ambiguity(
            raw, ("America/Los_Angeles", "America/Los_Angeles"),
            None, check_offset=True,
        )
    assert ambiguous is True
    assert caplog.text.count(
        "Timezone candidate validated: origin=provider_named zone=America/Los_Angeles"
    ) == 2


@pytest.mark.parametrize(("case", "overrides", "expected"), [
    (
        "reconstructed Christmas 2027 Gemini offset mismatch",
        {
            "title": "Christmas dinner",
            # This is the location-free Christmas dinner regression, not the
            # unrelated Encinitas invitation inherited from BASE.
            "location": "",
            "start": "2027-12-25T18:00:00-05:00",
            "end": "2027-12-25T20:00:00-05:00",
            "start_local": "2027-12-25T18:00:00",
            "end_local": "2027-12-25T20:00:00",
            "start_location_type": "unknown",
            "start_location_value": "",
            "time_zone_basis": "ha_fallback",
        },
        ("2027-12-25T18:00:00-08:00", "2027-12-25T20:00:00-08:00",
         "America/Los_Angeles", "America/Los_Angeles"),
    ),
    (
        "cross-zone airport itinerary",
        {
            "title": "Flight LAX to JFK",
            "start": "2027-07-15T08:00:00-07:00",
            "end": "2027-07-15T16:30:00-04:00",
            "start_local": "2027-07-15T08:00:00",
            "end_local": "2027-07-15T16:30:00",
            "start_location_type": "airport",
            "start_location_value": "LAX",
            "end_location_type": "airport",
            "end_location_value": "JFK",
            "time_zone_basis": "geographic",
        },
        ("2027-07-15T08:00:00-07:00", "2027-07-15T16:30:00-04:00",
         "America/Los_Angeles", "America/New_York"),
    ),
])
async def test_new_structured_source_contract_through_production_provider(
    monkeypatch, case, overrides, expected,
):
    """Provider schema → parser → real draft → durable review (not a bypass test)."""
    event = {**BASE, **overrides}
    outcome = await replay(monkeypatch, event)
    assert outcome.warnings == [], case
    assert len(outcome.events) == 1
    draft = outcome.events[0]
    assert (draft.start, draft.end,
            draft.temporal.start_zone, draft.temporal.end_zone) == expected
    assert draft.temporal.status == "resolved"
    pending = PendingImport.create(
        source_text="synthetic structured extraction", events=outcome.events,
        event_assumptions=outcome.event_assumptions,
    )
    restored = PendingImport.from_dict(pending.as_dict())
    assert restored.events[0].draft == draft


@pytest.mark.parametrize("support", ["", "Please import the attached invitation",
                                    "Location: Los Angeles International Airport (LAX)"])
async def test_attachment_explicit_zone_conflict_survives_support_text(monkeypatch, support):
    event = {
        **BASE, "title": "Airport meeting", "location": "",
        "start": "2027-07-15T08:00:00-04:00", "end": "2027-07-15T09:00:00-04:00",
        "start_local": "2027-07-15T08:00:00", "end_local": "2027-07-15T09:00:00",
        "start_location_type": "airport", "start_location_value": "LAX",
        "explicit_timezone": "America/New_York",
    }
    result = await replay(monkeypatch, event, support=support)
    draft, = result.events
    assert draft.temporal.status == "confirmation_required"
    assert draft.temporal.explicit_timezone == "America/New_York"
    assert draft.temporal.start_location_value == "LAX"
    assert draft.temporal.start_zone == "America/New_York"
    assert draft.start == "2027-07-15T08:00:00-04:00"
    assert draft.end == "2027-07-15T09:00:00-04:00"
    assert result.warnings
