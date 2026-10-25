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


def test_forwarded_history_cannot_inject_a_route():
    text = "Event info\n----- Forwarded message\nCalendar: kids\n"
    result = route(body=text)
    assert result.result.status == "none"
    assert result.body == text


def test_normal_prose_and_separator_do_not_block_later_directive():
    result = route(body="On Friday we meet at school\nCalendar: kids\n")
    assert result.result.status == "resolved"
    separator = route(body="----- Section -----\nCalendar: kids\n")
    assert separator.result.status == "resolved"


def test_actual_reply_header_blocks_later_directives():
    result = route(body="On Wednesday, Pat <pat@example.test> wrote:\nCalendar: kids")
    assert result.result.status == "none"
    assert result.body.endswith("Calendar: kids")


def test_html_quote_marker_is_not_a_valid_directive():
    result = route(body="Introduction\n>\nCalendar: kids\n")
    assert result.result.status == "none"


def test_blank_transport_body_is_not_invented_as_parser_evidence():
    absent = route(body=None)
    assert absent.body == ""
    assert absent.result.status == "none"


def test_conflicting_and_unknown_route_preserve_auditable_raw_hints():
    conflicted = route(subject="Calendar: kids", body="Calendar: work\nPractice")
    assert conflicted.body == "Practice"
    assert conflicted.result.status == "conflicting"
    assert conflicted.result.raw_hint == "kids"
    assert conflicted.result.calendar_entity is None

    unknown = route(body="Calendar: mystery\nPractice")
    assert unknown.body == "Practice"
    assert unknown.result.status == "unresolved"
    assert unknown.result.raw_hint == "mystery"
    assert unknown.result.calendar_entity is None

    forbidden = route(body="Calendar: kids\nPractice", allowed={"calendar.other"})
    assert forbidden.result.status == "unresolved"
    assert forbidden.result.raw_hint == "kids"
    assert forbidden.body == "Practice"


def test_route_hint_length_64_is_accepted_and_65_refused():
    key = "x" * 64
    accepted = route(body=f"Calendar: {key}\nPractice", aliases={key: "calendar.kids"})
    assert accepted.result.status == "resolved"
    assert accepted.result.raw_hint == key
    assert accepted.result.calendar_entity == "calendar.kids"
    assert accepted.body == "Practice"

    oversized = route(body="Calendar: " + key + "x\nPractice")
    assert oversized.result.status == "unresolved"
    assert oversized.result.raw_hint == key
    assert oversized.body == "Practice"

    empty = route(body="Calendar:   \nPractice")
    assert empty.result.status == "unresolved"
    assert empty.result.raw_hint == ""
    assert empty.body == "Practice"


def test_control_only_directives_preserve_whitespace_and_quoted_history():
    source = "  > Calendar: kids\nCalendar: work\n"
    result = route(body=source)
    assert result.result.status == "none"
    assert result.body == source

    raw = "Calendar: kids\r\nPractice\r\n"
    resolved = route(body=raw)
    assert resolved.result.status == "resolved"
    assert resolved.result.raw_hint == "kids"
    assert resolved.body == "Practice\r\n"


def test_routing_normalization_preserves_single_space_for_multiword_aliases():
    assert normalize_alias("  My    ＫＩＤＳ   ") == "my kids"
