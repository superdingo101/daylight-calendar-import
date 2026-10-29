"""Mutation-focused invariants for guarded hosted-source routing."""

from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import import source_routing as routing
from custom_components.daylight_calendar_import.sources import TextSourceAdapter


def test_resolved_route_without_entity_falls_back_to_writable_default(monkeypatch):
    """Only a resolved, non-null destination can override the default."""
    source = TextSourceAdapter().create("A school event")
    monkeypatch.setattr(
        routing, "extract_calendar_route",
        lambda **kwargs: SimpleNamespace(
            result=SimpleNamespace(status="resolved", calendar_entity=None),
            body=source.text,
            subject_consumed=False,
        ),
    )
    planned = routing.plan_source_routing(
        source,
        default_calendar="calendar.default",
        allowed_calendars={"calendar.default"},
        aliases={},
    )
    assert planned.calendar_entity == "calendar.default"
    assert planned.parser_source.text == source.text
    assert planned.requires_confirmation is False


def test_missing_calendar_has_actionable_review_warning():
    source = TextSourceAdapter().create("School event")
    planned = routing.plan_source_routing(
        source, default_calendar=None, allowed_calendars=set(), aliases={},
    )
    assert planned.calendar_entity is None
    assert planned.requires_confirmation is True
    assert planned.warnings == (
        "Choose a writable calendar in Settings, then confirm each destination during review.",
    )


def test_unwritable_default_has_specific_nonsecret_error():
    source = TextSourceAdapter().create("School event")
    with pytest.raises(ValueError) as raised:
        routing.plan_source_routing(
            source,
            default_calendar="calendar.forbidden",
            allowed_calendars={"calendar.allowed"},
            aliases={},
        )
    assert str(raised.value) == "Default calendar must be a writable calendar"
