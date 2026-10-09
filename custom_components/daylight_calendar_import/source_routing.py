"""Plan explicit source calendar routing without parser or HA side effects.

The plan is a review submission input, not calendar write authorization.
Unresolved requests retain the configured default plus an explicit warning;
the review step is always required before calendar creation.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from collections.abc import Mapping

from .calendar_routing import extract_calendar_route
from .sources import SourceDocument, SourceKind

_UNKNOWN = (
    "Calendar routing hint is not configured or allowed. "
    "Check the destination calendar during review."
)
_CONFLICTING = (
    "Conflicting calendar routing hints. "
    "Check the destination calendar during review."
)


# Prior versions stored these warning texts before per-event routing flags.
LEGACY_UNRESOLVED_WARNINGS = frozenset((_UNKNOWN, _CONFLICTING))


@dataclass(frozen=True, slots=True)
class SourceRoutingPlan:
    """Parser evidence, bounded destination, and review-only warnings."""

    parser_source: SourceDocument
    calendar_entity: str
    warnings: tuple[str, ...] = ()
    requires_confirmation: bool = False


def plan_source_routing(
    source: SourceDocument,
    *,
    default_calendar: str,
    allowed_calendars: set[str],
    aliases: Mapping[str, str],
) -> SourceRoutingPlan:
    """Route only a declared allowed alias and never infer an unknown target."""
    if default_calendar not in allowed_calendars:
        raise ValueError("Default calendar must be a writable calendar")
    extracted = extract_calendar_route(
        subject=source.title if source.kind is SourceKind.EMAIL else None,
        body=source.text,
        aliases=aliases,
        allowed_calendars=allowed_calendars,
    )
    resolution = extracted.result
    destination = (
        resolution.calendar_entity
        if resolution.status == "resolved" and resolution.calendar_entity is not None
        else default_calendar
    )
    warnings: tuple[str, ...] = ()
    if resolution.status == "unresolved":
        warnings = (_UNKNOWN,)
    elif resolution.status == "conflicting":
        warnings = (_CONFLICTING,)
    parser_source = replace(
        source,
        text=extracted.body if source.text is not None else None,
    )
    return SourceRoutingPlan(
        parser_source, destination, warnings,
        requires_confirmation=resolution.status in ("unresolved", "conflicting"),
    )
