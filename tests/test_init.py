"""Tests for Home Assistant service wiring."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

from homeassistant.core import Context, SupportsResponse

from custom_components.daylight_calendar_import import (
    _async_create_calendar_event,
    _parse_for_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.daylight_calendar_import.const import (
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITY,
    DOMAIN,
    SERVICE_IMPORT_TEXT,
    SERVICE_PARSE_TEXT,
)
from custom_components.daylight_calendar_import.models import EventDraft


class FakeServices:
    def __init__(self):
        self.handlers = {}
        self.calls = []

    def async_register(self, domain, service, handler, **kwargs):
        self.handlers[(domain, service)] = (handler, kwargs)

    def async_remove(self, domain, service):
        self.handlers.pop((domain, service), None)

    async def async_call(self, domain, service, data, blocking=False, context=None):
        self.calls.append((domain, service, data, blocking, context))


class FakeHass:
    def __init__(self):
        self.services = FakeServices()


def entry():
    return SimpleNamespace(
        data={
            CONF_AI_TASK_ENTITY: "ai_task.test",
            CONF_CALENDAR_ENTITY: "calendar.family",
        }
    )


def draft(all_day=False):
    return EventDraft(
        title="Practice",
        start="2026-10-08" if all_day else "2026-10-08T17:30:00-07:00",
        end="2026-10-09" if all_day else "2026-10-08T18:30:00-07:00",
        all_day=all_day,
        location="Park",
        description=None,
        confidence=0.9,
    )


async def test_setup_parse_and_import_services(monkeypatch):
    hass = FakeHass()
    parse = AsyncMock(return_value=[draft()])
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_parse_text", parse)
    assert await async_setup_entry(hass, entry()) is True

    parse_handler, parse_kwargs = hass.services.handlers[(DOMAIN, SERVICE_PARSE_TEXT)]
    assert parse_kwargs["supports_response"] is SupportsResponse.ONLY
    result = await parse_handler(SimpleNamespace(data={"text": "hello"}))
    assert result["events"][0]["title"] == "Practice"

    import_handler, import_kwargs = hass.services.handlers[(DOMAIN, SERVICE_IMPORT_TEXT)]
    assert import_kwargs["supports_response"] is SupportsResponse.OPTIONAL
    call_context = Context(user_id="test-user")
    result = await import_handler(
        SimpleNamespace(data={"text": "hello"}, context=call_context)
    )
    assert result["imported"] == 1
    assert hass.services.calls[0][0:2] == ("calendar", "create_event")
    assert hass.services.calls[0][4] is call_context

    assert await async_unload_entry(hass, entry()) is True
    assert hass.services.handlers == {}


async def test_parse_for_entry(monkeypatch):
    hass = FakeHass()
    parse = AsyncMock(return_value=[])
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_parse_text", parse)
    assert await _parse_for_entry(hass, entry(), "text") == []
    assert parse.await_args.kwargs["ai_task_entity"] == "ai_task.test"


async def test_calendar_event_payloads():
    hass = FakeHass()
    await _async_create_calendar_event(hass, "calendar.family", draft())
    timed_data = hass.services.calls[-1][2]
    assert timed_data["start_date_time"].endswith("-07:00")
    assert timed_data["description"] == ""

    await _async_create_calendar_event(hass, "calendar.family", draft(True))
    all_day_data = hass.services.calls[-1][2]
    assert all_day_data["start_date"] == "2026-10-08"
    assert "start_date_time" not in all_day_data

    no_location = EventDraft(
        title="No location",
        start="2026-10-08",
        end="2026-10-09",
        all_day=True,
        confidence=1,
    )
    await _async_create_calendar_event(hass, "calendar.family", no_location)
    assert "location" not in hass.services.calls[-1][2]
