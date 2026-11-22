"""Deterministic conversion of extracted local clocks and geographic evidence.

No document text or address is sent to a third-party service. Geographic
data is bundled with the integration's installed Python dependencies.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone, timedelta, tzinfo
from functools import lru_cache
import re
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

KINDS = frozenset({"airport", "address", "city", "postal_code", "venue", "unknown", "virtual"})
_BOUNDED = 256
_NAME = re.compile(r"[A-Za-z][A-Za-z0-9_+\-]*(?:/[A-Za-z0-9_+\-]+)*\Z")
_POSTAL = re.compile(r"\b(\d{5})(?:-\d{4})?\b")
_SOURCE_VENUE = re.compile(
    r"(?im)^[ \t]*(?:location|venue|where)[ \t]*:[ \t]*(?:\r?\n[ \t]*)?([^\r\n]+)"
)
_FIXED = re.compile(r"(?i:(UTC|GMT))\s*([+-])(\d{1,2})(?::?(\d{2}))?\Z")
_TRAVEL = re.compile(
    r"\b(?:flights?|departures?|departing|arrivals?|arriving|boarding|"
    r"itinerar(?:y|ies)|trains?|ferr(?:y|ies)|buses|bus|"
    r"journeys?|travell?ing|travels?|trips?|plane tickets?)\b", re.IGNORECASE,
)
_AIRPORT_ROUTE = re.compile(r"\b[A-Z]{3,4}\s*(?:to|→|[-–])\s*[A-Z]{3,4}\b")

@dataclass(frozen=True, slots=True)
class TemporalEvidence:
    """Original wall times and authoritative review state for one draft."""

    start_local: str
    end_local: str
    start_zone: str
    end_zone: str
    source: str
    status: str
    reason: str = ""
    start_location_type: str = "unknown"
    start_location_value: str = ""
    end_location_type: str = "unknown"
    end_location_value: str = ""
    explicit_timezone: str = ""

    @classmethod
    def from_dict(cls, raw: dict) -> TemporalEvidence:
        required = {"start_local", "end_local", "start_zone", "end_zone",
                    "source", "status", "reason"}
        allowed = required | {"start_location_type", "start_location_value",
                              "end_location_type", "end_location_value", "explicit_timezone"}
        if not isinstance(raw, dict) or not required.issubset(raw) or set(raw) - allowed:
            raise ValueError("invalid temporal review data")
        if any(not isinstance(v, str) or len(v) > _BOUNDED for v in raw.values()):
            raise ValueError("invalid temporal review fields")
        for name in ("start_local", "end_local"):
            _local(raw[name])
        for name in ("start_zone", "end_zone"):
            _zone(raw[name])
        if raw["source"] not in ("explicit", "airport", "geographic", "ha_fallback", "user"):
            raise ValueError("invalid timezone source")
        if raw["status"] not in ("resolved", "confirmation_required", "correction_required"):
            raise ValueError("invalid timezone status")
        for field_name in ("start_location_type", "end_location_type"):
            if raw.get(field_name, "unknown") not in KINDS:
                raise ValueError("invalid persisted location type")
        return cls(**raw)

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return asdict(self)


def _local(value: str) -> datetime:
    if not isinstance(value, str) or len(value) > 32:
        raise ValueError("invalid local date/time")
    try:
        dt = datetime.fromisoformat(value)
    except ValueError as err:
        raise ValueError("invalid local date/time") from err
    if dt.tzinfo is not None or "T" not in value:
        raise ValueError("local date/time must not include a timezone")
    return dt


def _fixed_zone(value: str) -> str | None:
    """Keep explicit UTC offsets distinct from geographic timezone identity."""
    if value in ("UTC", "GMT", "Z"):
        return "UTC"
    match = _FIXED.fullmatch(value)
    if match is None:
        return None
    hour = int(match[3])
    minute = int(match[4] or "0")
    if hour > 23 or minute > 59 or hour * 60 + minute > 1439:
        return None
    return f"UTC{match[2]}{hour:02d}:{minute:02d}"


def _zone(value: str) -> tzinfo:
    fixed = _fixed_zone(value) if isinstance(value, str) else None
    if fixed is not None:
        if fixed == "UTC":
            return timezone.utc
        sign = 1 if fixed[3] == "+" else -1
        return timezone(sign * timedelta(hours=int(fixed[4:6]), minutes=int(fixed[7:9])))
    if not isinstance(value, str) or not value or len(value) > 128 or not _NAME.fullmatch(value):
        raise ValueError("invalid timezone name")
    try:
        return ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as err:
        raise ValueError("unknown timezone name") from err


@lru_cache(maxsize=1)
def _airports(kind: str) -> dict:
    import airportsdata
    return airportsdata.load(kind)


def _airport_zone(value: str) -> str | None:
    """Resolve an IATA/ICAO code, including a full airport name with '(LAX)'."""
    value = value.strip()
    if re.fullmatch(r"[A-Za-z]{3,4}", value):
        codes = [value.upper()]
    else:
        # Do not guess from arbitrary words in a place or a multi-airport
        # itinerary. Parenthesized source codes are explicit identifiers.
        codes = re.findall(r"\(([A-Za-z]{3,4})\)", value)
    if not codes:
        return None
    zones = set()
    for candidate in codes:
        code = candidate.upper()
        record = _airports("IATA" if len(code) == 3 else "ICAO").get(code)
        zone = record.get("tz") if isinstance(record, dict) else None
        if not isinstance(zone, str):
            return None
        zones.add(zone)
    return next(iter(zones)) if len(zones) == 1 else None


def _labeled_flight_airports(source_text: str) -> tuple[str, str] | None:
    """Extract only unique, valid airport codes from a labeled single flight."""
    match = re.search(
        r"(?is)\bdeparture\s*:\s*(.{1,512}?)\barrival\s*:\s*(.{1,512})",
        source_text,
    )
    if match is None:
        return None

    def code_from(segment: str) -> str | None:
        parenthesized = re.findall(r"\(([A-Za-z]{3,4})\)", segment)
        candidates = parenthesized or re.findall(r"\b[A-Z]{3,4}\b", segment)
        valid = {code.upper() for code in candidates if _airport_zone(code)}
        return next(iter(valid)) if len(valid) == 1 else None

    start = code_from(match[1])
    end = code_from(match[2])
    return (start, end) if start and end else None


@lru_cache(maxsize=1)
def _finder():
    from timezonefinder import TimezoneFinder
    return TimezoneFinder(in_memory=False)


def _coordinates_zone(lat: object, lon: object) -> str | None:
    try:
        latitude, longitude = float(lat), float(lon)
    except (ValueError, TypeError):
        return None
    if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
        return None
    return _finder().timezone_at(lat=latitude, lng=longitude)


def _country_code(value: str) -> str | None:
    """Resolve ISO identifiers and exact aliases without assuming a country."""
    import localis

    name = value.strip()
    if not name:
        return None
    match = localis.countries.lookup(name) if len(name) in (2, 3) else None
    if match is not None:
        return match.alpha2
    matches = localis.countries.filter(name=name)
    codes = {item.alpha2 for item in matches}
    return next(iter(codes)) if len(codes) == 1 else None


def _city_zone(city: str, region: str = "", country: str = "") -> str | None:
    """Only exact, unambiguous geographic candidates may supply a timezone.

    localis resolves country and subdivision names, ISO codes and aliases
    worldwide. Never take the most populous or highest-ranked fuzzy result.
    """
    import localis

    if not city.strip():
        return None
    query = {"name": city.strip()}
    if country:
        code = _country_code(country)
        if code is None:
            return None
        query["country"] = code
    if region:
        # The city's subdivision filter indexes canonical names and codes,
        # but a country subdivision's alternate names (e.g. Bavaria for
        # Bayern) may only be indexed by localis.subdivisions. Resolve those
        # exact aliases through the canonical registry, never via fuzziness.
        records = localis.cities.filter(**query, subdivision=region.strip())
        region_query = {"name": region.strip()}
        if country:
            region_query["country"] = query["country"]
        divisions = localis.subdivisions.filter(**region_query)
        for division in divisions:
            records.extend(localis.cities.filter(**query, subdivision=division.key))
        matches = {record.key: record for record in records}.values()
    else:
        matches = localis.cities.filter(**query)
    if not matches:
        return None
    zones = {_coordinates_zone(item.lat, item.lng) for item in matches}
    return next(iter(zones)) if len(zones) == 1 and None not in zones else None


def _postal_zone(value: str) -> str | None:
    match = _POSTAL.fullmatch(value.strip())
    if match is None:
        return None
    # The pure-Python bundled ZIP database is US-only. Never infer a country
    # from a bare foreign-looking numeric code.
    import zipcodes
    entries = zipcodes.matching(match.group(1))
    zones = set()
    for entry in entries:
        zone = _coordinates_zone(entry.get("lat") or entry.get("latitude"),
                                 entry.get("long") or entry.get("longitude"))
        if not zone:
            zone = _city_zone(entry.get("city", ""), entry.get("state", ""), "US")
        if zone:
            zones.add(zone)
    return next(iter(zones)) if len(zones) == 1 else None


def _city_parts_zone(parts: list[str]) -> str | None:
    """Resolve a city with either a country, subdivision, or both."""
    if len(parts) == 2:
        city, qualifier = parts
        postal = _POSTAL.search(qualifier)
        if postal:
            # A US ZIP must agree with the locality, and any printed state,
            # before it can be used as geographic evidence.
            suffix = qualifier.replace(postal.group(), "").strip()
            by_postal = _postal_zone(postal.group(1))
            by_city = _city_zone(city, region=suffix, country="US")
            return by_postal if by_postal is not None and by_postal == by_city else None
        country = _country_code(qualifier)
        zones = set()
        if country is not None:
            country_zone = _city_zone(city, country=country)
            if country_zone is not None:
                zones.add(country_zone)
        region_zone = _city_zone(city, region=qualifier)
        if region_zone is not None:
            zones.add(region_zone)
        return next(iter(zones)) if len(zones) == 1 else None
    if len(parts) == 3:
        city, region, country = parts
        if _country_code(country) is None:
            return None
        return _city_zone(city, region=region, country=country)
    return None


def _place_zone(kind: str, value: str) -> str | None:
    if kind == "airport":
        return _airport_zone(value)
    if kind == "postal_code":
        return _postal_zone(value)
    if kind not in ("city", "address", "venue"):
        return None
    # A printed airport code overrides an unreliable AI venue classification.
    if re.search(r"\([A-Za-z]{3,4}\)", value):
        airport_zone = _airport_zone(value)
        if airport_zone is not None:
            return airport_zone
    parts = [part.strip() for part in value.split(",")]
    if kind == "city":
        return _city_parts_zone(parts)

    # A bare venue/street name is not independently geocodable. Search only
    # context explicitly printed after it, and reject conflicting candidates.
    if len(parts) < 2:
        return None
    options = [parts[-2:]]
    if len(parts) >= 3:
        options.insert(0, parts[-3:])
    if kind == "venue":
        options.insert(0, parts)
    zones = {_city_parts_zone(parts_candidate) for parts_candidate in options}
    zones.discard(None)
    return next(iter(zones)) if len(zones) == 1 else None


def _range_for_wall(start: datetime, end: datetime, start_name: str,
                    end_name: str, start_occurrence: str | None = None,
                    end_occurrence: str | None = None) -> tuple[str, str, bool, bool]:
    """Enumerate legal folds. Return instants and ambiguity/gap markers."""
    def candidates(wall: datetime, name: str) -> list[datetime]:
        zone = _zone(name)
        valid = {}
        for fold in (0, 1):
            local = wall.replace(tzinfo=zone, fold=fold)
            roundtrip = local.astimezone(timezone.utc).astimezone(zone)
            if roundtrip.replace(tzinfo=None) == wall:
                valid[local.utcoffset()] = local
        return list(valid.values())

    starts = candidates(start, start_name)
    ends = candidates(end, end_name)
    gap = not starts or not ends
    ambiguous = ((len(starts) > 1 and start_occurrence is None) or
                 (len(ends) > 1 and end_occurrence is None))
    if gap:
        # Keep the original wall clocks visible for correction but block writes.
        return (start.replace(tzinfo=_zone(start_name)).isoformat(),
                end.replace(tzinfo=_zone(end_name)).isoformat(), ambiguous, True)
    for occurrence, choices in ((start_occurrence, starts), (end_occurrence, ends)):
        if occurrence is not None and (occurrence not in ("first", "second") or len(choices) != 2):
            raise ValueError("Invalid DST occurrence")
    starts.sort(key=lambda item: item.astimezone(timezone.utc))
    ends.sort(key=lambda item: item.astimezone(timezone.utc))
    if start_occurrence is not None:
        starts = [starts[0 if start_occurrence == "first" else 1]]
    if end_occurrence is not None:
        ends = [ends[0 if end_occurrence == "first" else 1]]
    ranges = [(a, b) for a in starts for b in ends if b.astimezone(timezone.utc) >
              a.astimezone(timezone.utc)]
    if not ranges:
        raise ValueError("end must be after start")
    selected = min(ranges, key=lambda pair: (
        (pair[1].astimezone(timezone.utc) - pair[0].astimezone(timezone.utc)),
        pair[0].astimezone(timezone.utc),
    ))
    return selected[0].isoformat(), selected[1].isoformat(), ambiguous, False


def normalize_structured_event(
    raw: dict, ha_zone: str, source_text: str = "",
) -> tuple[dict, TemporalEvidence]:
    """Turn structured AI source facts into provisional safe review timestamps."""
    start = _local(raw.get("start_local"))
    end = _local(raw.get("end_local"))
    _zone(ha_zone)
    raw = dict(raw)
    # A single labeled flight supplies independently trustworthy airport
    # identifiers even if a model collapses the itinerary into one venue.
    # The parser supplies source_text only when exactly one event was extracted.
    labeled = _labeled_flight_airports(source_text) if source_text else None
    if labeled:
        raw.update(start_location_type="airport", start_location_value=labeled[0],
                   end_location_type="airport", end_location_value=labeled[1])
    # An AI provider can fill the event's display location yet omit the
    # separate typed start-location fields. Treat this as actual geographic
    # evidence, NOT as permission to silently use the HA timezone.
    if (raw.get("start_location_type", "unknown") == "unknown"
            and not raw.get("start_location_value")):
        display_place = raw.get("location")
        if isinstance(display_place, str) and display_place.strip():
            place = display_place.strip()
            printed = {match.strip() for match in _SOURCE_VENUE.findall(source_text)
                       if match.strip()}
            if len(printed) == 1 and place.casefold() == next(iter(printed)).casefold():
                # Only a source-verified location can resolve automatically.
                raw.update(start_location_type="venue",
                           start_location_value=place[:_BOUNDED])
            elif "," in place or printed:
                # Unverified or contradictory geography requires confirmation,
                # even if the model claimed this was an HA timezone fallback.
                raw["start_location_value"] = place[:_BOUNDED]
    explicit = raw.get("explicit_timezone", "")
    if explicit is None:
        explicit = ""
    if not isinstance(explicit, str) or len(explicit) > 128:
        raise ValueError("invalid explicit timezone evidence")
    explicit = explicit.strip()
    # Models sometimes copy the configured HA zone into an "explicit" field,
    # even though the invitation never printed it. Never let that hallucinated
    # claim override real geography, especially on a cross-zone flight.
    unsupported_explicit = bool(source_text and explicit and
                                explicit.casefold() not in source_text.casefold())
    if unsupported_explicit:
        explicit = ""
    explicit_unknown = False
    if explicit and not _fixed_zone(explicit):
        try:
            _zone(explicit)
        except ValueError:
            # Human-readable abbreviations/labels are reviewable evidence, but
            # malformed paths and arbitrary identifiers cannot masquerade as
            # ambiguous timezone declarations.
            if not re.fullmatch(r"[A-Za-z][A-Za-z .-]{1,63}", explicit):
                raise ValueError("invalid timezone name") from None
            explicit_unknown = True

    def endpoint(prefix: str, inherited: tuple[str, str] | None = None) -> tuple[str, str, bool]:
        kind = raw.get(prefix + "_location_type", "unknown")
        value = raw.get(prefix + "_location_value", "")
        if not isinstance(kind, str) or kind not in KINDS:
            raise ValueError("invalid location type")
        if value is None:
            value = ""
        if not isinstance(value, str) or len(value) > _BOUNDED:
            raise ValueError("invalid location evidence")
        if kind == "unknown" and not value and inherited is not None:
            return inherited[0], inherited[1], False
        if kind in ("unknown", "virtual") and not value:
            return ha_zone, "ha_fallback", False
        zone = _place_zone(kind, value)
        if zone is not None:
            _zone(zone)
            return zone, "airport" if kind == "airport" else "geographic", False
        return ha_zone, "ha_fallback", True

    first = endpoint("start")
    second = endpoint("end", (first[0], first[1]))

    # Independently supplied display or printed location evidence must not
    # silently disagree with a resolved structured endpoint. This comparison
    # uses only deterministic, unambiguous geographic matches; it does not
    # assume that missing image OCR means there was no location in the image.
    places = [raw.get("location")]
    printed_places = {item.strip() for item in _SOURCE_VENUE.findall(source_text)
                      if item.strip()}
    if len(printed_places) == 1:
        places.extend(printed_places)
    endpoint_zones = {first[0], second[0]}
    location_conflict = False
    for place in places:
        if not isinstance(place, str) or not place.strip():
            continue
        place_zone = (_place_zone("venue", place) or _place_zone("city", place)
                      or _place_zone("airport", place))
        if place_zone is not None and place_zone not in endpoint_zones:
            location_conflict = True

    # Missing destination geography is tolerable for an ordinary one-location
    # event, but not when an itinerary explicitly indicates travel. In that
    # case inheriting the departure zone (or guessing HA for departure) would
    # produce a plausible but possibly incorrect arrival instant.
    travel_context = f"{raw.get('title') or ''} {source_text}"
    travel_incomplete = bool(
        _TRAVEL.search(travel_context)
        or _AIRPORT_ROUTE.search(raw.get("location") or "")
    ) and not (raw.get("start_location_value") and raw.get("end_location_value"))

    # Legacy start/end are optional when structured clocks exist. If supplied,
    # an inconsistent wall clock is a reason to request confirmation, not to
    # replace the authoritative structured time with a provider guess.
    legacy_clock_conflict = False
    for field, wall in (("start", start), ("end", end)):
        legacy = raw.get(field)
        if legacy is None or legacy == "":
            continue
        try:
            if datetime.fromisoformat(legacy).replace(tzinfo=None) != wall:
                legacy_clock_conflict = True
        except (ValueError, TypeError):
            legacy_clock_conflict = True

    start_zone, source, unresolved = first
    unresolved = unresolved or location_conflict or travel_incomplete or legacy_clock_conflict
    end_zone, end_source, end_unresolved = second
    if explicit:
        if not explicit_unknown:
            start_zone = end_zone = _fixed_zone(explicit) or explicit
        source = "explicit"
        # Unknown abbreviations and contradictory geographic signals always
        # need deliberate review. Never invent an offset for an abbreviation.
        unresolved = explicit_unknown or unresolved or end_unresolved or any(
            item[1] not in ("ha_fallback", "explicit") and item[0] != start_zone
            for item in (first, second)
        )
    else:
        unresolved = unresolved or end_unresolved
        if unsupported_explicit and first[1] == "ha_fallback" and second[1] == "ha_fallback":
            unresolved = True
        if source == "ha_fallback" and end_source != "ha_fallback":
            source = end_source
    a, b, ambiguous, gap = _range_for_wall(start, end, start_zone, end_zone)
    status = ("correction_required" if gap else
              "confirmation_required" if unresolved or ambiguous else "resolved")
    reason = ("The local time does not exist in this timezone." if gap else
              "Choose the first or second DST occurrence." if ambiguous else
              "Location evidence conflicts; confirm the event timezone." if location_conflict else
              "Travel itinerary has an unverified endpoint; confirm both timezones." if travel_incomplete else
              "Structured and legacy clocks disagree; confirm the event time." if legacy_clock_conflict else
              "Confirm the source location's timezone." if unresolved else "")
    display_location = raw.get("location")
    if not isinstance(display_location, str) or not display_location.strip():
        start_label = raw.get("start_location_value") or ""
        end_label = raw.get("end_location_value") or ""
        display_location = (start_label + " → " + end_label if
                            start_label and end_label and start_label != end_label
                            else start_label or end_label)
    metadata = TemporalEvidence(
        start.isoformat(), end.isoformat(), start_zone, end_zone, source, status,
        reason,
        raw.get("start_location_type", "unknown"),
        raw.get("start_location_value") or "",
        raw.get("end_location_type", "unknown"),
        raw.get("end_location_value") or "",
        explicit,
    )
    return {**raw, "start": a, "end": b, "location": display_location}, metadata


def confirm_zone(evidence: TemporalEvidence, zone_name: str,
                 end_zone_name: str | None = None,
                 start_occurrence: str | None = None,
                 end_occurrence: str | None = None) -> tuple[str, str, TemporalEvidence]:
    """Recalculate both source-local endpoints with reviewer-confirmed zones."""
    _zone(zone_name)
    end_zone_name = end_zone_name or zone_name
    _zone(end_zone_name)
    a, b, ambiguous, gap = _range_for_wall(
        _local(evidence.start_local), _local(evidence.end_local),
        zone_name, end_zone_name, start_occurrence, end_occurrence,
    )
    if ambiguous or gap:
        raise ValueError("Choose valid local times and DST occurrence in the editor")
    from dataclasses import replace
    return a, b, replace(evidence, start_zone=zone_name, end_zone=end_zone_name,
                         source="user", status="resolved", reason="")
