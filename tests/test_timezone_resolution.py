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
    assert event.temporal.reason == "Confirm the source location's timezone."
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


def test_typed_source_location_remains_authoritative_over_untrusted_display():
    raw = structured(
        start_location_type="city", start_location_value="Encinitas, California",
        location="Montréal, Québec, Canada",
    )
    event, = parser.parse_ai_data({"events": [raw]},
        source_text="Location: Montréal, Québec, Canada",
        time_zone="America/Los_Angeles").events
    assert (event.temporal.start_zone, event.temporal.status,
            event.temporal.start_location_type, event.temporal.start_location_value) == (
        "America/Los_Angeles", "resolved", "city", "Encinitas, California",
    )


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


def test_structured_extraction_preserves_source_geo_when_end_only_location():
    result = parse_one(
        start_location_type="unknown", start_location_value=None,
        end_location_type="airport", end_location_value="JFK",
        end_local="2027-12-25T22:00:00",
    )
    event, = result.events
    assert event.temporal.source == "airport"
    assert event.temporal.start_zone == "America/Los_Angeles"
    assert event.temporal.end_zone == "America/New_York"
    assert event.temporal.status == "resolved"
    assert event.location == "JFK"


def test_structured_source_keeps_explicit_event_location_text_and_null_timezone():
    event, = parse_one(location="At home", explicit_timezone=None).events
    assert event.location == "At home"
    assert event.temporal.explicit_timezone == ""
    assert event.temporal.status == "resolved"


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
