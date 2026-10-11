"""Behavioral mutation regressions for permission, calendar, and IMAP boundaries.

Tests verify observable contracts, not implementation-specific mutant IDs.
"""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import Context, SupportsResponse
from homeassistant.auth.permissions.const import CAT_ENTITIES

import custom_components.daylight_calendar_import as integration
from custom_components.daylight_calendar_import import calendar_observation as observation
from custom_components.daylight_calendar_import import direct_imap
from custom_components.daylight_calendar_import.const import (
    DOMAIN, SERVICE_APPROVE_PENDING, SERVICE_GET_PENDING,
    SERVICE_GET_PENDING_EVENT, SERVICE_PARSE_TEXT, SERVICE_SUBMIT_TEXT,
)
from custom_components.daylight_calendar_import.models import EventDraft

from test_init import FakeHass, entry


class CapturedPermissionError(Exception):
    def __init__(self, **details):
        super().__init__(details)
        self.details = details


async def test_unknown_user_permission_error_retains_exact_identity(monkeypatch):
    monkeypatch.setattr(integration, "UnknownUser", CapturedPermissionError)
    hass = FakeHass(user=None)
    context = Context(user_id="actor-missing")
    with pytest.raises(CapturedPermissionError) as failure:
        await integration._async_check_entity_control_permission(
            hass, "calendar.private", context
        )
    assert failure.value.details == {
        "context": context,
        "permission": POLICY_CONTROL,
        "user_id": "actor-missing",
    }
    assert hass.auth.calls == ["actor-missing"]


async def test_denied_permission_error_retains_exact_scope_and_identity(monkeypatch):
    monkeypatch.setattr(integration, "Unauthorized", CapturedPermissionError)
    user = SimpleNamespace(permissions=SimpleNamespace(check_entity=Mock(return_value=False)))
    hass = FakeHass(user=user)
    context = Context(user_id="actor-denied")
    with pytest.raises(CapturedPermissionError) as failure:
        await integration._async_check_entity_control_permission(
            hass, "calendar.private", context
        )
    assert failure.value.details == {
        "context": context,
        "permission": POLICY_CONTROL,
        "user_id": "actor-denied",
        "perm_category": CAT_ENTITIES,
    }
    user.permissions.check_entity.assert_called_once_with("calendar.private", POLICY_CONTROL)


@pytest.mark.parametrize(("start", "end", "all_day", "message"), [
    ("2026-10-08T17:00:00-07:00", "2026-10-08T17:00:00-07:00", False,
     "Invalid event interval"),
    ("2026-10-08T18:00:00-07:00", "2026-10-08T17:00:00-07:00", False,
     "Invalid event interval"),
    ("bad-date", "2026-11-02", True, "Invalid all-day observation interval"),
    ("2026-10-31", "bad-date", True, "Invalid all-day observation interval"),
    ("0001-01-01", "0001-01-02", True, "Invalid all-day observation interval"),
])
def test_approval_probe_rejects_exact_invalid_intervals(start, end, all_day, message):
    zone = (timezone(timedelta(hours=1)) if start.startswith("0001-")
            else ZoneInfo("America/Los_Angeles"))
    draft = EventDraft("Important review", start, end, all_day)
    with pytest.raises(observation.CalendarObservationError) as exc:
        observation._approval_start_window(draft, local_zone=zone)
    assert str(exc.value) == message


def test_approval_probe_preserves_utc_boundary_and_error_context(monkeypatch):
    seen = []
    original = observation._as_utc

    def checked(value, *, message):
        seen.append(message)
        return original(value, message=message)

    monkeypatch.setattr(observation, "_as_utc", checked)
    whole_day = EventDraft("Leave", "2026-10-31", "2026-11-02", True)
    start, end = observation._approval_start_window(
        whole_day, local_zone=ZoneInfo("America/Los_Angeles")
    )
    assert start == datetime(2026, 10, 31, 7, 0, tzinfo=timezone.utc)
    assert end == datetime(2026, 11, 1, 7, 0, tzinfo=timezone.utc)
    assert seen == [
        "Invalid all-day observation interval",
        "Invalid all-day observation interval",
    ]


def test_approval_probe_is_bounded_by_short_real_event():
    draft = EventDraft(
        "Quick meeting", "2026-10-08T17:00:00-07:00",
        "2026-10-08T17:00:20-07:00", False
    )
    start, end = observation._approval_start_window(
        draft, local_zone=ZoneInfo("America/Los_Angeles")
    )
    assert end - start == timedelta(seconds=20)
    assert start == datetime(2026, 10, 9, 0, 0, tzinfo=timezone.utc)


def test_imap_fetch_multiframe_selects_only_the_requested_uid():
    response = SimpleNamespace(result="OK", lines=(
        b"1 FETCH (UID 7 BODY[] {3}", b"one", b")",
        b"2 FETCH (UID 42 BODY[] {3}", b"two", b")",
    ))
    assert direct_imap._extract_fetch_message(response, expected_uid=7)[0] == b"one"
    assert direct_imap._extract_fetch_message(response, expected_uid=42)[0] == b"two"
    assert direct_imap._extract_fetch_message(response, expected_uid=43) is None


def test_imap_fetch_accepts_highest_valid_uid():
    max_uid = direct_imap.MAX_IMAP_IDENTIFIER
    response = SimpleNamespace(result="OK", lines=(
        f"1 FETCH (UID {max_uid} BODY[] {{1}}".encode(), b"X", b")",
    ))
    assert direct_imap._extract_fetch_message(response, expected_uid=max_uid)[0] == b"X"


async def test_registered_service_contract_and_admission_after_setup(monkeypatch):
    from custom_components.daylight_calendar_import.const import (
        SERVICE_GET_PENDING, SERVICE_GET_PENDING_EVENT, SERVICE_PARSE_TEXT,
        SERVICE_SUBMIT_TEXT, SERVICE_APPROVE_PENDING,
    )
    store = SimpleNamespace(async_load=AsyncMock(),
                            list_notifications=Mock(return_value=()))
    timer = Mock(return_value=Mock())
    monkeypatch.setattr(integration, "PendingImportStore", lambda _hass: store)
    monkeypatch.setattr(integration, "async_register_review_panel", AsyncMock())
    monkeypatch.setattr(integration, "async_track_time_interval", timer)
    monkeypatch.setattr(integration, "async_setup_email_runtime", AsyncMock(return_value=None))
    hass = FakeHass()
    config = entry()
    assert await integration.async_setup_entry(hass, config) is True
    assert store.accepting_services is True
    assert hass.data[DOMAIN][config.entry_id] is store
    assert callable(store.on_notifications)
    assert callable(store.on_review_ready)
    assert len(timer.call_args.args) == 3
    assert timer.call_args.args[0] is hass
    assert timer.call_args.args[1] is store.on_notifications
    assert timer.call_args.args[2] == timedelta(seconds=60)
    expected = [
        (SERVICE_PARSE_TEXT, integration.PARSE_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_SUBMIT_TEXT, integration.SUBMIT_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_GET_PENDING, integration.PENDING_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_GET_PENDING_EVENT, integration.PENDING_EVENT_SCHEMA, SupportsResponse.ONLY),
        (SERVICE_APPROVE_PENDING, integration.PENDING_SCHEMA, SupportsResponse.OPTIONAL),
    ]
    for service, schema, response in expected:
        handler, kwargs = hass.services.handlers[(DOMAIN, service)]
        assert callable(handler)
        assert kwargs["schema"] is schema
        assert kwargs["supports_response"] is response
    hass.config_entries.async_forward_entry_setups.assert_awaited_once()
