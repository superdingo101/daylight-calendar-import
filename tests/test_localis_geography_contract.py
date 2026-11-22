"""Exact geographic lookup contracts; designed to detect timezone regressions.

These tests complement live localis-data integration cases by proving that
untrusted place strings cannot select a popular, partial or uncertain match.
"""
from types import SimpleNamespace
import sys

import pytest

from custom_components.daylight_calendar_import import timezone_resolution as zones


def _localis(monkeypatch, *, countries=None, cities=None, subdivisions=None):
    """Install a deterministic registry stand-in at the import boundary."""
    country_calls = []
    city_calls = []
    countries = countries or {}
    cities = cities if cities is not None else []
    subdivisions = subdivisions if subdivisions is not None else []

    def lookup(code):
        country_calls.append(("lookup", code))
        return countries.get(("lookup", code))

    def country_filter(*, name):
        country_calls.append(("filter", name))
        return countries.get(("filter", name), [])

    def city_filter(**fields):
        city_calls.append(fields)
        return cities

    fake = SimpleNamespace(
        countries=SimpleNamespace(lookup=lookup, filter=country_filter),
        cities=SimpleNamespace(filter=city_filter),
        subdivisions=SimpleNamespace(filter=lambda **fields: subdivisions),
    )
    monkeypatch.setitem(sys.modules, "localis", fake)
    return country_calls, city_calls


def test_country_registry_uses_exact_iso_and_aliases_and_rejects_ambiguous_names(monkeypatch):
    country_calls, _ = _localis(monkeypatch, countries={
        ("lookup", "US"): SimpleNamespace(alpha2="US"),
        ("filter", "UK"): [SimpleNamespace(alpha2="GB")],
        ("filter", "Dual Name"): [
            SimpleNamespace(alpha2="AU"), SimpleNamespace(alpha2="NZ"),
        ],
        ("filter", "Alternative"): [
            SimpleNamespace(alpha2="CA"), SimpleNamespace(alpha2="CA"),
        ],
    })
    assert zones._country_code(" US ") == "US"
    assert country_calls == [("lookup", "US")]
    assert zones._country_code("UK") == "GB"
    assert zones._country_code("Dual Name") is None
    assert zones._country_code("Alternative") == "CA"
    assert zones._country_code("Unrecognized") is None
    before = list(country_calls)
    assert zones._country_code(" ") is None
    assert country_calls == before
    assert country_calls[-1] == ("filter", "Unrecognized")


@pytest.mark.parametrize(("records", "points", "expected"), [
    ([1], {1: "America/Los_Angeles"}, "America/Los_Angeles"),
    ([1, 2], {1: "Europe/Paris", 2: "Europe/Paris"}, "Europe/Paris"),
    ([1, 2], {1: "Europe/Paris", 2: "America/Chicago"}, None),
    ([1, 2], {1: "Europe/Paris", 2: None}, None),
    ([], {}, None),
])
def test_exact_city_filter_never_selects_one_ambiguous_candidate(
    monkeypatch, records, points, expected,
):
    candidates = [SimpleNamespace(key=n, lat=float(n), lng=float(n)) for n in records]
    _, calls = _localis(monkeypatch, cities=candidates)
    monkeypatch.setattr(zones, "_coordinates_zone", lambda lat, lng: points.get(int(lat)))
    monkeypatch.setattr(zones, "_country_code", lambda code: "US" if code == "USA" else None)
    assert zones._city_zone("Example", "CA", "USA") == expected
    assert calls == [{"name": "Example", "country": "US", "subdivision": "CA"}]


def test_city_filter_requires_nonempty_city_and_valid_country(monkeypatch):
    _, calls = _localis(monkeypatch, cities=[SimpleNamespace(lat=1, lng=2)])
    monkeypatch.setattr(zones, "_country_code", lambda value: None)
    assert zones._city_zone("   ") is None
    assert zones._city_zone("Paris", country="Atlantis") is None
    assert calls == []


def test_global_city_query_does_not_impose_a_country_or_region(monkeypatch):
    _, calls = _localis(monkeypatch, cities=[SimpleNamespace(lat=12, lng=34)])
    monkeypatch.setattr(zones, "_coordinates_zone", lambda lat, lng: "Europe/Paris")
    assert zones._city_zone("Paris") == "Europe/Paris"
    assert calls == [{"name": "Paris"}]


def test_two_part_country_subdivision_collision_fails_closed(monkeypatch):
    recorded = []
    monkeypatch.setattr(zones, "_country_code",
                        lambda value: "CA" if value == "CA" else None)

    def city_zone(city, region="", country=""):
        recorded.append((city, region, country))
        if country == "CA":
            return "America/Toronto"
        if region == "CA":
            return "America/Los_Angeles"
        return None

    monkeypatch.setattr(zones, "_city_zone", city_zone)
    assert zones._city_parts_zone(["Ontario", "CA"]) is None
    assert recorded == [("Ontario", "", "CA"), ("Ontario", "CA", "")]


def test_two_part_exact_country_or_region_prefers_only_supported_zone(monkeypatch):
    monkeypatch.setattr(zones, "_country_code", lambda value: "CA" if value == "CA" else None)
    monkeypatch.setattr(zones, "_city_zone",
                        lambda city, region="", country="": "America/Los_Angeles"
                        if region == "CA" else None)
    assert zones._city_parts_zone(["Encinitas", "CA"]) == "America/Los_Angeles"
    assert zones._city_parts_zone(["Imaginary", "NO_SUCH_SUBDIVISION"]) is None


def test_three_part_city_requires_known_country_and_exact_region(monkeypatch):
    monkeypatch.setattr(zones, "_country_code",
                        lambda value: "AU" if value == "Australia" else None)
    calls = []
    def city_zone(city, region="", country=""):
        calls.append((city, region, country))
        return "Australia/Sydney"
    monkeypatch.setattr(zones, "_city_zone", city_zone)
    assert zones._city_parts_zone(["Sydney", "NSW", "Australia"]) == "Australia/Sydney"
    assert calls == [("Sydney", "NSW", "Australia")]
    assert zones._city_parts_zone(["Sydney", "NSW", "Atlantis"]) is None
    assert zones._city_parts_zone(["Sydney"]) is None
    assert calls == [("Sydney", "NSW", "Australia")]


def test_zip_evidence_requires_city_and_subdivision_agreement(monkeypatch):
    monkeypatch.setattr(zones, "_postal_zone", lambda zip_code: "America/Los_Angeles")
    seen = []
    def city_zone(city, region="", country=""):
        seen.append((city, region, country))
        return "America/Los_Angeles" if region in ("", "CA") else "America/New_York"
    monkeypatch.setattr(zones, "_city_zone", city_zone)
    assert zones._city_parts_zone(["Encinitas", "92024"]) == "America/Los_Angeles"
    assert zones._city_parts_zone(["Encinitas", "CA 92024"]) == "America/Los_Angeles"
    assert zones._city_parts_zone(["Encinitas", "NY 92024"]) is None
    assert seen == [("Encinitas", "", "US"),
                    ("Encinitas", "CA", "US"),
                    ("Encinitas", "NY", "US")]


def test_venue_uses_only_consistent_printed_geography(monkeypatch):
    def city_zone(parts):
        if parts == ["Community Hall", "Sydney", "NSW"]:
            return "America/Chicago"
        if parts == ["Sydney", "NSW"]:
            return "Australia/Sydney"
        return None
    monkeypatch.setattr(zones, "_city_parts_zone", city_zone)
    assert zones._place_zone("venue", "Community Hall, Sydney, NSW") is None
    assert zones._place_zone("venue", "Community Hall") is None
    assert zones._place_zone("address", "Main Street") is None


def test_unsupported_location_kinds_and_unverified_airports_do_not_guess(monkeypatch):
    monkeypatch.setattr(zones, "_airport_zone", lambda value: None)
    monkeypatch.setattr(zones, "_city_parts_zone", lambda parts: "Europe/London")
    assert zones._place_zone("airport", "INVALID") is None
    assert zones._place_zone("virtual", "London, England") is None
    assert zones._place_zone("address", "Unknown Airport (ZZZ)") is None


@pytest.mark.parametrize("kind", ["venue", "address"])
def test_two_component_venue_or_address_preserves_geographic_safety(kind):
    # A full city/state pair is trustworthy, even when the provider labels it
    # as a venue or an address. A name plus state without a matching city is not.
    assert zones._place_zone(kind, "Encinitas, California") == "America/Los_Angeles"
    assert zones._place_zone(kind, "Fictional Venue, California") is None


def test_subdivision_alias_can_be_resolved_only_through_canonical_city_region(monkeypatch):
    calls = []
    def city_filter(**fields):
        calls.append(fields)
        if fields.get("subdivision") == "DE-BY":
            return [SimpleNamespace(key=2867714, lat=48.13, lng=11.58)]
        return []
    mock = SimpleNamespace(
        cities=SimpleNamespace(filter=city_filter),
        subdivisions=SimpleNamespace(filter=lambda **fields:
            [SimpleNamespace(key="DE-BY")] if fields == {
                "name": "Bavaria", "country": "DE",
            } else []),
    )
    monkeypatch.setitem(sys.modules, "localis", mock)
    monkeypatch.setattr(zones, "_country_code", lambda name: "DE")
    monkeypatch.setattr(zones, "_coordinates_zone", lambda lat, lng: "Europe/Berlin")
    assert zones._city_zone("Munich", "Bavaria", "Germany") == "Europe/Berlin"
    assert calls == [
        {"name": "Munich", "country": "DE", "subdivision": "Bavaria"},
        {"name": "Munich", "country": "DE", "subdivision": "DE-BY"},
    ]


def test_region_alias_disagreement_does_not_approve_a_city_guess(monkeypatch):
    def city_filter(**fields):
        if fields.get("subdivision") == "DE-BY":
            return [SimpleNamespace(key=1, lat=48.1, lng=11.5)]
        if fields.get("subdivision") == "DE-BE":
            return [SimpleNamespace(key=2, lat=52.5, lng=13.4)]
        return []
    mock = SimpleNamespace(
        cities=SimpleNamespace(filter=city_filter),
        subdivisions=SimpleNamespace(filter=lambda **fields: [
            SimpleNamespace(key="DE-BY"), SimpleNamespace(key="DE-BE"),
        ]),
    )
    monkeypatch.setitem(sys.modules, "localis", mock)
    monkeypatch.setattr(zones, "_country_code", lambda name: "DE")
    monkeypatch.setattr(zones, "_coordinates_zone",
                        lambda lat, lng: "Europe/Berlin" if lat < 50
                        else "Europe/Paris")
    assert zones._city_zone("Example", "Regional Alias", "Germany") is None


def test_exact_and_alias_filters_deduplicate_same_city(monkeypatch):
    same = SimpleNamespace(key=2867714, lat=48.13, lng=11.58)
    def city_filter(**fields):
        if fields.get("subdivision") in ("Bavaria", "DE-BY"):
            return [same]
        return []
    mock = SimpleNamespace(
        cities=SimpleNamespace(filter=city_filter),
        subdivisions=SimpleNamespace(filter=lambda **fields: [
            SimpleNamespace(key="DE-BY"),
        ]),
    )
    monkeypatch.setitem(sys.modules, "localis", mock)
    monkeypatch.setattr(zones, "_country_code", lambda name: "DE")
    seen = []
    def coordinate_zone(lat, lng):
        seen.append((lat, lng))
        return "Europe/Berlin"
    monkeypatch.setattr(zones, "_coordinates_zone", coordinate_zone)
    assert zones._city_zone("Munich", "Bavaria", "Germany") == "Europe/Berlin"
    assert seen == [(48.13, 11.58)]


def test_unmatched_country_or_region_never_uses_other_subdivisions(monkeypatch):
    mock = SimpleNamespace(
        cities=SimpleNamespace(filter=lambda **fields: []),
        subdivisions=SimpleNamespace(filter=lambda **fields: []),
    )
    monkeypatch.setitem(sys.modules, "localis", mock)
    monkeypatch.setattr(zones, "_country_code",
                        lambda name: "DE" if name == "Germany" else None)
    assert zones._city_zone("Munich", "Nevada", "Germany") is None
    assert zones._city_zone("Munich", "Bavaria", "Atlantis") is None
