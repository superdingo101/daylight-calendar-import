"""Acceptance checks for native and translated municipality/subdivision names.

Kept early in pytest collection so genuine reference-data failures are found
without waiting for the unrelated Home Assistant full-suite cases.
"""
import pytest
import localis

from custom_components.daylight_calendar_import import timezone_resolution as zones


@pytest.mark.parametrize(("city", "region", "country", "expected"), [
    ("Munich", "Bavaria", "Germany", "Europe/Berlin"),
    ("Montréal", "Québec", "Canada", "America/Toronto"),
])
def test_city_and_subdivision_alias_lookup_is_exact_and_correct(city, region, country, expected):
    result = zones._city_zone(city, region, country)
    if result != expected:
        candidates = localis.cities.filter(name=city, country=country)
        subdivisions = localis.subdivisions.filter(name=region, country=country)
        pytest.fail("LOCALIS_DIAGNOSTIC " + repr({
            "query": (city, region, country),
            "actual": result,
            "city_candidates": [
                (c.name, [(x.name, x.key, x.iso_code, x.geonames_code)
                          for x in c.subdivisions]) for c in candidates
            ],
            "subdivisions": [(x.name, x.key, x.aliases) for x in subdivisions],
            "direct_matches": len(localis.cities.filter(
                name=city, subdivision=region, country=country)),
        }))
    assert zones._city_zone(city, region, country) == expected


def test_unindexed_city_exonym_is_not_automatically_guessed():
    # localis 3.0.0 currently indexes Munich, not München, as the city's
    # name. The Bayer(n) administrative region is valid but cannot establish
    # which municipality an unsupported spelling refers to.
    assert zones._city_zone("München", "Bayern", "Germany") is None
    assert zones._place_zone("city", "München, Bayern, Germany") is None
