"""End-to-end contracts for the structured, location-aware timezone path.

Assertions deliberately check exact instants and state transitions so surviving
offset, gate and persistence mutants cannot hide behind truthy/status checks.
"""
from dataclasses import replace
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from hypothesis import given, settings, strategies as st

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
    ("postal_code", "92024, US"),
])
def test_encinitas_offline_geography_has_pacific_zone(kind, value):
    event, = parse_one(start_location_type=kind, start_location_value=value).events
    assert event.start == "2027-12-25T18:00:00-08:00"
    assert event.temporal.start_zone == "America/Los_Angeles"
    assert event.temporal.source == "geographic"
    assert event.temporal.status == "resolved"


@pytest.mark.parametrize(("location", "zone", "offset"), [
    ("Seattle, Washington", "America/Los_Angeles", "-08:00"),
    ("Miami, Florida", "America/New_York", "-05:00"),
    ("Austin, Texas", "America/Chicago", "-06:00"),
    ("Honolulu, Hawaii", "Pacific/Honolulu", "-10:00"),
])
def test_full_us_state_names_resolve_to_correct_regional_timezones(location, zone, offset):
    event, = parse_one(start_location_type="city", start_location_value=location).events
    assert event.temporal.status == "resolved"
    assert event.temporal.start_zone == zone
    assert event.start == f"2027-12-25T18:00:00{offset}"


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



@pytest.mark.parametrize("kind,value", [
    ("city", "Sydney, NSW, Australia"),
    ("city", "Sydney, New South Wales, Australia"),
    ("address", "Sydney, NSW, Australia"),
    ("venue", "Sydney, NSW, Australia"),
    ("venue", "Convention Centre, Sydney, NSW, Australia"),
])
def test_sydney_region_evidence_does_not_fall_back_to_los_angeles(kind, value):
    result = parser.parse_ai_data({"events": [structured(
        title="Sydney Conference",
        start_local="2027-12-15T10:00:00",
        end_local="2027-12-15T12:00:00",
        start_location_type=kind, start_location_value=value,
        end_location_type=kind, end_location_value=value,
    )]}, time_zone="America/Los_Angeles")
    assert result.warnings == []
    event, = result.events
    assert (event.start, event.end) == (
        "2027-12-15T10:00:00+11:00", "2027-12-15T12:00:00+11:00",
    )
    assert (event.temporal.start_zone, event.temporal.end_zone,
            event.temporal.source, event.temporal.status) == (
        "Australia/Sydney", "Australia/Sydney", "geographic", "resolved",
    )


@pytest.mark.parametrize(("city", "region", "country", "zone", "offset"), [
    ("Sydney", "NSW", "Australia", "Australia/Sydney", "+11:00"),
    ("Sydney", "New South Wales", "AU", "Australia/Sydney", "+11:00"),
    ("Brisbane", "Queensland", "Australia", "Australia/Brisbane", "+10:00"),
    ("Perth", "WA", "Australia", "Australia/Perth", "+08:00"),
    ("Adelaide", "SA", "Australia", "Australia/Adelaide", "+10:30"),
    ("Mumbai", "Maharashtra", "India", "Asia/Kolkata", "+05:30"),
    ("Toronto", "Ontario", "Canada", "America/Toronto", "-05:00"),
    ("Montréal", "Québec", "Canada", "America/Toronto", "-05:00"),
    ("Berlin", "Berlin", "Germany", "Europe/Berlin", "+01:00"),
])
def test_global_iso_and_geonames_subdivisions(city, region, country, zone, offset):
    value = f"{city}, {region}, {country}"
    event, = parser.parse_ai_data({"events": [structured(
        start_location_type="city", start_location_value=value,
    )]}, time_zone="America/Los_Angeles").events
    assert (event.temporal.start_zone, event.temporal.end_zone) == (zone, zone)
    assert (event.temporal.source, event.temporal.status) == ("geographic", "resolved")
    assert (event.start, event.end) == (
        f"2027-12-25T18:00:00{offset}", f"2027-12-25T20:00:00{offset}",
    )


def test_names_and_subdivision_tables_are_not_hardcoded_into_daylight():
    for attribute in ("_US_STATES", "_STATES", "_AU_REGIONS", "_COUNTRIES", "_cities"):
        assert not hasattr(zones, attribute)


@pytest.mark.parametrize(("country", "code"), [
    ("US", "US"), ("United States", "US"),
    ("UK", "GB"), ("United Kingdom", "GB"),
    ("Canada", "CA"), ("Australia", "AU"),
    ("Germany", "DE"), ("India", "IN"),
])
def test_localis_exact_country_identifiers_and_names(country, code):
    assert zones._country_code(country) == code


@pytest.mark.parametrize("value", ["", "Atlantis", "ZZ", "Not A Country"])
def test_unknown_countries_are_not_guessed(value):
    assert zones._country_code(value) is None


def test_unknown_australian_subdivision_remains_unresolved():
    event, = parse_one(
        start_location_type="city", start_location_value="Sydney, ZZ, Australia",
    ).events
    assert (event.temporal.source, event.temporal.status) == (
        "ha_fallback", "confirmation_required",
    )
    assert event.temporal.reason == "Confirm the source location's timezone."


@pytest.mark.parametrize(("value", "expected"), [
    ("Encinitas, CA", "America/Los_Angeles"),
    ("Encinitas, California", "America/Los_Angeles"),
    ("Toronto, ON", "America/Toronto"),
    ("Sydney, NSW", "Australia/Sydney"),
    ("Sydney, New South Wales", "Australia/Sydney"),
    ("Montréal, Quebec", "America/Toronto"),
    ("London, United Kingdom", "Europe/London"),
])
def test_two_part_country_or_subdivision_uses_exact_offline_evidence(value, expected):
    assert zones._place_zone("city", value) == expected


@pytest.mark.parametrize("value", [
    "Springfield, USA", "Imaginary City, Australia",
    "Encinitas, Canada", "Paris, Atlantis",
    "Springfield", "Encinitas, NY 92024",
    "Madeup Town, 92024",
])
def test_invalid_or_ambiguous_places_never_autoresolve(value):
    assert zones._place_zone("city", value) is None


@pytest.mark.parametrize("value", [
    "123 Main Street, Encinitas, CA 92024",
    "123 Main Street, Encinitas, California",
    "Encinitas, CA 92024",
])
def test_us_address_and_zip_are_consistent_with_city(value):
    kind = "city" if value.startswith("Encinitas,") else "address"
    assert zones._place_zone(kind, value) == "America/Los_Angeles"


@pytest.mark.parametrize("value", ["L0L", "0000", "L0X", "ABCDE"])
def test_invalid_airports_stay_unresolved_instead_of_becoming_ha_fallback(value):
    result = parse_one(start_location_type="airport", start_location_value=value)
    event, = result.events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.source == "ha_fallback"
    assert event.temporal.reason == "Confirm the source location's timezone."



@pytest.mark.parametrize(("kind", "value", "zone", "source"), [
    ("venue", "Moonlight Beach, Encinitas, California", "America/Los_Angeles", "geographic"),
    ("airport", "Los Angeles International Airport (LAX)", "America/Los_Angeles", "airport"),
    ("venue", "John F. Kennedy International Airport (JFK)", "America/New_York", "geographic"),
])
def test_source_place_suffix_and_printed_airport_codes_resolve(kind, value, zone, source):
    event, = parse_one(start_location_type=kind, start_location_value=value).events
    assert (event.temporal.start_zone, event.temporal.status, event.temporal.source) == (
        zone, "resolved", source,
    )
    assert datetime.fromisoformat(event.start).utcoffset() == (
        datetime.fromisoformat(event.end).utcoffset()
    )


def test_single_flight_labeled_source_repairs_combined_venue_and_hallucinated_explicit_zone():
    source = """Flight Itinerary
Flight: AA1234
Date: July 15, 2027
Departure:
Los Angeles International Airport (LAX)
8:00 AM local time
Arrival:
John F. Kennedy International Airport (JFK)
4:30 PM local time
All times are local to the respective airports."""
    event, = parser.parse_ai_data({"events": [structured(
        title="AA1234 flight",
        start_local="2027-07-15T08:00:00",
        end_local="2027-07-15T16:30:00",
        start_location_type="venue",
        start_location_value="Los Angeles International Airport (LAX) to JFK",
        end_location_type="unknown", end_location_value="",
        explicit_timezone="America/Los_Angeles",
        location="LAX to JFK",
    )]}, source_text=source, time_zone="America/Los_Angeles").events
    assert (event.start, event.end) == (
        "2027-07-15T08:00:00-07:00", "2027-07-15T16:30:00-04:00",
    )
    assert (event.temporal.start_zone, event.temporal.end_zone) == (
        "America/Los_Angeles", "America/New_York",
    )
    assert (event.temporal.source, event.temporal.status,
            event.temporal.explicit_timezone) == ("airport", "resolved", "")
    assert (event.temporal.start_location_value, event.temporal.end_location_value) == (
        "LAX", "JFK",
    )
    assert event.location == "LAX to JFK"


def test_unsupported_explicit_claim_does_not_override_geography_or_silently_fallback():
    raw = structured(explicit_timezone="America/New_York")
    source = "Christmas dinner on December 25, 2027, at 6 PM in Encinitas, CA"
    normalized, temporal = zones.normalize_structured_event(
        {**raw, "start_location_type": "city", "start_location_value": "Encinitas, CA"},
        "America/Los_Angeles", source,
    )
    assert normalized["start"] == "2027-12-25T18:00:00-08:00"
    assert (temporal.source, temporal.status, temporal.explicit_timezone) == (
        "geographic", "resolved", "",
    )
    _, unknown = zones.normalize_structured_event(raw, "America/Los_Angeles", source)
    assert (unknown.start_zone, unknown.status, unknown.explicit_timezone) == (
        "America/Los_Angeles", "confirmation_required", "",
    )


def test_printed_explicit_zone_remains_authoritative_and_conflict_is_blocked():
    source = "All times are in America/New_York time."
    raw = structured(explicit_timezone="America/New_York")
    _, temporal = zones.normalize_structured_event(raw, "America/Los_Angeles", source)
    assert (temporal.source, temporal.status, temporal.start_zone) == (
        "explicit", "resolved", "America/New_York",
    )
    _, conflicting = zones.normalize_structured_event({
        **raw, "start_location_type": "airport", "start_location_value": "LAX",
    }, "America/Los_Angeles", source)
    assert conflicting.status == "confirmation_required"
    assert conflicting.reason == "Confirm the source location's timezone."




@pytest.mark.parametrize(("city", "subdivision", "country", "expected"), [
    ("Munich", "Bavaria", "Germany", "Europe/Berlin"),
    ("Montréal", "Québec", "Canada", "America/Toronto"),
])
def test_real_provider_municipality_exonyms_and_subdivisions(
    city, subdivision, country, expected,
):
    import localis

    # Diagnostic arrays identify which exact index loses the legitimate name
    # without introducing approximate or popularity-based automatic matching.
    resolved = zones._city_zone(city, region=subdivision, country=country)
    candidates = localis.cities.filter(name=city, country=country)
    subdivisions = localis.subdivisions.filter(name=subdivision, country=country)
    diagnostics = {
        "city": city, "subdivision": subdivision, "country": country,
        "city_records": [
            (candidate.name,
             [(region.name, region.key, region.iso_code, region.geonames_code)
              for region in candidate.subdivisions])
            for candidate in candidates
        ],
        "subdivision_records": [
            (region.name, region.key, region.iso_code, region.aliases)
            for region in subdivisions
        ],
        "direct_matches": len(localis.cities.filter(
            name=city, country=country, subdivision=subdivision,
        )),
    }
    if resolved != expected:
        pytest.fail("LOCALIS_DIAGNOSTIC " + repr(diagnostics))


def test_real_provider_location_label_recovers_missing_typed_geography():
    source = ("Montréal Community Festival - Geo Test\n"
              "June 15, 2029\n2:00 PM - 5:00 PM\n\n"
              "Location:\nMontréal, Québec, Canada")
    raw = structured(
        title="Montréal Community Festival - Geo Test",
        start_local="2029-06-15T14:00:00",
        end_local="2029-06-15T17:00:00",
        start_location_type="unknown", start_location_value="",
        location="Montréal, Québec, Canada",
    )
    outcome = parser.parse_ai_data(
        {"events": [raw]}, source_text=source,
        time_zone="America/Los_Angeles",
    )
    assert outcome.warnings == []
    event, = outcome.events
    assert (event.temporal.start_zone, event.temporal.end_zone,
            event.temporal.source, event.temporal.status) == (
        "America/Toronto", "America/Toronto", "geographic", "resolved",
    )
    assert (event.start, event.end) == (
        "2029-06-15T14:00:00-04:00", "2029-06-15T17:00:00-04:00",
    )



@pytest.mark.parametrize("prefix", [
    "Location:", "Location:\n", "Venue:", "Where:",
])
def test_source_verified_untyped_place_works_with_common_labels(prefix):
    source = (
        "Meeting June 15, 2029 at 2 PM.\n" + prefix +
        ("" if prefix.endswith("\n") else " ") +
        "Montréal, Québec, Canada\n"
    )
    raw = structured(
        start_local="2029-06-15T14:00:00", end_local="2029-06-15T17:00:00",
        location="Montréal, Québec, Canada",
    )
    event, = parser.parse_ai_data({"events": [raw]}, source_text=source,
        time_zone="America/Los_Angeles").events
    assert (event.temporal.start_zone, event.temporal.start_location_type,
            event.temporal.start_location_value, event.temporal.status) == (
        "America/Toronto", "venue", "Montréal, Québec, Canada", "resolved",
    )
    assert (event.start, event.end) == (
        "2029-06-15T14:00:00-04:00", "2029-06-15T17:00:00-04:00",
    )


def test_conflicting_display_and_source_place_requires_confirmation():
    raw = structured(location="Montréal, Québec, Canada")
    source = "Location: Toronto, Ontario, Canada"
    event, = parser.parse_ai_data({"events": [raw]}, source_text=source,
        time_zone="America/Los_Angeles").events
    assert (event.temporal.source, event.temporal.status,
            event.temporal.start_location_type, event.temporal.start_location_value) == (
        "ha_fallback", "confirmation_required",
        "unknown", "Montréal, Québec, Canada",
    )
    assert event.temporal.reason == "Location evidence conflicts; confirm the event timezone."
    assert event.location == "Montréal, Québec, Canada"


def test_printed_but_unsupported_venue_still_blocks_approval():
    raw = structured(location="Community Hall")
    event, = parser.parse_ai_data({"events": [raw]},
        source_text="Location:\nCommunity Hall", time_zone="America/Los_Angeles").events
    assert (event.temporal.status, event.temporal.start_location_type,
            event.temporal.start_location_value) == (
        "confirmation_required", "venue", "Community Hall",
    )


def test_long_unverified_location_remains_reviewable_and_bounded():
    original = "Unknown Festival, " + "X" * 300
    raw = structured(location=original)
    result = parser.parse_ai_data({"events": [raw]},
        source_text="We meet here next week", time_zone="America/Los_Angeles")
    assert result.warnings
    event, = result.events
    assert (event.temporal.status, event.temporal.start_location_type,
            len(event.temporal.start_location_value)) == (
        "confirmation_required", "unknown", 256,
    )
    assert event.location == original


def test_typed_location_disagreement_with_display_requires_confirmation():
    raw = structured(
        start_location_type="city", start_location_value="Encinitas, California",
        location="Montréal, Québec, Canada",
    )
    event, = parser.parse_ai_data({"events": [raw]},
        source_text="Location: Montréal, Québec, Canada",
        time_zone="America/Los_Angeles").events
    assert (event.temporal.start_zone, event.temporal.status,
            event.temporal.start_location_type, event.temporal.start_location_value) == (
        "America/Los_Angeles", "confirmation_required", "city", "Encinitas, California",
    )
    assert event.temporal.reason == "Location evidence conflicts; confirm the event timezone."


def test_real_provider_unverified_display_location_must_not_approve_ha_guess():
    raw = structured(
        title="Location not confirmed", location="Montréal, Québec, Canada",
        start_location_type="unknown", start_location_value="",
    )
    outcome = parser.parse_ai_data({"events": [raw]},
        source_text="Not a matching place name",
        time_zone="America/Los_Angeles")
    event, = outcome.events
    assert (event.temporal.source, event.temporal.status) == (
        "ha_fallback", "confirmation_required",
    )
    assert event.temporal.reason == "Confirm the source location's timezone."


def test_real_provider_location_free_source_still_uses_ha_zone_without_blocking():
    event, = parser.parse_ai_data({"events": [structured(
        location="", start_location_type="unknown", start_location_value="",
    )]}, source_text="Dinner December 25, 2027 at 6 PM",
        time_zone="America/Los_Angeles").events
    assert (event.temporal.start_zone, event.temporal.status) == (
        "America/Los_Angeles", "resolved",
    )

def test_unrecognized_airport_names_and_venue_names_are_not_guessed():
    assert zones._airport_zone("ZZZ") is None
    assert zones._airport_zone("Invalid Airport (ZZZ)") is None
    assert zones._airport_zone("Airport without printed code") is None
    assert zones._place_zone("venue", "Some School, Springfield, USA") is None
    assert zones._place_zone("address", "Unknown Airport (ZZZ)") is None


def test_labeled_airports_must_be_unique_and_real():
    assert zones._labeled_flight_airports(
        "Departure: Los Angeles (LAX) 8 AM Arrival: New York (JFK) 4 PM"
    ) == ("LAX", "JFK")
    assert zones._labeled_flight_airports(
        "Departure: LAX and JFK Arrival: JFK"
    ) is None
    assert zones._labeled_flight_airports(
        "Departure: L0X Arrival: JFK"
    ) is None
    assert zones._airport_zone("LAX (LAX) (JFK)") is None


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


def test_dedup_identity_for_unresolved_source_is_independent_of_ai_offset():
    from custom_components.daylight_calendar_import.dedup import event_fingerprint
    event, = parse_one(start_location_type="venue",
                       start_location_value="Community Hall").events
    different_provisional_offset = replace(
        event, start="2027-12-25T18:00:00-05:00",
        end="2027-12-25T20:00:00-05:00",
    )
    assert event.temporal.status == "confirmation_required"
    assert event_fingerprint(event) == event_fingerprint(different_provisional_offset)
    resolved = replace(event, temporal=replace(event.temporal, status="resolved"))
    assert event_fingerprint(resolved) != event_fingerprint(event)


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


@pytest.mark.parametrize(("raw_patch", "message"), [
    ({"status": "unreviewed"}, "invalid timezone status"),
    ({"source": "trust_the_ai"}, "invalid timezone source"),
    ({"start_location_type": "server"}, "invalid persisted location type"),
    ({"reason": 10}, "invalid temporal review fields"),
    ({"reason": "R" * 257}, "invalid temporal review fields"),
    ({"start_local": "2027-15-01T10:00:00"}, "invalid local date/time"),
])
def test_temporal_storage_strict_validation_catches_corruption(raw_patch, message):
    event, = parse_one().events
    snapshot = event.temporal.as_dict()
    snapshot.update(raw_patch)
    with pytest.raises(ValueError, match=message):
        zones.TemporalEvidence.from_dict(snapshot)


def test_temporal_storage_rejects_missing_extra_and_nonobject_metadata():
    event, = parse_one().events
    snapshot = event.temporal.as_dict()
    for invalid in (None, [], {"status": "resolved"},
                    {**snapshot, "extra": "unknown"}):
        with pytest.raises(ValueError, match="invalid temporal review data"):
            zones.TemporalEvidence.from_dict(invalid)
    assert zones.TemporalEvidence.from_dict(snapshot) == event.temporal


@pytest.mark.parametrize("value", [None, 1, "1" * 33, "bad-time", "2027-20-01T10:00:00"])
def test_invalid_local_datetime_values_never_become_timestamp_candidates(value):
    with pytest.raises(ValueError, match="invalid local date/time"):
        zones._local(value)


@pytest.mark.parametrize("value", ["UTC+25:00", "GMT-14:60", "UTC+24", "UTC+01:99"])
def test_impossible_fixed_utc_offsets_are_rejected(value):
    assert zones._fixed_zone(value) is None


@pytest.mark.parametrize("value", [None, "", "../etc/passwd", "Unknown/Zone", "A" * 129])
def test_timezone_names_are_bounded_and_validated(value):
    with pytest.raises(ValueError, match="timezone name"):
        zones._zone(value)


@pytest.mark.parametrize(("latitude", "longitude"), [
    (None, None), ("not-a-coordinate", -117), (91, -117),
    (33, 181), (-91, 0),
])
def test_invalid_coordinates_never_produce_a_zone(latitude, longitude):
    assert zones._coordinates_zone(latitude, longitude) is None


def test_city_and_postal_lookup_reject_incomplete_ambiguous_geography():
    assert zones._postal_zone("London SW1A 1AA") is None
    assert zones._place_zone("city", "Springfield") is None
    assert zones._place_zone("virtual", "unspecified") is None
    assert zones._city_zone("NotARealCityAtAll", "CA", "US") is None
    assert zones._place_zone("city", "Encinitas, 92024") == "America/Los_Angeles"


def test_postal_lookup_falls_back_to_city_and_checks_missing_zone(monkeypatch):
    monkeypatch.setattr(zones, "_coordinates_zone", lambda *_args: None)
    monkeypatch.setattr(zones, "_city_zone", lambda *_args: "America/Los_Angeles")
    assert zones._postal_zone("92024") == "America/Los_Angeles"
    monkeypatch.setattr(zones, "_city_zone", lambda *_args: None)
    assert zones._postal_zone("92024") is None


@pytest.mark.parametrize(("start_choice", "end_choice", "expected_start", "expected_end"), [
    ("first", "first", "-07:00", "-07:00"),
    ("first", "second", "-07:00", "-08:00"),
    ("second", "second", "-08:00", "-08:00"),
])
def test_dst_fold_confirmation_chooses_exact_first_or_second_instants(
    start_choice, end_choice, expected_start, expected_end,
):
    event, = parse_one(start_local="2027-11-07T01:15:00",
                       end_local="2027-11-07T01:45:00").events
    start, end, confirmed = zones.confirm_zone(
        event.temporal, "America/Los_Angeles",
        start_occurrence=start_choice, end_occurrence=end_choice,
    )
    assert (start, end) == ("2027-11-07T01:15:00" + expected_start,
                            "2027-11-07T01:45:00" + expected_end)
    assert confirmed.status == "resolved"
    assert confirmed.source == "user"


def test_dst_fold_rejects_invalid_occurrences_and_reversed_instant_range():
    event, = parse_one(start_local="2027-11-07T01:15:00",
                       end_local="2027-11-07T01:45:00").events
    for kwargs in ({"start_occurrence": "third"},
                   {"start_occurrence": "second", "end_occurrence": "first"}):
        with pytest.raises(ValueError, match="Invalid DST occurrence|end must be after start"):
            zones.confirm_zone(event.temporal, "America/Los_Angeles", **kwargs)
    with pytest.raises(ValueError, match="Invalid DST occurrence"):
        zones.confirm_zone(parse_one().events[0].temporal, "America/Los_Angeles",
                           start_occurrence="first")


def test_structured_extraction_preserves_source_geo_and_blocks_arrival_only_location():
    result = parse_one(
        start_location_type="unknown", start_location_value=None,
        end_location_type="airport", end_location_value="JFK",
        end_local="2027-12-25T22:00:00",
    )
    event, = result.events
    assert event.temporal.source == "airport"
    assert event.temporal.start_zone == "America/Los_Angeles"
    assert event.temporal.end_zone == "America/New_York"
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.travel_incomplete is True
    assert event.temporal.reason == "Travel itinerary has an unverified endpoint; confirm both timezones."
    assert event.location == "JFK"


def test_structured_source_keeps_explicit_event_location_text_and_null_timezone():
    event, = parse_one(location="At home", explicit_timezone=None).events
    assert event.location == "At home"
    assert event.temporal.explicit_timezone == ""
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.start_location_value == "At home"


@pytest.mark.parametrize("value", [42, "X" * 129])
def test_invalid_explicit_timezones_are_rejected_not_guessed(value):
    outcome = parse_one(explicit_timezone=value)
    assert not outcome.events
    assert "invalid explicit timezone evidence" in outcome.warnings[0]


@pytest.mark.parametrize("changes", [
    {"all_day": True},
    {"start_local": "2027-12-25T18:00:00", "end_local": None},
])
def test_incomplete_timed_source_fails_closed_without_calendar_draft(changes):
    result = parse_one(**changes)
    assert not result.events
    assert "invalid" in result.warnings[0] or "require a timed event" in result.warnings[0]


def test_structured_source_requires_configured_home_timezone():
    outcome = parser.parse_ai_data({"events": [structured()]}, time_zone=None)
    assert outcome.events == []
    assert "Home Assistant time zone is unavailable" in outcome.warnings[0]


def test_invalid_all_day_temporal_and_forged_temporal_are_rejected():
    event, = parse_one().events
    raw = event.as_dict()
    raw["temporal"] = {"status": "resolved"}
    with pytest.raises(DraftValidationError, match="invalid temporal review state"):
        EventDraft.from_mapping(raw)
    raw = event.as_dict()
    raw.update(start="2027-12-25", end="2027-12-26", all_day=True)
    with pytest.raises(DraftValidationError, match="all-day events cannot have"):
        EventDraft.from_mapping(raw)


async def test_store_confirm_timezone_missing_inflight_legacy_invalid_and_duplicate(monkeypatch):
    backend = _Backend()
    store = _store(monkeypatch, backend)
    await store.async_load()
    unresolved, = parse_one(start_location_type="venue",
                            start_location_value="Community Hall").events
    pending = (await store.async_add(source_text="one", events=[unresolved])).pending
    ev = pending.events[0]
    assert await store.async_confirm_timezone("missing", ev.id, "America/Los_Angeles") is None
    assert await store.async_confirm_timezone(pending.id, "missing", "America/Los_Angeles") is None
    with pytest.raises(PendingEventEditError, match="cannot be changed"):
        store._reprocessing.add(pending.id)
        try:
            await store.async_confirm_timezone(pending.id, ev.id, "America/Los_Angeles")
        finally:
            store._reprocessing.discard(pending.id)
    with pytest.raises(PendingEventEditError, match="timezone name"):
        await store.async_confirm_timezone(pending.id, ev.id, "../etc/passwd")
    assert store.get_event(pending.id, ev.id) == ev

    legacy = (await store.async_add(source_text="legacy", events=[
        replace(unresolved, title="Separate record", temporal=None),
    ])).pending
    with pytest.raises(PendingEventEditError, match="no structured timezone evidence"):
        await store.async_confirm_timezone(legacy.id, legacy.events[0].id, "America/Los_Angeles")

    # Two candidates have different fingerprints while unresolved; once the
    # provisional instant becomes definitive, the duplicate is rejected.
    exact = replace(unresolved, temporal=None)
    other = (await store.async_add(source_text="completed", events=[exact])).pending
    with pytest.raises(PendingEventEditError, match="duplicates a pending"):
        await store.async_confirm_timezone(pending.id, ev.id, "America/Los_Angeles")
    assert store.get_event(pending.id, ev.id) == ev


async def test_store_timed_to_all_day_conversion_cannot_bypass_gate(monkeypatch):
    store = _store(monkeypatch, _Backend())
    await store.async_load()
    unresolved, = parse_one(start_location_type="venue", start_location_value="Hall").events
    pending = (await store.async_add(source_text="source", events=[unresolved])).pending
    previous = pending.events[0]
    all_day = replace(unresolved, start="2027-12-25", end="2027-12-26",
                      all_day=True, temporal=None)
    with pytest.raises(PendingEventEditError, match="Confirm the timezone"):
        await store.async_edit_event(pending.id, previous.id, all_day, expected_event=previous)
    confirmed = await store.async_confirm_timezone(pending.id, previous.id, "America/Los_Angeles")
    edited = await store.async_edit_event(pending.id, previous.id, all_day, expected_event=confirmed)
    assert edited.draft.temporal is None
    assert edited.draft.all_day is True
    assert edited.draft.start == "2027-12-25"


async def test_store_invalid_edit_clock_still_retains_original_pending_state(monkeypatch):
    store = _store(monkeypatch, _Backend())
    await store.async_load()
    original, = parse_one(start_location_type="venue", start_location_value="Hall").events
    pending = (await store.async_add(source_text="source", events=[original])).pending
    ev = pending.events[0]
    draft = replace(original, start="2027-12-25 17:00:00-08:00", temporal=None)
    with pytest.raises(PendingEventEditError, match="Invalid edited local clocks"):
        await store.async_edit_event(pending.id, ev.id, draft, expected_event=ev)
    assert store.get_event(pending.id, ev.id) == ev



@pytest.mark.parametrize(("changes", "source_text", "expected_status"), [
    (
        {"start_location_type": "city", "start_location_value": "Encinitas, CA",
         "location": "Montréal, Québec, Canada"},
        "Location: Montréal, Québec, Canada", "confirmation_required",
    ),
    (
        {"start_location_type": "city", "start_location_value": "Encinitas, CA",
         "location": ""},
        "Location: Montréal, Québec, Canada", "confirmation_required",
    ),
    (
        {"start_location_type": "city", "start_location_value": "Encinitas, CA",
         "location": "John F. Kennedy International Airport (JFK)"},
        "", "confirmation_required",
    ),
    (
        {"start_location_type": "city", "start_location_value": "Encinitas, CA",
         "location": "Encinitas, California"},
        "Location: Encinitas, CA", "resolved",
    ),
    (
        {"start_location_type": "city", "start_location_value": "Encinitas, CA",
         "location": ""},
        "", "resolved",
    ),
])
def test_independent_location_evidence_never_silently_overrides_typed_geography(
    changes, source_text, expected_status,
):
    result = parser.parse_ai_data(
        {"events": [structured(**changes)]},
        source_text=source_text, time_zone="America/Los_Angeles",
    )
    event, = result.events
    assert event.temporal.start_zone == "America/Los_Angeles"
    assert event.temporal.status == expected_status
    assert event.start == "2027-12-25T18:00:00-08:00"
    assert event.end == "2027-12-25T20:00:00-08:00"
    if expected_status == "confirmation_required":
        assert event.temporal.reason == "Location evidence conflicts; confirm the event timezone."
        assert "needs confirmation" in result.warnings[0]
    else:
        assert result.warnings == []


@pytest.mark.parametrize(("changes", "expected_status", "start_zone", "end_zone"), [
    (
        {"title": "Flight AA1234",
         "start_location_type": "airport", "start_location_value": "LAX"},
        "confirmation_required", "America/Los_Angeles", "America/Los_Angeles",
    ),
    (
        {"title": "Flight AA1234",
         "end_location_type": "airport", "end_location_value": "JFK"},
        "confirmation_required", "America/Los_Angeles", "America/New_York",
    ),
    (
        {"title": "Inbound event",
         "end_location_type": "airport", "end_location_value": "JFK"},
        "confirmation_required", "America/Los_Angeles", "America/New_York",
    ),
    (
        {"title": "Flight AA1234"},
        "confirmation_required", "America/Los_Angeles", "America/Los_Angeles",
    ),
    (
        {"title": "AA1234", "location": "LAX → JFK",
         "start_location_type": "airport", "start_location_value": "LAX"},
        "confirmation_required", "America/Los_Angeles", "America/Los_Angeles",
    ),
    (
        {"title": "Flight AA1234",
         "start_location_type": "airport", "start_location_value": "LAX",
         "end_location_type": "airport", "end_location_value": "JFK"},
        "resolved", "America/Los_Angeles", "America/New_York",
    ),
    (
        {"title": "Airport birthday",
         "start_location_type": "airport", "start_location_value": "LAX"},
        "resolved", "America/Los_Angeles", "America/Los_Angeles",
    ),
])
def test_partial_travel_itineraries_require_both_endpoint_locations(
    changes, expected_status, start_zone, end_zone,
):
    changes = {**changes, "end_local": "2027-12-25T23:30:00"}
    outcome = parse_one(**changes)
    event, = outcome.events
    assert (event.temporal.start_zone, event.temporal.end_zone) == (start_zone, end_zone)
    assert event.temporal.status == expected_status
    assert event.temporal.travel_incomplete is (expected_status == "confirmation_required")
    if expected_status == "confirmation_required":
        assert event.temporal.reason == (
            "Travel itinerary has an unverified endpoint; confirm both timezones."
        )
        assert "needs confirmation" in outcome.warnings[0]
    else:
        assert event.temporal.reason == ""
        assert not outcome.warnings


@pytest.mark.parametrize(("legacy", "expected_status"), [
    ({}, "resolved"),
    ({"start": None, "end": ""}, "resolved"),
    ({"start": "2027-12-25T18:00:00-05:00",
      "end": "2027-12-25T20:00:00-05:00"}, "resolved"),
    ({"start": "2027-12-25T17:00:00-08:00"}, "confirmation_required"),
    ({"end": "2027-12-25T21:00:00-08:00"}, "confirmation_required"),
    ({"start": "not-a-datetime"}, "confirmation_required"),
    ({"end": 42}, "confirmation_required"),
])
def test_optional_legacy_clocks_cannot_silently_disagree_with_structured_source(
    legacy, expected_status,
):
    event, = parse_one(**legacy).events
    assert event.temporal.status == expected_status
    assert (event.start, event.end) == (
        "2027-12-25T18:00:00-08:00", "2027-12-25T20:00:00-08:00",
    )
    if expected_status == "confirmation_required":
        assert event.temporal.reason == (
            "Structured and legacy clocks disagree; confirm the event time."
        )
    else:
        assert event.temporal.reason == ""


def test_partial_structured_field_omissions_do_not_break_location_free_fallback():
    raw = structured()
    for name in ("start_location_type", "start_location_value",
                 "end_location_type", "end_location_value",
                 "start_time_zone", "end_time_zone",
                 "uses_reference_time_zone", "time_zone_basis", "location",
                 "assumptions", "confidence"):
        raw.pop(name, None)
    outcome = parser.parse_ai_data(
        {"events": [raw]}, time_zone="America/Los_Angeles",
    )
    event, = outcome.events
    assert outcome.warnings == []
    assert event.temporal.status == "resolved"
    assert event.temporal.source == "ha_fallback"
    assert (event.start, event.end) == (
        "2027-12-25T18:00:00-08:00", "2027-12-25T20:00:00-08:00",
    )


@pytest.mark.parametrize("missing_field", ["start_local", "end_local"])
def test_required_structured_clocks_cannot_be_half_present(missing_field):
    raw = structured()
    del raw[missing_field]
    outcome = parser.parse_ai_data(
        {"events": [raw]}, time_zone="America/Los_Angeles",
    )
    assert outcome.events == []
    assert "invalid local date/time" in outcome.warnings[0]


async def test_conflicting_location_or_partial_flight_is_blocked_at_calendar_write(monkeypatch):
    from unittest.mock import AsyncMock
    for event in (
        parse_one(
            start_location_type="city", start_location_value="Encinitas, CA",
            location="Montréal, Québec, Canada",
        ).events[0],
        parse_one(
            title="Flight AA1234",
            start_location_type="airport", start_location_value="LAX",
        ).events[0],
        parse_one(
            start="2027-12-25T19:00:00-08:00",
        ).events[0],
    ):
        backend = _Backend()
        store = _store(monkeypatch, backend)
        await store.async_load()
        pending = (await store.async_add(source_text="source", events=[event])).pending
        assert pending is not None
        writer = AsyncMock()
        with pytest.raises(PendingEventEditError, match="Confirm the event timezone"):
            await store.async_approve_event(
                pending.id, pending.events[0].id, writer,
                expected_event=pending.events[0],
            )
        writer.assert_not_awaited()


def test_multi_event_provider_results_keep_valid_drafts_and_block_conflicts():
    safe = structured(title="Local dinner", location="")
    conflict = structured(
        title="Festival", start_location_type="city",
        start_location_value="Encinitas, CA",
        location="Montréal, Québec, Canada",
    )
    malformed = {**structured(title="Incomplete flight"), "end_local": None}
    result = parser.parse_ai_data(
        {"events": [safe, conflict, malformed]},
        source_text="Three unrelated events", time_zone="America/Los_Angeles",
    )
    assert len(result.events) == 2
    assert [event.title for event in result.events] == ["Local dinner", "Festival"]
    assert [event.temporal.status for event in result.events] == [
        "resolved", "confirmation_required",
    ]
    assert len(result.warnings) == 2
    assert "needs confirmation" in result.warnings[0]
    assert "invalid local date/time" in result.warnings[1]



def test_travel_confirmation_flag_roundtrips_and_rejects_nonboolean_metadata():
    event, = parse_one(title="Flight AA1234",
                       start_location_type="airport", start_location_value="LAX").events
    assert event.temporal.travel_incomplete is True
    restored = EventDraft.from_mapping(event.as_dict())
    assert restored.temporal.travel_incomplete is True
    state = event.temporal.as_dict()
    del state["travel_incomplete"]
    assert zones.TemporalEvidence.from_dict(state).travel_incomplete is False
    for invalid in (None, "true", 1, [], {}):
        with pytest.raises(ValueError, match="invalid temporal review fields"):
            zones.TemporalEvidence.from_dict({
                **event.temporal.as_dict(), "travel_incomplete": invalid,
            })


async def test_incomplete_trip_requires_explicit_end_zone_even_for_direct_service(monkeypatch):
    event, = parse_one(
        title="Flight AA1234", end_local="2027-12-25T23:30:00",
        start_location_type="airport", start_location_value="LAX",
    ).events
    with pytest.raises(ValueError, match="Confirm departure and arrival timezones independently"):
        zones.confirm_zone(event.temporal, "America/Los_Angeles")
    backend = _Backend()
    store = _store(monkeypatch, backend)
    await store.async_load()
    pending = (await store.async_add(source_text="source", events=[event])).pending
    original = pending.events[0]
    with pytest.raises(PendingEventEditError, match="Confirm departure and arrival timezones independently"):
        await store.async_confirm_timezone(
            pending.id, original.id, "America/Los_Angeles",
            expected_event=original,
        )
    assert store.get_event(pending.id, original.id) == original
    confirmed = await store.async_confirm_timezone(
        pending.id, original.id, "America/Los_Angeles",
        end_zone_name="America/New_York", expected_event=original,
    )
    assert confirmed.draft.start == "2027-12-25T18:00:00-08:00"
    assert confirmed.draft.end == "2027-12-25T23:30:00-05:00"
    assert confirmed.draft.temporal.travel_incomplete is True
    assert confirmed.draft.temporal.status == "resolved"
    reloaded = PendingImport.from_dict(store.get(pending.id).as_dict())
    assert reloaded.events[0].draft == confirmed.draft



def test_printed_flight_zone_conflict_is_caught_when_ai_omits_explicit_field():
    """Manual AI replay: omitted extraction must not erase printed evidence."""
    source = (
        "Flight AA1234 — July 10, 2028\n"
        "Depart LAX at 8:00 AM local time.\n"
        "Arrive JFK at 4:30 PM local time.\n"
        "All times are in America/Los_Angeles."
    )
    result = parser.parse_ai_data(
        {"events": [structured(
            title="Flight AA1234",
            start_local="2028-07-10T08:00:00",
            end_local="2028-07-10T16:30:00",
            start_location_type="airport", start_location_value="LAX",
            end_location_type="airport", end_location_value="JFK",
            explicit_timezone="",
            location="LAX to JFK",
        )]},
        source_text=source, time_zone="America/Los_Angeles",
    )
    event, = result.events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.source == "explicit"
    assert event.temporal.explicit_timezone == "America/Los_Angeles"
    assert event.temporal.start_location_value == "LAX"
    assert event.temporal.end_location_value == "JFK"
    assert (event.temporal.start_zone, event.temporal.end_zone) == (
        "America/Los_Angeles", "America/Los_Angeles",
    )
    assert (event.start, event.end) == (
        "2028-07-10T08:00:00-07:00", "2028-07-10T16:30:00-07:00",
    )
    assert event.temporal.reason == "Confirm the source location's timezone."
    assert "needs confirmation" in result.warnings[0]


@pytest.mark.parametrize("source", [
    "Meeting at 6 PM.\nTime zone: America/New_York",
    "Meeting at 6 PM.\nTimezone is America/New_York",
    "Meeting at 6 PM.\nAll event times are in America/New_York.",
])
def test_printed_explicit_zone_recovers_from_missing_ai_field_without_geography(source):
    event, = parser.parse_ai_data(
        {"events": [structured(explicit_timezone="", location="")]},
        source_text=source, time_zone="America/Los_Angeles",
    ).events
    assert event.temporal.status == "resolved"
    assert event.temporal.source == "explicit"
    assert event.temporal.explicit_timezone == "America/New_York"
    assert (event.start, event.end) == (
        "2027-12-25T18:00:00-05:00", "2027-12-25T20:00:00-05:00",
    )


def test_multiple_printed_global_zones_block_instead_of_choosing_one():
    source = (
        "Christmas dinner on December 25, 2027.\n"
        "All times are in America/Los_Angeles.\n"
        "Time zone: America/New_York"
    )
    outcome = parser.parse_ai_data(
        {"events": [structured(explicit_timezone="")]},
        source_text=source, time_zone="America/Los_Angeles",
    )
    event, = outcome.events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.source == "ha_fallback"
    assert event.temporal.explicit_timezone == ""
    assert event.temporal.reason == (
        "Source declares conflicting timezones; confirm the event timezone."
    )
    assert "needs confirmation" in outcome.warnings[0]


def test_incidental_iana_zone_in_source_is_not_an_explicit_event_declaration():
    outcome = parser.parse_ai_data(
        {"events": [structured(explicit_timezone="")]},
        source_text="Christmas dinner at 6 PM. The organizer works in America/New_York.",
        time_zone="America/Los_Angeles",
    )
    event, = outcome.events
    assert event.temporal.status == "resolved"
    assert event.temporal.source == "ha_fallback"
    assert event.temporal.explicit_timezone == ""
    assert event.start == "2027-12-25T18:00:00-08:00"
    assert outcome.warnings == []


def test_ai_explicit_zone_already_present_does_not_get_overridden():
    outcome = parser.parse_ai_data(
        {"events": [structured(explicit_timezone="America/New_York")]},
        source_text="Time zone: America/New_York",
        time_zone="America/Los_Angeles",
    )
    event, = outcome.events
    assert event.temporal.status == "resolved"
    assert event.temporal.explicit_timezone == "America/New_York"
    assert event.temporal.start_zone == "America/New_York"
    assert outcome.warnings == []


async def test_printed_flight_conflict_cannot_write_until_reviewer_confirms(monkeypatch):
    source = (
        "Flight AA1234 — July 10, 2028\n"
        "Depart LAX at 8:00 AM local time.\n"
        "Arrive JFK at 4:30 PM local time.\n"
        "All times are in America/Los_Angeles."
    )
    event, = parser.parse_ai_data(
        {"events": [structured(
            title="Flight AA1234", start_local="2028-07-10T08:00:00",
            end_local="2028-07-10T16:30:00",
            start_location_type="airport", start_location_value="LAX",
            end_location_type="airport", end_location_value="JFK",
            explicit_timezone="",
        )]},
        source_text=source, time_zone="America/Los_Angeles",
    ).events
    backend = _Backend()
    store = _store(monkeypatch, backend)
    await store.async_load()
    pending = (await store.async_add(source_text=source, events=[event])).pending
    original = pending.events[0]
    writer = AsyncMock()
    with pytest.raises(PendingEventEditError, match="Confirm the event timezone"):
        await store.async_approve_event(
            pending.id, original.id, writer, expected_event=original,
        )
    writer.assert_not_awaited()


def test_bare_postal_codes_are_not_us_evidence():
    assert zones._place_zone("postal_code", "75001") is None
    assert zones._place_zone("postal_code", "92024") is None
    assert zones._place_zone("postal_code", "92024, US") == "America/Los_Angeles"
    event, = parse_one(start_location_type="postal_code",
                       start_location_value="75001").events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.source == "ha_fallback"


def test_country_parentheses_cannot_be_accepted_as_airport():
    assert zones._place_zone("city", "Toronto (CAN)") is None
    event, = parse_one(start_location_type="city", start_location_value="Toronto (CAN)").events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.start_zone == "America/Los_Angeles"


def test_printed_source_location_recovered_without_ai_location_fields():
    source = "Meeting\nLocation: Sydney, NSW, Australia"
    event, = parser.parse_ai_data({"events": [structured()]},
        source_text=source, time_zone="America/Los_Angeles").events
    assert event.temporal.status == "resolved"
    assert event.temporal.start_zone == "Australia/Sydney"
    assert event.start == "2027-12-25T18:00:00+11:00"


def test_multi_event_shared_explicit_declaration_is_preserved():
    result = parser.parse_ai_data({"events": [
        structured(title="First"), structured(title="Second"),
    ]}, source_text="All event times are in America/New_York",
        time_zone="America/Los_Angeles")
    assert len(result.events) == 2
    for event in result.events:
        assert event.temporal.status == "resolved"
        assert event.temporal.start_zone == "America/New_York"
        assert event.start == "2027-12-25T18:00:00-05:00"


def test_printed_declaration_conflicting_with_ai_is_blocked():
    source = "Time zone: America/New_York\nOrganizer works in America/Los_Angeles"
    event, = parser.parse_ai_data({"events": [structured(
        explicit_timezone="America/Los_Angeles",
    )]}, source_text=source, time_zone="America/Los_Angeles").events
    assert event.temporal.status == "confirmation_required"
    assert "conflicting timezones" in event.temporal.reason


@pytest.mark.parametrize("abbreviation", ["CET", "EST", "MST", "HST"])
def test_zoneinfo_abbreviations_still_require_confirmation(abbreviation):
    event, = parse_one(explicit_timezone=abbreviation).events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.explicit_timezone == abbreviation


def test_distinct_explicit_zones_are_distinct_pending_fingerprints():
    from custom_components.daylight_calendar_import.dedup import event_fingerprint
    first, = parse_one(start_local="2027-11-07T01:15:00",
                       end_local="2027-11-07T01:45:00",
                       explicit_timezone="America/New_York").events
    second, = parse_one(start_local="2027-11-07T01:15:00",
                        end_local="2027-11-07T01:45:00",
                        explicit_timezone="America/Los_Angeles").events
    assert first.temporal.status == second.temporal.status == "confirmation_required"
    assert event_fingerprint(first) != event_fingerprint(second)


def test_unresolved_dateline_interval_remains_reviewable():
    event, = parse_one(title="Plane ticket",
                       start_local="2027-07-15T17:00:00",
                       end_local="2027-07-15T11:00:00",
                       start_location_type="airport", start_location_value="INVALID",
                       end_location_type="airport", end_location_value="MISSING").events
    assert event.temporal.status == "confirmation_required"
    assert (event.temporal.start_local, event.temporal.end_local) == (
        "2027-07-15T17:00:00", "2027-07-15T11:00:00")


def test_multileg_source_does_not_override_known_destination():
    source = ("Departure: Los Angeles (LAX)\nArrival: New York (JFK)\\n" +
              "Baggage terms " + "x" * 600 +
              "\nDeparture: New York (JFK)\nArrival: London (LHR)")
    event, = parser.parse_ai_data({"events": [structured(
        title="LAX to LHR journey",
        start_local="2027-07-15T08:00:00",
        end_local="2027-07-16T08:00:00",
        start_location_type="airport", start_location_value="LAX",
        end_location_type="airport", end_location_value="LHR",
    )]}, source_text=source, time_zone="America/Los_Angeles").events
    assert event.temporal.end_zone == "Europe/London"
    assert event.end == "2027-07-16T08:00:00+01:00"


@pytest.mark.parametrize("ai_zone", ["", "America/New_York"])
def test_legacy_named_source_record_protects_structured_event_zone(ai_zone):
    source = ("Christmas dinner\nDate: December 25, 2027\n"
              "Time: 6:00 PM - 8:00 PM (America/New_York)")
    event, = parser.parse_ai_data({"events": [structured(explicit_timezone=ai_zone)]},
        source_text=source, time_zone="America/Los_Angeles").events
    assert event.temporal.status == "resolved"
    assert event.temporal.start_zone == "America/New_York"
    assert event.start == "2027-12-25T18:00:00-05:00"


def test_legacy_named_source_record_conflicting_ai_zone_is_blocked():
    source = ("Christmas dinner\nDate: December 25, 2027\n"
              "Time: 6:00 PM - 8:00 PM (America/New_York)\n"
              "Organizer works in America/Los_Angeles")
    # The full-match backstop intentionally does not match extra prose.
    # A direct declared source zone must still defeat incidental AI claims.
    source = "Time zone: America/New_York\n" + source
    event, = parser.parse_ai_data({"events": [structured(
        explicit_timezone="America/Los_Angeles")]},
        source_text=source, time_zone="America/Los_Angeles").events
    assert event.temporal.status == "confirmation_required"


async def test_store_minute_precision_iso_edit_requires_timezone_reconfirmation(monkeypatch):
    store = _store(monkeypatch, _Backend())
    await store.async_load()
    original, = parse_one(start_location_type="venue",
                           start_location_value="Community Hall").events
    pending = (await store.async_add(source_text="minute precision",
                                     events=[original])).pending
    old = pending.events[0]
    modified = replace(original, start="2027-12-25T17:00-08:00",
                       end="2027-12-25T19:00-08:00", temporal=None)
    changed = await store.async_edit_event(pending.id, old.id, modified,
                                           expected_event=old)
    assert changed.draft.temporal.status == "confirmation_required"
    assert changed.draft.temporal.start_local == "2027-12-25T17:00:00"
    assert changed.draft.temporal.end_local == "2027-12-25T19:00:00"
    assert store.get_event(pending.id, old.id) == changed


def test_labeled_flight_does_not_overwrite_distinct_typed_airports():
    source = "Departure: Los Angeles (LAX) 8 AM Arrival: New York (JFK) 4 PM"
    event, = parser.parse_ai_data({"events": [structured(
        title="Connecting flight",
        start_local="2027-07-15T08:00:00",
        end_local="2027-07-16T08:00:00",
        start_location_type="airport", start_location_value="LAX",
        end_location_type="airport", end_location_value="LHR",
    )]}, source_text=source, time_zone="America/Los_Angeles").events
    assert event.temporal.end_zone == "Europe/London"
    assert event.end == "2027-07-16T08:00:00+01:00"



def test_multiple_flights_retain_travel_review_gate_without_cross_event_airport_recovery():
    source = ("Flight AA1234 Departure: Los Angeles (LAX) Arrival: New York (JFK)\\n"
              "Flight AA5678 Departure: New York (JFK) Arrival: London (LHR)")
    extracted = [
        structured(title="AA1234", start_location_type="airport",
                   start_location_value="LAX"),
        structured(title="AA5678", start_location_type="airport",
                   start_location_value="JFK"),
    ]
    result = parser.parse_ai_data({"events": extracted}, source_text=source,
                                  time_zone="America/Los_Angeles")
    assert len(result.events) == 2
    for event, departure in zip(result.events, ("America/Los_Angeles", "America/New_York"), strict=True):
        assert event.temporal.start_zone == departure
        assert event.temporal.end_zone == departure
        assert event.temporal.status == "confirmation_required"
        assert event.temporal.travel_incomplete is True
        assert "Travel itinerary" in event.temporal.reason


def test_conflicting_printed_flight_endpoint_requires_review_without_overwriting_ai_fields():
    source = "Departure: Los Angeles (LAX) 8 AM Arrival: New York (JFK) 4 PM"
    event, = parser.parse_ai_data({"events": [structured(
        title="Flight", start_location_type="airport", start_location_value="LAX",
        end_location_type="airport", end_location_value="LHR",
    )]}, source_text=source, time_zone="America/Los_Angeles").events
    assert event.temporal.end_location_value == "LHR"
    assert event.temporal.end_zone == "Europe/London"
    assert event.temporal.status == "confirmation_required"
    assert "Location evidence conflicts" in event.temporal.reason


def test_source_venue_never_erases_unresolved_independent_arrival():
    event, = parser.parse_ai_data({"events": [structured(
        title="Flight", start_location_type="airport",
        start_location_value="Los Angeles International Airport",
        end_location_type="airport",
        end_location_value="John F Kennedy International Airport",
    )]}, source_text="Location: Los Angeles International Airport (LAX)",
        time_zone="America/Los_Angeles").events
    assert event.temporal.start_location_value == "Los Angeles International Airport"
    assert event.temporal.end_location_value == "John F Kennedy International Airport"
    assert event.temporal.start_zone == "America/Los_Angeles"
    assert event.temporal.end_zone == "America/Los_Angeles"
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.reason == "Location evidence conflicts; confirm the event timezone."


def test_unresolvable_printed_venue_without_ai_geography_blocks_fallback():
    event, = parser.parse_ai_data({"events": [structured()]},
        source_text="Location: Community Hall",
        time_zone="America/Los_Angeles").events
    assert event.temporal.start_zone == "America/Los_Angeles"
    assert event.temporal.end_zone == "America/Los_Angeles"
    assert event.temporal.source == "ha_fallback"
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.reason == "The printed venue could not be verified; confirm the event timezone."


def test_all_day_empty_optional_structured_clocks_are_not_discarded():
    event = {"title": "Christmas Day", "all_day": True,
             "start": "2027-12-25", "end": "2027-12-26",
             "start_local": "", "end_local": "", "location": "",
             "confidence": 0.9}
    result = parser.parse_ai_data({"events": [event]},
        time_zone="America/Los_Angeles")
    assert result.warnings == []
    draft, = result.events
    assert draft.all_day is True
    assert (draft.start, draft.end) == ("2027-12-25", "2027-12-26")
    assert draft.temporal is None


@pytest.mark.parametrize(("clock", "other"), [
    ("2027-12-25T18:00:00", ""),
    ("", "2027-12-25T20:00:00"),
])
def test_all_day_rejects_actual_structured_timed_clock(clock, other):
    raw = {"title": "Christmas Day", "all_day": True,
           "start": "2027-12-25", "end": "2027-12-26",
           "start_local": clock, "end_local": other,
           "confidence": 0.9}
    result = parser.parse_ai_data({"events": [raw]},
                                  time_zone="America/Los_Angeles")
    assert result.events == []
    assert "structured local clocks require a timed event" in result.warnings[0]


def test_reversed_nontravel_source_without_missing_zones_is_not_kept():
    result = parse_one(start_local="2027-12-25T20:00:00",
                       end_local="2027-12-25T18:00:00")
    assert result.events == []
    assert "end must be after start" in result.warnings[0]


@settings(max_examples=24, deadline=None)
@given(
    venues=st.lists(st.sampled_from(["Sydney, NSW, Australia", "London, UK",
                                    "Community Hall", "Encinitas, CA"]),
                    min_size=1, max_size=3, unique=True),
    count=st.integers(min_value=2, max_value=4),
    ha_zone=st.sampled_from(["America/Los_Angeles", "America/New_York", "UTC"]),
)
def test_unassociated_document_venues_never_authorize_ha_fallback(venues, count, ha_zone):
    source = "\n".join(f"Location: {venue}" for venue in venues)
    result = parser.parse_ai_data(
        {"events": [structured(title=f"Meeting {index}") for index in range(count)]},
        source_text=source, time_zone=ha_zone,
    )
    assert len(result.events) == count
    assert len(result.warnings) == count
    for event in result.events:
        assert event.temporal.status == "confirmation_required"
        assert event.temporal.source == "ha_fallback"
        assert event.temporal.start_zone == event.temporal.end_zone == ha_zone
        assert event.temporal.start_location_value == ""
        assert event.temporal.end_location_value == ""
        assert event.temporal.reason == "The printed venue could not be verified; confirm the event timezone."
        assert EventDraft.from_mapping(event.as_dict()) == event


@settings(max_examples=12, deadline=None)
@given(
    first=st.sampled_from(["Sydney, NSW, Australia", "London, UK", "Community Hall"]),
    second=st.sampled_from(["Encinitas, CA", "Paris, France", "School gym"]),
    explicit=st.sampled_from(["", "America/Los_Angeles"]),
)
def test_adding_unassociated_venue_evidence_cannot_remove_confirmation(first, second, explicit):
    source = f"Location: {first}\nWhere: {second}"
    if explicit:
        source += f"\nTime zone: {explicit}"
    event, = parser.parse_ai_data({"events": [structured(explicit_timezone=explicit)]},
        source_text=source, time_zone="America/Los_Angeles").events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.start_location_value == event.temporal.end_location_value == ""
    assert event.temporal.reason == "The printed venue could not be verified; confirm the event timezone."


def test_source_only_recovered_venue_survives_fold_review_and_dedup():
    from custom_components.daylight_calendar_import.dedup import event_fingerprint
    events = []
    for location, zone in [
        ("Los Angeles International Airport (LAX)", "America/Los_Angeles"),
        ("John F. Kennedy International Airport (JFK)", "America/New_York"),
    ]:
        event, = parser.parse_ai_data({"events": [structured(
            start_local="2027-11-07T01:15:00", end_local="2027-11-07T01:45:00",
        )]}, source_text=f"Location: {location}", time_zone="America/Los_Angeles").events
        assert event.temporal.status == "confirmation_required"
        assert event.temporal.start_location_value == location
        assert event.temporal.start_location_type == "venue"
        assert event.location == location
        assert event.temporal.start_zone == event.temporal.end_zone == zone
        assert EventDraft.from_mapping(event.as_dict()) == event
        events.append(event)
    assert event_fingerprint(events[0]) != event_fingerprint(events[1])


@pytest.mark.parametrize(("start_place", "end_place", "arrival_zone", "arrival_offset"), [
    ("LAX", "JFK", "America/New_York", "-05:00"),
    ("LAX", "SFO", "America/Los_Angeles", "-08:00"),
])
def test_complete_travel_confirmation_requires_independent_arrival(start_place, end_place, arrival_zone, arrival_offset):
    event, = parse_one(title="Flight", start_local="2027-11-07T01:30:00",
        end_local="2027-11-07T10:00:00", start_location_type="airport",
        start_location_value=start_place, end_location_type="airport",
        end_location_value=end_place).events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.travel_incomplete is False
    with pytest.raises(ValueError, match="Confirm departure and arrival timezones independently"):
        zones.confirm_zone(event.temporal, "America/Los_Angeles", start_occurrence="first")
    start, end, evidence = zones.confirm_zone(event.temporal, "America/Los_Angeles",
        end_zone_name=arrival_zone, start_occurrence="first")
    assert start == "2027-11-07T01:30:00-07:00"
    assert end == "2027-11-07T10:00:00" + arrival_offset
    assert evidence.status == "resolved"
    assert evidence.end_zone == arrival_zone


def test_distinct_zone_without_location_still_requires_independent_arrival():
    event, = parse_one(start_local="2027-11-07T01:15:00",
                       end_local="2027-11-07T01:45:00").events
    evidence = replace(event.temporal, end_zone="America/New_York")
    with pytest.raises(ValueError, match="Confirm departure and arrival timezones independently"):
        zones.confirm_zone(evidence, "America/Los_Angeles", start_occurrence="first")


@pytest.mark.parametrize("location", ["Community Hall", "Local school", "At home", "Hall, West Wing"])
def test_untyped_display_location_is_evidence_even_without_punctuation(location):
    event, = parser.parse_ai_data(
        {"events": [structured(location=location)]},
        source_text=f"We meet at {location}", time_zone="America/Los_Angeles",
    ).events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.source == "ha_fallback"
    assert event.temporal.start_location_type == "unknown"
    assert event.temporal.start_location_value == location
    assert event.temporal.start_zone == "America/Los_Angeles"
    assert event.location == location


@pytest.mark.parametrize(("start", "end", "expected_end"), [
    ("2027-03-14T02:30:00", "2027-03-14T03:00:00", "2027-03-14T03:31:00-07:00"),
    ("2027-03-14T02:15:00", "2027-03-14T02:45:00", "2027-03-14T02:45:00-08:00"),
    ("2027-03-14T02:30:00", "2027-03-14T03:30:00", "2027-03-14T03:31:00-07:00"),
])
def test_gap_draft_retains_original_clocks_and_valid_provisional_range(start, end, expected_end):
    event, = parse_one(start_local=start, end_local=end).events
    assert event.temporal.status == "correction_required"
    assert event.temporal.start_local == start
    assert event.temporal.end_local == end
    assert event.start == start + "-08:00"
    assert event.end == expected_end
    assert datetime.fromisoformat(event.end) > datetime.fromisoformat(event.start)
    assert EventDraft.from_mapping(event.as_dict()) == event
    with pytest.raises(ValueError, match="DST occurrence"):
        zones.confirm_zone(event.temporal, "America/Los_Angeles")


@pytest.mark.parametrize(("explicit", "offset", "canonical"), [
    ("+02:00", "+02:00", "UTC+02:00"),
    ("-0430", "-04:30", "UTC-04:30"),
    ("+5:45", "+05:45", "UTC+05:45"),
])
@pytest.mark.parametrize("claimed", [True, False])
def test_printed_numeric_offsets_work_with_or_without_ai_claim(explicit, offset, canonical, claimed):
    event, = parser.parse_ai_data(
        {"events": [structured(explicit_timezone=explicit if claimed else "")]},
        source_text=f"Time zone: {explicit}", time_zone="America/Los_Angeles",
    ).events
    assert event.temporal.status == "resolved"
    assert event.temporal.explicit_timezone == explicit
    assert event.temporal.start_zone == event.temporal.end_zone == canonical
    assert event.start == "2027-12-25T18:00:00" + offset
    assert event.end == "2027-12-25T20:00:00" + offset


@pytest.mark.parametrize("offset", ["+24:00", "-02:60", "+12345", "+02:000"])
def test_invalid_bare_offsets_never_become_valid_fixed_zones(offset):
    assert zones._fixed_zone(offset) is None


def test_omitted_printed_abbreviation_is_still_unresolved():
    event, = parser.parse_ai_data(
        {"events": [structured()]}, source_text="Time zone: PST",
        time_zone="America/Los_Angeles",
    ).events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.explicit_timezone == "PST"


@pytest.mark.parametrize("display", ["", "Community Hall"])
def test_virtual_classification_cannot_erase_independent_physical_venue(display):
    event, = parser.parse_ai_data(
        {"events": [structured(start_location_type="virtual", location=display)]},
        source_text="Location: Sydney, NSW, Australia", time_zone="America/Los_Angeles",
    ).events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.start_zone == "America/Los_Angeles"
    assert event.temporal.source == "ha_fallback"
    assert event.temporal.start_location_value == display


def test_local_zone_declaration_cannot_be_assigned_to_unassociated_sibling():
    result = parser.parse_ai_data(
        {"events": [structured(title="A"), structured(title="B")]},
        source_text="Event A\nTime zone: America/New_York\nEvent B",
        time_zone="America/Los_Angeles",
    )
    assert len(result.events) == 2
    assert len(result.warnings) == 2
    for event in result.events:
        assert event.temporal.status == "confirmation_required"
        assert event.temporal.start_zone == "America/Los_Angeles"
        assert event.temporal.explicit_timezone == ""
        assert event.temporal.reason == "Source declares conflicting timezones; confirm the event timezone."


@pytest.mark.parametrize("malformed", [False, 0, [], {}])
def test_malformed_falsey_structured_clock_never_downgrades_to_legacy(malformed):
    result = parse_one(start_local=malformed, end_local="",
                       start="2027-12-25T18:00:00-08:00",
                       end="2027-12-25T20:00:00-08:00")
    assert result.events == []
    assert "invalid local date/time" in result.warnings[0]


@pytest.mark.parametrize("structured_clocks", [False, True])
def test_provider_cannot_forge_authoritative_review_metadata(structured_clocks):
    event, = parse_one().events
    raw = structured(temporal=event.temporal.as_dict())
    if not structured_clocks:
        raw.pop("start_local")
        raw.pop("end_local")
        raw.update(start=event.start, end=event.end)
    result = parser.parse_ai_data({"events": [raw]}, time_zone="America/Los_Angeles")
    assert not result.events
    assert "Provider cannot supply authoritative timezone review metadata" in result.warnings[0]


def test_out_of_range_structured_instant_does_not_discard_other_events():
    result = parser.parse_ai_data({"events": [
        structured(title="Invalid", start_local="0001-01-01T00:00:00",
                   end_local="0001-01-01T01:00:00", explicit_timezone="UTC+14:00"),
        structured(title="Valid"),
    ]}, time_zone="America/Los_Angeles")
    assert [event.title for event in result.events] == ["Valid"]
    assert len(result.warnings) == 1
    assert "event 0 is invalid" in result.warnings[0]


async def test_final_temporal_fingerprint_prevents_duplicate_edits(monkeypatch):
    from custom_components.daylight_calendar_import.dedup import event_fingerprint
    store = _store(monkeypatch, _Backend())
    await store.async_load()
    first, = parse_one(title="First", start_location_type="venue",
                       start_location_value="Community Hall").events
    second = replace(first, title="Second")
    pending = (await store.async_add(source_text="source", events=[first, second])).pending
    a, b = pending.events
    assert event_fingerprint(a.draft) != event_fingerprint(b.draft)
    with pytest.raises(PendingEventEditError, match="Edited event duplicates"):
        await store.async_edit_event(
            pending.id, b.id, replace(b.draft, title="First", temporal=None),
            expected_event=b,
        )
    assert store.get(pending.id) == pending
    assert store.get_event(pending.id, a.id) == a
    assert store.get_event(pending.id, b.id) == b


async def test_resolved_location_edit_requires_reconfirmation_before_write(monkeypatch):
    store = _store(monkeypatch, _Backend())
    await store.async_load()
    draft, = parse_one(start_location_type="city", start_location_value="San Diego, CA").events
    assert draft.temporal.status == "resolved"
    pending = (await store.async_add(source_text="source", events=[draft])).pending
    original = pending.events[0]
    edited = await store.async_edit_event(
        pending.id, original.id,
        replace(draft, location="New York, NY", temporal=None), expected_event=original,
    )
    assert edited.draft.temporal.status == "confirmation_required"
    assert edited.draft.temporal.reason == "Edited location needs timezone reconfirmation"
    assert edited.draft.temporal.start_local == draft.temporal.start_local
    writer = AsyncMock()
    with pytest.raises(PendingEventEditError, match="Confirm the event timezone"):
        await store.async_approve_event(pending.id, edited.id, writer)
    writer.assert_not_awaited()
    confirmed = await store.async_confirm_timezone(
        pending.id, edited.id, "America/New_York", expected_event=edited,
    )
    assert confirmed.draft.start == "2027-12-25T18:00:00-05:00"
    assert confirmed.draft.end == "2027-12-25T20:00:00-05:00"
    assert confirmed.draft.temporal.status == "resolved"


async def test_confirmation_with_unrepresentable_utc_instant_is_atomic(monkeypatch):
    store = _store(monkeypatch, _Backend())
    await store.async_load()
    draft, = parse_one(start_local="0001-01-01T00:00:00",
                       end_local="0001-01-01T01:00:00",
                       start_location_type="venue", start_location_value="Unknown").events
    assert draft.temporal.status == "confirmation_required"
    pending = (await store.async_add(source_text="source", events=[draft])).pending
    original = pending.events[0]
    with pytest.raises(PendingEventEditError):
        await store.async_confirm_timezone(
            pending.id, original.id, "UTC+14:00", expected_event=original,
        )
    assert store.get_event(pending.id, original.id) == original
    writer = AsyncMock()
    with pytest.raises(PendingEventEditError, match="Confirm the event timezone"):
        await store.async_approve_event(pending.id, original.id, writer)
    writer.assert_not_awaited()


@pytest.mark.parametrize(("title", "source", "display"), [
    ("AA1234", "AA1234\nLAX → JFK\nJuly 15 08:00–16:30", ""),
    ("LAX → JFK", "", ""),
    ("AA1234", "", "LAX → JFK"),
    ("AA1234", "", "Departure flight"),
])
async def test_route_context_blocks_missing_arrival_before_calendar_write(monkeypatch, title, source, display):
    result = parser.parse_ai_data({"events": [structured(
        title=title, location=display, start_location_type="airport",
        start_location_value="LAX", start_local="2027-07-15T08:00:00",
        end_local="2027-07-15T16:30:00",
    )]}, source_text=source, time_zone="America/Los_Angeles")
    event, = result.events
    assert event.temporal.travel_incomplete is True
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.start_zone == event.temporal.end_zone == "America/Los_Angeles"
    assert event.temporal.end_location_value == ""
    assert event.end == "2027-07-15T16:30:00-07:00"
    store = _store(monkeypatch, _Backend())
    await store.async_load()
    item = (await store.async_add(source_text=source or title, events=[event])).pending
    original = item.events[0]
    writer = AsyncMock()
    with pytest.raises(PendingEventEditError, match="Confirm the event timezone"):
        await store.async_approve_event(item.id, original.id, writer)
    with pytest.raises(PendingEventEditError, match="Resolve all event timezones"):
        await store.async_process_events(item.id, writer)
    writer.assert_not_awaited()
    with pytest.raises(PendingEventEditError, match="Confirm departure and arrival timezones independently"):
        await store.async_confirm_timezone(item.id, original.id, "America/Los_Angeles")
    confirmed = await store.async_confirm_timezone(
        item.id, original.id, "America/Los_Angeles", end_zone_name="America/New_York",
        expected_event=original,
    )
    assert confirmed.draft.start == "2027-07-15T08:00:00-07:00"
    assert confirmed.draft.end == "2027-07-15T16:30:00-04:00"
    assert confirmed.draft.temporal.status == "resolved"


@pytest.mark.parametrize(("route", "start", "end", "status"), [
    ("LAX → JFK", "LAX", "JFK", "resolved"),
    ("LAX → JFK", "LAX", "LHR", "confirmation_required"),
    ("LAX → JFK", "LHR", "JFK", "confirmation_required"),
    ("ZZZZ → JFK", "LAX", "JFK", "resolved"),
    ("LAX → ZZZZ", "LAX", "JFK", "resolved"),
])
def test_scoped_route_conflict_never_replaces_extracted_airport_zones(route, start, end, status):
    event, = parser.parse_ai_data({"events": [structured(
        title="AA1234", start_local="2027-07-15T08:00:00",
        end_local="2027-07-15T20:30:00", start_location_type="airport",
        start_location_value=start, end_location_type="airport", end_location_value=end,
    )]}, source_text=route, time_zone="America/Los_Angeles").events
    assert event.temporal.status == status
    assert event.temporal.start_location_value == start
    assert event.temporal.end_location_value == end
    assert event.temporal.start_zone == {"LAX": "America/Los_Angeles", "LHR": "Europe/London"}[start]
    assert event.temporal.end_zone == {"JFK": "America/New_York", "LHR": "Europe/London"}[end]
    assert event.temporal.reason == (
        "Location evidence conflicts; confirm the event timezone." if status == "confirmation_required" else ""
    )


def test_multi_event_route_source_does_not_override_independent_complete_airports():
    result = parser.parse_ai_data({"events": [
        structured(title="AA1", start_location_type="airport", start_location_value="LAX",
                   end_location_type="airport", end_location_value="JFK", end_local="2027-12-25T23:30:00"),
        structured(title="AA2", start_location_type="airport", start_location_value="JFK",
                   end_location_type="airport", end_location_value="LHR", end_local="2027-12-26T08:30:00"),
    ]}, source_text="LAX → JFK\nJFK → LHR", time_zone="America/Los_Angeles")
    assert result.warnings == []
    assert [(e.temporal.status, e.temporal.start_zone, e.temporal.end_zone) for e in result.events] == [
        ("resolved", "America/Los_Angeles", "America/New_York"),
        ("resolved", "America/New_York", "Europe/London"),
    ]


@pytest.mark.parametrize(("start", "end", "initial_status", "zone"), [
    ("2027-12-25T18:00:00", "2027-12-25T20:00:00", "confirmation_required", "America/Los_Angeles"),
    ("2027-03-14T02:15:00", "2027-03-14T02:45:00", "correction_required", "America/Phoenix"),
])
async def test_timezone_confirmation_preserves_original_date_disclosures(monkeypatch, start, end, initial_status, zone):
    notes = [
        "Assumed 2027 because no year is printed in the invitation.",
        "The date and time zone were inferred from the reference date.",
        "Date inferred independently of the DST occurrence.",
    ]
    result = parse_one(start_local=start, end_local=end, start_location_type="venue",
                       start_location_value="Community Hall", assumptions=notes)
    event, = result.events
    assert event.temporal.status == initial_status
    assert list(result.event_assumptions[0][1:]) == notes
    backend = _Backend()
    store = _store(monkeypatch, backend)
    await store.async_load()
    item = (await store.async_add(source_text="source", events=[event],
                                  event_assumptions=result.event_assumptions)).pending
    original = item.events[0]
    confirmed = await store.async_confirm_timezone(item.id, original.id, zone, expected_event=original)
    assert confirmed.draft.temporal.status == "resolved"
    assert confirmed.date_time_assumptions == tuple(notes)
    restored = PendingImport.from_dict(store.get(item.id).as_dict())
    assert restored.events[0].date_time_assumptions == tuple(notes)


@pytest.mark.parametrize(("start", "end"), [
    ("2027-12-25T15:00:00", "2027-12-25T20:00:00"),
    ("2027-12-25T18:00:00", "2027-12-25T19:00:00"),
    ("2027-12-25T15:00:00", "2027-12-25T17:00:00"),
    ("2027-12-25T18:00:01", "2027-12-25T20:00:00"),
])
def test_structured_matched_source_record_rejects_wrong_printed_clock(start, end):
    source = ("Christmas dinner\nDate: December 25, 2027\n"
              "Time: 6:00 PM - 8:00 PM (America/New_York)")
    result = parser.parse_ai_data({"events": [structured(
        start_local=start, end_local=end,
    )]}, source_text=source, time_zone="America/Los_Angeles")
    assert result.events == []
    assert result.warnings == ["event 0 is invalid: Extracted clock time differs from the source time"]


def test_structured_matched_source_record_compares_original_gap_clocks():
    source = ("Christmas dinner\nDate: March 14, 2027\n"
              "Time: 2:30 am - 3:00 am (America/Los_Angeles)")
    event, = parser.parse_ai_data({"events": [structured(
        start_local="2027-03-14T02:30:00", end_local="2027-03-14T03:00:00",
    )]}, source_text=source, time_zone="America/New_York").events
    assert event.temporal.status == "correction_required"
    assert event.temporal.start_local == "2027-03-14T02:30:00"
    assert event.temporal.end_local == "2027-03-14T03:00:00"
    assert event.end == "2027-03-14T03:31:00-07:00"


@pytest.mark.parametrize("explicit", ["", "America/New_York"])
def test_unmatched_source_title_cannot_authorize_timezone_guess(explicit):
    source = ("Different event\nDate: December 25, 2027\n"
              "Time: 6:00 PM - 8:00 PM (America/New_York)")
    result = parser.parse_ai_data({"events": [structured(explicit_timezone=explicit)]},
        source_text=source, time_zone="America/Los_Angeles")
    event, = result.events
    assert event.temporal.status == "confirmation_required"
    assert event.temporal.explicit_timezone == explicit
    assert event.temporal.start_zone == (explicit or "America/Los_Angeles")
    assert event.temporal.reason == "Source declares conflicting timezones; confirm the event timezone."
    assert len(event.temporal.source_evidence_id) == 64
    assert result.warnings == ["event 0: Event timezone or DST occurrence needs confirmation."]


def test_unmatched_source_title_does_not_replace_existing_correction_status():
    source = ("Different event\nDate: March 14, 2027\n"
              "Time: 2:30 AM - 3:00 AM (America/Los_Angeles)")
    event, = parser.parse_ai_data({"events": [structured(
        start_local="2027-03-14T02:30:00", end_local="2027-03-14T03:00:00",
    )]}, source_text=source, time_zone="America/Los_Angeles").events
    assert event.temporal.status == "correction_required"
    assert event.temporal.reason == "The local time does not exist in this timezone."


@pytest.mark.parametrize(("first_source", "second_source", "equivalent_source"), [
    ("Location: Sydney, NSW, Australia", "Location: London, UK",
     "Where:  SYDNEY,  NSW, AUSTRALIA\nLocation: Sydney, NSW, Australia"),
    ("Time zone: Australia/Sydney", "Time zone: Europe/London",
     "Timezone is Australia/Sydney\nTime zone: Australia/Sydney"),
    ("LAX → JFK", "LAX → LHR", "LAX to JFK\nLAX → JFK"),
])
async def test_unassociated_printed_evidence_preserves_distinct_pending_dedup_identity(
    monkeypatch, first_source, second_source, equivalent_source,
):
    from custom_components.daylight_calendar_import.dedup import event_fingerprint

    def extract(source):
        return parser.parse_ai_data({"events": [
            structured(title="Session A"), structured(title="Session B"),
        ]}, source_text=source, time_zone="America/Los_Angeles").events

    first, second, equivalent = map(extract, (first_source, second_source, equivalent_source))
    assert len(first) == len(second) == len(equivalent) == 2
    for a, b, c in zip(first, second, equivalent, strict=True):
        assert a.temporal.status == b.temporal.status == c.temporal.status == "confirmation_required"
        assert a.temporal.start_location_value == b.temporal.start_location_value == ""
        assert a.temporal.end_location_value == b.temporal.end_location_value == ""
        assert a.location is None and b.location is None
        assert a.temporal.start_zone == b.temporal.start_zone == "America/Los_Angeles"
        assert len(a.temporal.source_evidence_id) == 64
        assert a.temporal.source_evidence_id != b.temporal.source_evidence_id
        assert a.temporal.source_evidence_id == c.temporal.source_evidence_id
        assert event_fingerprint(a) != event_fingerprint(b)
        assert event_fingerprint(a) == event_fingerprint(c)
        assert EventDraft.from_mapping(a.as_dict()) == a
    backend = _Backend()
    store = _store(monkeypatch, backend)
    await store.async_load()
    one = await store.async_add(source_text=first_source, events=first)
    two = await store.async_add(source_text=second_source, events=second)
    repeat = await store.async_add(source_text=equivalent_source, events=equivalent)
    assert len(one.pending.events) == len(two.pending.events) == 2
    assert one.duplicate_events == two.duplicate_events == 0
    assert repeat.pending is None
    assert repeat.duplicate_events == 2
    reboot = _store(monkeypatch, backend)
    await reboot.async_load()
    assert reboot.get(one.pending.id) == one.pending
    assert reboot.get(two.pending.id) == two.pending
    original = one.pending.events[0]
    zones_to_confirm = dict(end_zone_name="Australia/Sydney") if original.draft.temporal.travel_incomplete else {}
    confirmed = await reboot.async_confirm_timezone(one.pending.id, original.id, "Australia/Sydney",
                                                      expected_event=original, **zones_to_confirm)
    assert confirmed.draft.temporal.status == "resolved"
    assert event_fingerprint(confirmed.draft) == event_fingerprint(replace(confirmed.draft, temporal=None))
    assert confirmed.draft.start == "2027-12-25T18:00:00+11:00"


def test_source_evidence_id_is_optional_and_strictly_validated():
    event, = parse_one().events
    assert event.temporal.source_evidence_id == ""
    old = event.as_dict()
    del old["temporal"]["source_evidence_id"]
    assert EventDraft.from_mapping(old) == event
    invalid = event.temporal.as_dict()
    invalid["source_evidence_id"] = "not-a-digest"
    with pytest.raises(ValueError, match="invalid source evidence identity"):
        zones.TemporalEvidence.from_dict(invalid)


def test_preview_zone_matches_confirmation_for_summer_and_winter_flights():
    for date, start_offset, end_offset, start_utc, end_utc in [
        ("2028-07-10", "-0700", "-0400",
         "2028-07-10T15:00:00+00:00", "2028-07-10T20:30:00+00:00"),
        ("2028-01-10", "-0800", "-0500",
         "2028-01-10T16:00:00+00:00", "2028-01-10T21:30:00+00:00"),
    ]:
        evidence = zones.TemporalEvidence(
            date + "T08:00:00", date + "T16:30:00",
            "America/Los_Angeles", "America/Los_Angeles",
            "ha_fallback", "confirmation_required",
            start_location_type="airport", start_location_value="LAX",
            end_location_type="airport", end_location_value="JFK",
            travel_incomplete=True,
        )
        assert zones.suggested_zone(evidence, "start") == "America/Los_Angeles"
        assert zones.suggested_zone(evidence, "end") == "America/New_York"
        assert zones.preview_zone(evidence, "America/Los_Angeles", "America/New_York") == {
            "start_zone": "America/Los_Angeles",
            "end_zone": "America/New_York",
            "start_local": date + "T08:00:00",
            "end_local": date + "T16:30:00",
            "start_iso": date + "T08:00:00" + start_offset[:3] + ":" + start_offset[3:],
            "end_iso": date + "T16:30:00" + end_offset[:3] + ":" + end_offset[3:],
            "start_utc": start_utc,
            "end_utc": end_utc,
            "start_offset": start_offset,
            "end_offset": end_offset,
            "start_abbreviation": "PDT" if date.endswith("07-10") else "PST",
            "end_abbreviation": "EDT" if date.endswith("07-10") else "EST",
            "duration_seconds": 19800,
        }
        with pytest.raises(ValueError, match="Confirm departure and arrival timezones"):
            zones.preview_zone(evidence, "America/Los_Angeles")
    assert zones.suggested_zone(replace(evidence, end_location_value=""), "end") is None
    assert zones.suggested_zone(replace(evidence, end_location_type="unknown"), "end") is None
    with pytest.raises(ValueError, match="invalid timezone endpoint"):
        zones.suggested_zone(evidence, "other")


def test_preview_zone_rejects_gap_and_requires_both_dst_occurrences():
    fold = zones.TemporalEvidence(
        "2027-11-07T01:15:00", "2027-11-07T01:45:00",
        "America/Los_Angeles", "America/Los_Angeles",
        "ha_fallback", "confirmation_required",
    )
    with pytest.raises(ValueError, match="Choose valid local times"):
        zones.preview_zone(fold, "America/Los_Angeles")
    result = zones.preview_zone(
        fold, "America/Los_Angeles", start_occurrence="first",
        end_occurrence="second",
    )
    assert result["start_iso"] == "2027-11-07T01:15:00-07:00"
    assert result["end_iso"] == "2027-11-07T01:45:00-08:00"
    assert result["duration_seconds"] == 5400
    assert result["start_abbreviation"] == "PDT"
    assert result["end_abbreviation"] == "PST"
    gap = replace(fold, start_local="2027-03-14T02:15:00",
                  end_local="2027-03-14T03:30:00")
    with pytest.raises(ValueError, match="Choose valid local times"):
        zones.preview_zone(gap, "America/Los_Angeles")
    with pytest.raises(ValueError, match="unknown timezone name"):
        zones.preview_zone(fold, "Imaginary/Nowhere")


def test_preview_zone_supports_nonneutral_off_minute_zones():
    evidence = zones.TemporalEvidence(
        "2028-07-10T10:00:00", "2028-07-10T12:00:00",
        "Asia/Kathmandu", "Asia/Kathmandu",
        "ha_fallback", "confirmation_required",
    )
    result = zones.preview_zone(evidence, "Asia/Kathmandu")
    assert (result["start_offset"], result["end_offset"]) == ("+0545", "+0545")
    assert result["duration_seconds"] == 7200
    assert result["start_utc"] == "2028-07-10T04:15:00+00:00"
    assert result["end_utc"] == "2028-07-10T06:15:00+00:00"


def test_endpoint_identity_uses_conservative_shared_normalization():
    evidence = zones.TemporalEvidence(
        "2028-01-10T10:00:00", "2028-01-10T12:00:00",
        "Europe/Berlin", "Europe/Berlin", "ha_fallback", "confirmation_required",
        start_location_type="venue", end_location_type="venue",
        start_location_value="Hauptstrasse 1, Berlin",
        end_location_value="Hauptstraße 1, Berlin",
    )
    assert zones.needs_end_timezone(evidence) is True
    with pytest.raises(ValueError, match="Confirm departure and arrival timezones independently"):
        zones.preview_zone(evidence, "Europe/Berlin")
    result = zones.preview_zone(evidence, "Europe/Berlin", "Europe/Berlin")
    assert result["duration_seconds"] == 7200
    same = replace(evidence, end_location_value="  HAUPTSTRASSE 1, BERLIN\t")
    assert zones.needs_end_timezone(same) is False
    assert zones.preview_zone(same, "Europe/Berlin")["end_zone"] == "Europe/Berlin"
