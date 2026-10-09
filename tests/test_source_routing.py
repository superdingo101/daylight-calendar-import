"""Routing plans separate explicit source control from AI parser evidence."""

from dataclasses import replace

import pytest

from custom_components.daylight_calendar_import.source_routing import plan_source_routing
from custom_components.daylight_calendar_import.sources import SourceKind, TextSourceAdapter


def plan(source, aliases=None, allowed=None, default="calendar.family"):
    return plan_source_routing(
        source,
        default_calendar=default,
        allowed_calendars=allowed if allowed is not None else {
            "calendar.family", "calendar.kids"
        },
        aliases=aliases if aliases is not None else {"kids": "calendar.kids"},
    )


def test_valid_directive_selects_writable_calendar_and_strips_parser_control():
    source = TextSourceAdapter().create("Calendar: KIDS\nSoccer at five")
    result = plan(source)
    assert result.calendar_entity == "calendar.kids"
    assert result.parser_source.text == "Soccer at five"
    assert source.text == "Calendar: KIDS\nSoccer at five"
    assert result.warnings == ()
    assert result.parser_source.id == source.id


def test_email_subject_routes_but_other_source_titles_do_not():
    source = TextSourceAdapter().create("Soccer at five")
    email = replace(source, kind=SourceKind.EMAIL, title="Calendar: kids")
    assert plan(email).calendar_entity == "calendar.kids"
    assert plan(email).parser_source.text == source.text
    image = replace(source, kind=SourceKind.IMAGE, title="Calendar: kids")
    assert plan(image).calendar_entity == "calendar.family"


def test_unknown_and_conflicting_hints_cannot_grant_a_calendar():
    unknown = plan(TextSourceAdapter().create("Calendar: stranger\nPractice"))
    assert unknown.calendar_entity == "calendar.family"
    assert "not configured" in unknown.warnings[0]
    assert "Calendar:" not in unknown.parser_source.text
    conflicts = plan(TextSourceAdapter().create(
        "Calendar: kids\nCalendar: family\nPractice"
    ))
    assert conflicts.calendar_entity == "calendar.family"
    assert "Conflicting" in conflicts.warnings[0]
    assert conflicts.parser_source.text == "Practice"
    assert len(conflicts.warnings) == 1


def test_no_directive_keeps_source_and_default_without_warning():
    source = TextSourceAdapter().create("Bring the cupcakes.")
    result = plan(source)
    assert result.parser_source == source
    assert result.calendar_entity == "calendar.family"
    assert not result.warnings


def test_alias_to_nonwritable_destination_is_refused():
    source = TextSourceAdapter().create("Calendar: kids\nPractice")
    result = plan(source, allowed={"calendar.family"})
    assert result.calendar_entity == "calendar.family"
    assert result.warnings


def test_email_attachment_only_subject_can_route_without_adding_text():
    source = replace(TextSourceAdapter().create("foo"), kind=SourceKind.EMAIL,
                     text=None, title="Calendar: kids")
    result = plan(source)
    assert result.calendar_entity == "calendar.kids"
    assert result.parser_source.text is None


def test_invalid_default_calendar_fails_closed():
    with pytest.raises(ValueError, match="Default calendar"):
        plan(TextSourceAdapter().create("Practice"),
             default="calendar.private")


def test_quoted_history_directive_is_not_routing_control():
    source = TextSourceAdapter().create(
        "Original meeting\n----- Forwarded message\nCalendar: kids"
    )
    result = plan(source)
    assert result.calendar_entity == "calendar.family"
    assert result.parser_source.text == source.text


def test_unresolved_route_is_explicitly_flagged():
    from custom_components.daylight_calendar_import.sources import TextSourceAdapter
    assert plan(TextSourceAdapter().create("Calendar: missing\nPractice")).requires_confirmation
    assert plan(TextSourceAdapter().create("Calendar: kids\nPractice")).requires_confirmation is False


def test_conflicting_calendar_hints_require_review_without_granting_write_access():
    """A conflict must not be downgraded into an ordinary default route."""
    source = TextSourceAdapter().create(
        "Calendar: kids\nCalendar: family\nPractice"
    )
    result = plan(source)
    assert result.calendar_entity == "calendar.family"
    assert result.requires_confirmation is True
    assert result.warnings == (
        "Conflicting calendar routing hints. "
        "Check the destination calendar during review.",
    )
    assert result.parser_source.text == "Practice"
