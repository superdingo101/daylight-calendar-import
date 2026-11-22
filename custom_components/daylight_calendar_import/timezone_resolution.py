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
_FIXED = re.compile(r"(?i:(UTC|GMT))\s*([+-])(\d{1,2})(?::?(\d{2}))?\Z")
_STATES = {"California": "CA", "Arizona": "AZ", "New York": "NY", "Colorado": "CO"}
_COUNTRIES = {"USA": "US", "United States": "US", "US": "US",
              "UK": "GB", "United Kingdom": "GB", "GB": "GB",
              "Australia": "AU", "AU": "AU", "Canada": "CA", "CA": "CA"}


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

    @classmethod
    def from_dict(cls, raw: dict) -> TemporalEvidence:
        if not isinstance(raw, dict) or set(raw) != {
            "start_local", "end_local", "start_zone", "end_zone",
            "source", "status", "reason",
        }:
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
    code = value.strip().upper()
    if not re.fullmatch(r"[A-Z]{3,4}", code):
        return None
    record = _airports("IATA" if len(code) == 3 else "ICAO").get(code)
    zone = record.get("tz") if isinstance(record, dict) else None
    return zone if isinstance(zone, str) else None


@lru_cache(maxsize=1)
def _cities() -> tuple[dict, ...]:
    import geonamescache
    cache = geonamescache.GeonamesCache(min_city_population=1000)
    return tuple(cache.get_cities().values())


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


def _city_zone(city: str, region: str = "", country: str = "") -> str | None:
    candidates = [
        item for item in _cities()
        if item.get("name", "").casefold() == city.casefold()
        and (not region or item.get("admin1code", "").casefold() == region.casefold())
        and (not country or item.get("countrycode", "").casefold() == country.casefold())
    ]
    # Do not infer which Springfield the author meant. Same-zone duplicates
    # are fine only if their *actual* geographic zones agree.
    zones = {_coordinates_zone(item.get("latitude"), item.get("longitude"))
             for item in candidates}
    zones.discard(None)
    return next(iter(zones)) if len(zones) == 1 and len(zones) == len({
        _coordinates_zone(item.get("latitude"), item.get("longitude")) for item in candidates
    }) else None


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


def _place_zone(kind: str, value: str) -> str | None:
    if kind == "airport":
        return _airport_zone(value)
    if kind == "postal_code":
        return _postal_zone(value)
    if kind not in ("city", "address", "venue"):
        return None
    # A venue name alone is not a geocodable city. Structured address suffix
    # can be resolved locally, but unknown street addresses remain uncertain.
    if kind == "venue":
        return None
    parts = [part.strip() for part in value.split(",")]
    if kind == "address" and len(parts) >= 3:
        parts = parts[-3:]
        parts = parts[1:] if re.search(r"\d", parts[0]) else parts
    if len(parts) < 2:
        return None
    city = parts[0] if len(parts) == 2 else parts[-3]
    region = parts[-2] if len(parts) >= 3 else parts[-1]
    country = parts[-1] if len(parts) >= 3 else "US"
    postal = _POSTAL.search(region)
    if postal:
        region = region.replace(postal.group(), "").strip()
    if len(parts) == 2:
        postal = _POSTAL.search(region)
        if postal:
            region = region.replace(postal.group(), "").strip()
    region = _STATES.get(region, region)
    country = _COUNTRIES.get(country, country if len(country) == 2 else "")
    if len(parts) == 2 and not region:
        return _postal_zone(postal.group(1)) if postal else None
    return _city_zone(city, region, country)


def _range_for_wall(start: datetime, end: datetime, start_name: str,
                    end_name: str) -> tuple[str, str, bool, bool]:
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
    ambiguous = len(starts) > 1 or len(ends) > 1
    if gap:
        # Keep the original wall clocks visible for correction but block writes.
        return (start.replace(tzinfo=_zone(start_name)).isoformat(),
                end.replace(tzinfo=_zone(end_name)).isoformat(), ambiguous, True)
    ranges = [(a, b) for a in starts for b in ends if b.astimezone(timezone.utc) >
              a.astimezone(timezone.utc)]
    if not ranges:
        raise ValueError("end must be after start")
    selected = min(ranges, key=lambda pair: (
        (pair[1].astimezone(timezone.utc) - pair[0].astimezone(timezone.utc)),
        pair[0].astimezone(timezone.utc),
    ))
    return selected[0].isoformat(), selected[1].isoformat(), ambiguous, False


def normalize_structured_event(raw: dict, ha_zone: str) -> tuple[dict, TemporalEvidence]:
    """Turn structured AI source facts into provisional safe review timestamps."""
    start = _local(raw.get("start_local"))
    end = _local(raw.get("end_local"))
    _zone(ha_zone)
    explicit = raw.get("explicit_timezone", "")
    if explicit is None:
        explicit = ""
    if not isinstance(explicit, str) or len(explicit) > 128:
        raise ValueError("invalid explicit timezone evidence")
    explicit = explicit.strip()
    explicit_unknown = False
    if explicit and not _fixed_zone(explicit):
        try:
            _zone(explicit)
        except ValueError:
            # "DST", "CST", and other unqualified abbreviations are source
            # evidence, but not independently resolvable geographic zones.
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
    start_zone, source, unresolved = first
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
        if source == "ha_fallback" and end_source != "ha_fallback":
            source = end_source
    a, b, ambiguous, gap = _range_for_wall(start, end, start_zone, end_zone)
    status = ("correction_required" if gap else
              "confirmation_required" if unresolved or ambiguous else "resolved")
    reason = ("The local time does not exist in this timezone." if gap else
              "Confirm the event timezone or DST occurrence." if status != "resolved" else "")
    metadata = TemporalEvidence(start.isoformat(), end.isoformat(), start_zone,
                                end_zone, source, status, reason)
    return {**raw, "start": a, "end": b}, metadata


def confirm_zone(evidence: TemporalEvidence, zone_name: str) -> tuple[str, str, TemporalEvidence]:
    """Recalculate local times from reviewer-selected named zone without a conversion."""
    _zone(zone_name)
    a, b, ambiguous, gap = _range_for_wall(
        _local(evidence.start_local), _local(evidence.end_local), zone_name, zone_name,
    )
    if ambiguous or gap:
        raise ValueError("Choose valid local times and DST occurrence in the editor")
    return a, b, TemporalEvidence(
        evidence.start_local, evidence.end_local, zone_name, zone_name,
        "user", "resolved",
    )
