"""Pure routing extraction tests, independent from HA and parser providers."""

from custom_components.daylight_calendar_import.calendar_routing import (
    MAX_BODY_LINES, extract_calendar_route, normalize_alias,
)


def route(subject=None, body=None, aliases=None, allowed=None):
    return extract_calendar_route(
        subject=subject, body=body, aliases=aliases or {"kids": "calendar.kids"},
        allowed_calendars=allowed if allowed is not None else {"calendar.kids"},
    )


def test_route_normalizes_exact_alias_without_fuzzy_matching():
    assert normalize_alias("  ＫＩＤＳ  ") == "kids"
    result = route(body="Calendar: ＫＩＤＳ\nPractice Thursday")
    assert result.result.status == "resolved"
    assert result.result.calendar_entity == "calendar.kids"
    assert result.body == "Practice Thursday"


def test_no_directive_preserves_evidence():
    result = route(subject="School newsletter", body="A school calendar update\n")
    assert result.result.status == "none"
    assert result.body == "A school calendar update\n"


def test_subject_is_control_and_same_hint_repeated_is_ok():
    result = route(subject="Calendar: Kids", body="Calendar: kids\nA game\n")
    assert result.result.status == "resolved"
    assert result.body == "A game\n"


def test_conflicting_or_unknown_directives_are_not_routed():
    conflict = route(subject="Calendar: kids", body="Calendar: work\nMeeting")
    assert conflict.result.status == "conflicting"
    assert conflict.result.calendar_entity is None
    unknown = route(body="Calendar: kindergarden\nMeeting")
    assert unknown.result.status == "unresolved"
    assert unknown.result.calendar_entity is None


def test_empty_and_oversized_aliases_are_unresolved():
    assert route(body="Calendar: \nMeeting").result.status == "unresolved"
    assert route(body="Calendar: " + "x" * 100 + "\nMeeting").result.status == "unresolved"


def test_directive_never_grants_write_authority():
    result = route(body="Calendar: kids\nMeeting", allowed={"calendar.other"})
    assert result.result.status == "unresolved"


def test_only_bounded_leading_unquoted_body_is_considered():
    text = "Line\n" * MAX_BODY_LINES + "Calendar: kids\n"
    result = route(body=text)
    assert result.result.status == "none"
    assert result.body == text
    quoted = route(body="> Calendar: kids\n----- Forwarded message\nOn Tuesday\n")
    assert quoted.result.status == "none"
    assert quoted.body.startswith("> Calendar")
