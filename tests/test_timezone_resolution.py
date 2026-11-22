"""End-to-end contracts for the structured, location-aware timezone path.

Assertions deliberately check exact instants and state transitions so surviving
offset, gate and persistence mutants cannot hide behind truthy/status checks.
"""
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import import parser, timezone_resolution as zones
from custom_components.daylight_calendar_import.models import EventDraft, DraftValidationError
from custom_components.daylight_calendar_import.storage import (
    PendingImport, PendingImportStore, PendingEventEditError,
)
import custom_components.daylight_calendar_import.storage as storage_module


def structured(**changes):
    return {
        "title": "Christmas dinner",
        "all_day": False,
        "start_local": "2027-12-25T18:00:00",
        "end_local": "2027-12-25T20:00:00",
        "start_location_type": "unknown",
        "start_location_value": "",
        "location": "",
        "confidence": 0.95,
        "assumptions": [],
        "uses_reference_time_zone": True,
        "time_zone_basis": "ha_fallback",
        "start_time_zone": "",
        "end_time_zone": "",
        **changes,
    }


def parse_one(**changes):
    return parser.parse_ai_data(
        {"events": [structured(**changes)], "unsupported_recurrence": False},
        time_zone="America/Los_Angeles",
    )


def test_christmas_dinner_reconstructs_source_clock_without_ai_offset():
    result = parse_one(start="2027-12-25T18:00:00-05:00",
                       end="2027-12-25T20:00:00-05:00")
    assert result.warnings == []
    assert len(result.events) == 1
    event = result.events[0]
    assert (event.start, event.end) == (
        "2027-12-25T18:00:00-08:00",
        "2027-12-25T20:00:00-08:00",
    )
    assert event.temporal == zones.TemporalEvidence(
        "2027-12-25T18:00:00", "2027-12-25T20:00:00",
        "America/Los_Angeles", "America/Los_Angeles",
        "ha_fallback", "resolved",
    )
    assert EventDraft.from_mapping(event.as_dict()) == event


def test_airport_itinerary_two_actual_instant_and_zones():
    result = parser.parse_ai_data({"events": [structured(
        title="LAX to JFK",
        start_local="2027-07-15T08:00:00",
        end_local="2027-07-15T16:30:00",
        start_location_type="airport", start_location_value="LAX",
        end_location_type="airport", end_location_value="JFK",
    )]}, time_zone="America/Los_Angeles")
    assert not result.warnings
    event, = result.events
    assert (event.start, event.end) == (
        "2027-07-15T08:00:00-07:00",
        "2027-07-15T16:30:00-04:00",
    )
    assert (datetime.fromisoformat(event.end) - datetime.fromisoformat(event.start)).total_seconds() == 19800
    assert (event.temporal.start_zone, event.temporal.end_zone,
            event.temporal.source, event.temporal.status) == (
        "America/Los_Angeles", "America/New_York", "airport", "resolved",
    )


@pytest.mark.parametrize(("kind", "value"), [
    ("city", "Encinitas, CA"),
    ("address", "123 Main Street, Encinitas, CA 92024"),
    ("postal_code", "92024"),
])
def test_encinitas_offline_geography_has_pacific_zone(kind, value):
    event, = parse_one(start_location_type=kind, start_location_value=value).events
    assert event.start == "2027-12-25T18:00:00-08:00"
    assert event.temporal.start_zone == "America/Los_Angeles"
    assert event.temporal.source == "geographic"
    assert event.temporal.status == "resolved"


@pytest.mark.parametrize(("location", "zone"), [
    ("London, UK", "Europe/London"),
    ("Paris, France", "Europe/Paris"),
    ("Sydney, Australia", "Australia/Sydney"),
])
def test_international_city_names_do_not_default_to_us_timezone(location, zone):
    event, = parse_one(start_location_type="city", start_location_value=location).events
    assert event.temporal.start_zone == zone
    assert event.temporal.source == "geographic"
    assert event.temporal.status == "resolved"
    # December offsets must be calculated under the event's actual regional zone.
    assert datetime.fromisoformat(event.start).utcoffset() != (
        datetime.fromisoformat("2027-12-25T18:00:00-08:00").utcoffset()
    )


@pytest.mark.parametrize("value", ["L0L", "0000", "L0X", "ABCDE"])
def test_invalid_airports_stay_unresolved_instead_of_becoming_ha_fallback(value):
    result = parse_one(start_location_type="airport", start_location_value=value)
    event, = result.events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.source == "ha_fallback"
    assert "timezone or DST occurrence" in event.temporal.reason


def test_unresolvable_venue_and_missing_country_are_not_guessed():
    event, = parse_one(start_location_type="venue", start_location_value="Some School").events
    assert event.temporal.status == "confirmation_required"
    assert zones._city_zone("Springfield") is None


def test_explicit_source_zone_is_not_downgraded_to_ha():
    event, = parse_one(explicit_timezone="America/New_York").events
    assert event.start == "2027-12-25T18:00:00-05:00"
    assert event.temporal.source == "explicit"
    assert event.temporal.status == "resolved"


@pytest.mark.parametrize(("explicit", "offset"), [
    ("UTC-05:00", "-05:00"),
    ("GMT+05:30", "+05:30"),
    ("UTC", "+00:00"),
])
def test_explicit_fixed_offsets_are_valid_evidence_not_geographic_zones(explicit, offset):
    event, = parse_one(explicit_timezone=explicit).events
    assert event.start == "2027-12-25T18:00:00" + offset
    assert event.temporal.source == "explicit"
    assert event.temporal.status == "resolved"
    assert EventDraft.from_mapping(event.as_dict()) == event


def test_ambiguous_explicit_abbreviation_never_loses_recoverable_event():
    event, = parse_one(explicit_timezone="DST").events
    assert event.temporal.source == "explicit"
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.start_zone == "America/Los_Angeles"


def test_conflicting_geo_and_explicit_zone_must_be_confirmed():
    event, = parse_one(explicit_timezone="America/New_York",
                       start_location_type="airport", start_location_value="LAX").events
    assert event.start == "2027-12-25T18:00:00-05:00"
    assert event.temporal.status == "confirmation_required"


def test_dst_repeated_hour_requires_confirmation_and_unselected_fold_cannot_auto_resolve():
    event, = parse_one(start_local="2027-11-07T01:15:00",
                       end_local="2027-11-07T01:45:00").events
    assert event.temporal.status == "confirmation_required"
    with pytest.raises(ValueError, match="DST occurrence"):
        zones.confirm_zone(event.temporal, "America/Los_Angeles")


def test_nonexistent_dst_clock_is_kept_but_requires_correction():
    event, = parser.parse_ai_data({"events": [structured(
        start_local="2027-03-14T02:15:00", end_local="2027-03-14T02:45:00",
    )]}, time_zone="America/Los_Angeles").events
    assert event.temporal.status == "correction_required"
    assert event.temporal.start_local == "2027-03-14T02:15:00"
    with pytest.raises(ValueError, match="DST occurrence"):
        zones.confirm_zone(event.temporal, "America/Los_Angeles")


@pytest.mark.parametrize(("bad", "message"), [
    ({"start_local": "2027-12-25T18:00:00-05:00"}, "must not include a timezone"),
    ({"start_location_type": "../etc/passwd"}, "invalid location type"),
    ({"explicit_timezone": "../etc/passwd"}, "invalid timezone name"),
    ({"start_local": ""}, "invalid local date/time"),
    ({"start_location_value": "x" * 257}, "invalid location evidence"),
])
def test_untrusted_structured_metadata_cannot_bypass_validation(bad, message):
    outcome = parse_one(**bad)
    assert outcome.events == []
    assert message in outcome.warnings[0]


def test_temporal_roundtrip_does_not_change_old_imports_or_dedup_shape():
    event, = parse_one().events
    item = PendingImport.create(source_text="Private dinner invitation", events=[event])
    stored = item.as_dict()
    assert PendingImport.from_dict(stored).events[0].draft == event
    legacy = {key: value for key, value in event.as_dict().items() if key != "temporal"}
    assert "temporal" not in EventDraft.from_mapping(legacy).as_dict()


class _Backend:
    def __init__(self):
        self.saved = []
        self.fail = None

    async def async_load(self):
        return self.saved[-1] if self.saved else None

    async def async_save(self, data):
        if self.fail:
            raise self.fail
        self.saved.append(data)

    async def async_remove(self):
        pass


def _store(monkeypatch, backend):
    monkeypatch.setattr(storage_module, "_PendingStore", lambda *_a, **_k: backend)
    return PendingImportStore(SimpleNamespace())


async def test_timezone_confirmation_gate_restarts_stale_clients_and_bulk(monkeypatch):
    backend = _Backend()
    store = _store(monkeypatch, backend)
    await store.async_load()
    event, = parse_one(start_location_type="venue", start_location_value="Unknown Venue").events
    item = (await store.async_add(source_text="source", events=[event])).pending
    original = item.events[0]
    writer = AsyncMock()
    with pytest.raises(PendingEventEditError, match="Confirm the event timezone"):
        await store.async_approve_event(item.id, original.id, writer)
    with pytest.raises(PendingEventEditError, match="Resolve all event timezones"):
        await store.async_process_events(item.id, writer)
    writer.assert_not_awaited()
    confirmed = await store.async_confirm_timezone(
        item.id, original.id, "America/Los_Angeles", expected_event=original,
    )
    assert confirmed.draft.temporal.status == "resolved"
    assert confirmed.draft.start == "2027-12-25T18:00:00-08:00"
    with pytest.raises(PendingEventEditError, match="Event changed"):
        await store.async_confirm_timezone(
            item.id, original.id, "America/New_York", expected_event=original,
        )
    backend_state = _Backend()
    backend_state.saved = backend.saved.copy()
    reboot = _store(monkeypatch, backend_state)
    await reboot.async_load()
    current = reboot.get_event(item.id, original.id)
    assert current == confirmed
    assert current.draft.temporal.start_zone == "America/Los_Angeles"
    await reboot.async_approve_event(item.id, original.id, writer, expected_event=current)
    writer.assert_awaited_once()
    assert reboot.get(item.id) is None


async def test_time_edit_does_not_clear_pending_timezone_gate(monkeypatch):
    backend = _Backend()
    store = _store(monkeypatch, backend)
    await store.async_load()
    event, = parse_one(start_location_type="venue", start_location_value="Unknown").events
    pending = (await store.async_add(source_text="source", events=[event])).pending
    original = pending.events[0]
    edited = await store.async_edit_event(
        pending.id, original.id,
        replace(event, start="2027-12-25T17:00:00-08:00", temporal=None),
        expected_event=original,
    )
    assert edited.draft.temporal.status == "confirmation_required"
    assert edited.draft.temporal.start_local == "2027-12-25T17:00:00"
    with pytest.raises(PendingEventEditError, match="metadata cannot be edited"):
        await store.async_edit_event(
            pending.id, original.id, event,
            expected_event=edited,
        )
    writer = AsyncMock()
    with pytest.raises(PendingEventEditError, match="Confirm the event timezone"):
        await store.async_approve_event(pending.id, original.id, writer)
    writer.assert_not_awaited()


async def test_failed_confirmation_storage_is_atomic(monkeypatch):
    backend = _Backend()
    store = _store(monkeypatch, backend)
    await store.async_load()
    event, = parse_one(start_location_type="venue", start_location_value="Unresolved").events
    pending = (await store.async_add(source_text="source", events=[event])).pending
    original = pending.events[0]
    backend.fail = RuntimeError("disk write failed")
    with pytest.raises(RuntimeError, match="disk write failed"):
        await store.async_confirm_timezone(pending.id, original.id, "America/New_York")
    assert store.get_event(pending.id, original.id) == original
