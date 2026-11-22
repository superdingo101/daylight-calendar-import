"""Exact geographic lookup contracts; designed to detect timezone regressions.

These tests complement live localis-data integration cases by proving that
untrusted place strings cannot select a popular, partial or uncertain match.
"""
from types import SimpleNamespace
import sys

import pytest

from custom_components.daylight_calendar_import import timezone_resolution as zones


def _localis(monkeypatch, *, countries=None, cities=None):
    """Install a deterministic registry stand-in at the import boundary."""
    country_calls = []
    city_calls = []
    countries = countries or {}
    cities = cities if cities is not None else []

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
    candidates = [SimpleNamespace(lat=float(n), lng=float(n)) for n in records]
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
