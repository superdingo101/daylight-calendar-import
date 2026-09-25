"""Tests for Home Assistant service wiring."""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import Context, SupportsResponse
from homeassistant.exceptions import Unauthorized, UnknownUser

from custom_components.daylight_calendar_import import (
    _async_check_entity_control_permission,
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


class FakePermissions:
    def __init__(self, allowed=True):
        self.allowed = allowed
        self.calls = []

    def check_entity(self, entity_id, permission):
        self.calls.append((entity_id, permission))
        return self.allowed


class FakeAuth:
    def __init__(self, user=None):
        self.user = user
        self.calls = []

    async def async_get_user(self, user_id):
        self.calls.append(user_id)
        return self.user


class FakeHass:
    def __init__(self, user=None):
        self.services = FakeServices()
        self.auth = FakeAuth(user)


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
    parse_context = Context(user_id=None)
    result = await parse_handler(
        SimpleNamespace(data={"text": "hello"}, context=parse_context)
    )
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
    permissions = FakePermissions(allowed=True)
    user = SimpleNamespace(permissions=permissions)
    hass = FakeHass(user=user)
    parse = AsyncMock(return_value=[])
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_parse_text", parse)
    context = Context(user_id="allowed-user")

    assert await _parse_for_entry(hass, entry(), "text", context=context) == []
    assert hass.auth.calls == ["allowed-user"]
    assert permissions.calls == [("ai_task.test", POLICY_CONTROL)]
    assert parse.await_args.kwargs["ai_task_entity"] == "ai_task.test"


async def test_entity_permission_internal_call_skips_auth():
    hass = FakeHass()
    await _async_check_entity_control_permission(hass, "ai_task.test", None)
    await _async_check_entity_control_permission(
        hass, "ai_task.test", Context(user_id=None)
    )
    assert hass.auth.calls == []


async def test_entity_permission_unknown_user():
    hass = FakeHass(user=None)
    context = Context(user_id="missing-user")

    with pytest.raises(UnknownUser):
        await _async_check_entity_control_permission(
            hass, "ai_task.test", context
        )

    assert hass.auth.calls == ["missing-user"]


async def test_entity_permission_denied():
    permissions = FakePermissions(allowed=False)
    user = SimpleNamespace(permissions=permissions)
    hass = FakeHass(user=user)
    context = Context(user_id="denied-user")

    with pytest.raises(Unauthorized):
        await _async_check_entity_control_permission(
            hass, "ai_task.test", context
        )

    assert permissions.calls == [("ai_task.test", POLICY_CONTROL)]


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
