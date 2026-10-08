"""Transport-neutral deterministic source calendar directive extraction.

Only an entire subject directive and a bounded, unquoted body prefix are control-plane inputs.
HTML normalizers must preserve quotation boundaries before invoking this resolver.
No alias interpretation is delegated to the AI parser.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from collections.abc import Mapping

_DIRECTIVE = re.compile(r"^\s*calendar\s*:\s*(.*?)\s*$", re.IGNORECASE)
_QUOTE_HEADER = re.compile(
    r"^On\s+.+\s+wrote:\s*$|^-{2,}\s*(?:forwarded|original)\s+message(?:\s*-{2,})?\s*$",
    re.IGNORECASE,
)
MAX_BODY_LINES = 8
MAX_HINT_LENGTH = 64


def normalize_alias(value: str) -> str:
    """Normalize exact-match aliases consistently with settings validation."""
    return " ".join(unicodedata.normalize("NFKC", value).split()).casefold()


@dataclass(frozen=True, slots=True)
class CalendarRouteResolution:
    """A single source-level requested destination, or a review warning."""

    status: str
    calendar_entity: str | None = None
    raw_hint: str | None = None


@dataclass(frozen=True, slots=True)
class RoutingExtraction:
    """Keep a clean parser evidence body and an auditable routing decision."""

    body: str
    result: CalendarRouteResolution


def extract_calendar_route(
    *, subject: str | None, body: str | None,
    aliases: Mapping[str, str], allowed_calendars: set[str],
) -> RoutingExtraction:
    """Resolve only explicit bounded directives; never guess a calendar."""
    hints: list[str] = []
    if subject is not None:
        match = _DIRECTIVE.fullmatch(subject.strip())
        if match:
            hints.append(match.group(1))
    kept: list[str] = []
    quoted_history = False
    for index, line in enumerate((body or "").splitlines(keepends=True)):
        if line.lstrip().startswith(">") or _QUOTE_HEADER.fullmatch(line.strip()):
            quoted_history = True
        if quoted_history or index >= MAX_BODY_LINES:
            kept.append(line)
            continue
        match = _DIRECTIVE.fullmatch(line.rstrip("\r\n"))
        if match:
            hints.append(match.group(1))
        else:
            kept.append(line)
    clean_body = "".join(kept)
    if not hints:
        return RoutingExtraction(clean_body, CalendarRouteResolution("none"))
    normalized = [normalize_alias(value) for value in hints]
    raw_hint = hints[0][:MAX_HINT_LENGTH]
    if len(set(normalized)) > 1:
        return RoutingExtraction(clean_body, CalendarRouteResolution("conflicting", raw_hint=raw_hint))
    alias = normalized[0]
    if not alias or len(alias) > MAX_HINT_LENGTH:
        return RoutingExtraction(clean_body, CalendarRouteResolution("unresolved", raw_hint=raw_hint))
    target = aliases.get(alias)
    if target is None or target not in allowed_calendars:
        return RoutingExtraction(clean_body, CalendarRouteResolution("unresolved", raw_hint=raw_hint))
    return RoutingExtraction(clean_body, CalendarRouteResolution("resolved", target, raw_hint))
