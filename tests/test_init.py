"""Tests for Home Assistant service wiring."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import Context, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser

from custom_components.daylight_calendar_import import (
    PENDING_SCHEMA,
    SUBMIT_SCHEMA,
    _async_check_entity_control_permission,
    _async_create_calendar_event,
    _parse_for_entry,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.daylight_calendar_import.const import (
    ATTR_PENDING_ID,
    ATTR_SOURCE_ID,
    ATTR_TEXT,
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITY,
    DOMAIN,
    SERVICE_APPROVE_PENDING,
    SERVICE_IMPORT_TEXT,
    SERVICE_PARSE_TEXT,
    SERVICE_REJECT_PENDING,
    SERVICE_SUBMIT_TEXT,
)
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.storage import (
    PendingImport,
    PendingImportAddResult,
    PendingImportApprovalUncertainError,
)


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
        self.data = {}


def entry():
    return SimpleNamespace(
        entry_id="test-entry",
        data={
            CONF_AI_TASK_ENTITY: "ai_task.test",
            CONF_CALENDAR_ENTITY: "calendar.family",
        },
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


def pending(*events):
    return PendingImport.create(source_text="hello", events=events or (draft(),))


async def test_setup_review_workflow_and_unload(monkeypatch):
    permissions = FakePermissions(allowed=True)
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    parse = AsyncMock(return_value=[draft()])
    submitted = pending(draft())
    approval = pending(draft(), draft(True))
    is_source_duplicate = Mock(return_value=False)

    async def process_pending_events(pending_id, processor):
        assert pending_id == approval.id
        for event in approval.events:
            await processor(event.draft)
        return approval

    pending_store = SimpleNamespace(
        async_load=AsyncMock(),
        is_source_duplicate=is_source_duplicate,
        async_add=AsyncMock(
            return_value=PendingImportAddResult(
                pending=submitted,
                duplicate_source=False,
                duplicate_events=0,
            )
        ),
        async_process_events=AsyncMock(side_effect=process_pending_events),
        async_remove=AsyncMock(return_value=True),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_parse_text", parse)
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: pending_store,
    )

    config_entry = entry()
    assert await async_setup_entry(hass, config_entry) is True
    pending_store.async_load.assert_awaited_once_with()
    assert hass.data[DOMAIN][config_entry.entry_id] is pending_store

    parse_handler, parse_kwargs = hass.services.handlers[(DOMAIN, SERVICE_PARSE_TEXT)]
    assert parse_kwargs["supports_response"] is SupportsResponse.ONLY
    parse_result = await parse_handler(
        SimpleNamespace(data={"text": "hello"}, context=Context(user_id=None))
    )
    assert parse_result["events"][0]["title"] == "Practice"

    user_context = Context(user_id="test-user")
    import_handler, import_kwargs = hass.services.handlers[(DOMAIN, SERVICE_IMPORT_TEXT)]
    assert import_kwargs["supports_response"] is SupportsResponse.OPTIONAL
    import_result = await import_handler(
        SimpleNamespace(data={"text": "hello"}, context=user_context)
    )
    assert import_result["imported"] == 1

    submit_handler, submit_kwargs = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)]
    assert submit_kwargs["supports_response"] is SupportsResponse.ONLY
    assert submit_kwargs["schema"] is SUBMIT_SCHEMA
    submit_result = await submit_handler(
        SimpleNamespace(
            data={"text": "hello", ATTR_SOURCE_ID: "message-1"},
            context=user_context,
        )
    )
    assert submit_result == {
        "pending": submitted.as_service_dict(),
        "duplicate": False,
        "duplicate_source": False,
        "duplicate_events": 0,
    }
    is_source_duplicate.assert_called_once_with("message-1")
    pending_store.async_add.assert_awaited_once_with(
        source_text="hello",
        events=[draft()],
        source_id="message-1",
    )

    approve_handler, approve_kwargs = hass.services.handlers[
        (DOMAIN, SERVICE_APPROVE_PENDING)
    ]
    assert approve_kwargs["supports_response"] is SupportsResponse.OPTIONAL
    approve_result = await approve_handler(
        SimpleNamespace(
            data={ATTR_PENDING_ID: approval.id},
            context=user_context,
        )
    )
    assert approve_result["approved"] is True
    assert approve_result["imported"] == 2
    assert approve_result["pending_id"] == approval.id
    assert len(approve_result["events"]) == 2

    reject_handler, reject_kwargs = hass.services.handlers[
        (DOMAIN, SERVICE_REJECT_PENDING)
    ]
    assert reject_kwargs["supports_response"] is SupportsResponse.OPTIONAL
    reject_result = await reject_handler(
        SimpleNamespace(
            data={ATTR_PENDING_ID: submitted.id},
            context=user_context,
        )
    )
    assert reject_result == {"pending_id": submitted.id, "rejected": True}
    pending_store.async_remove.assert_awaited_once_with(submitted.id)

    assert [call[0:2] for call in hass.services.calls] == [
        ("calendar", "create_event"),
        ("calendar", "create_event"),
        ("calendar", "create_event"),
    ]
    assert all(call[4] is user_context for call in hass.services.calls)
    assert permissions.calls == [
        ("ai_task.test", POLICY_CONTROL),
        ("ai_task.test", POLICY_CONTROL),
        ("calendar.family", POLICY_CONTROL),
        ("calendar.family", POLICY_CONTROL),
    ]

    assert await async_unload_entry(hass, config_entry) is True
    assert hass.services.handlers == {}
    assert hass.data[DOMAIN] == {}


async def test_submit_text_without_events_records_source_handling(monkeypatch):
    hass = FakeHass()
    parse = AsyncMock(return_value=[])
    pending_store = SimpleNamespace(
        async_load=AsyncMock(),
        is_source_duplicate=Mock(return_value=False),
        async_add=AsyncMock(
            return_value=PendingImportAddResult(
                pending=None,
                duplicate_source=False,
                duplicate_events=0,
            )
        ),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_parse_text", parse)
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: pending_store,
    )

    await async_setup_entry(hass, entry())
    submit_handler = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]

    result = await submit_handler(
        SimpleNamespace(
            data={"text": "nothing here"},
            context=Context(user_id=None),
        )
    )

    assert result == {
        "pending": None,
        "duplicate": False,
        "duplicate_source": False,
        "duplicate_events": 0,
    }
    pending_store.is_source_duplicate.assert_not_called()
    pending_store.async_add.assert_awaited_once_with(
        source_text="nothing here",
        events=[],
        source_id=None,
    )


async def test_submit_text_skips_ai_for_known_source(monkeypatch):
    hass = FakeHass()
    parse = AsyncMock()
    pending_store = SimpleNamespace(
        async_load=AsyncMock(),
        is_source_duplicate=Mock(return_value=True),
        async_add=AsyncMock(),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_parse_text", parse)
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: pending_store,
    )

    await async_setup_entry(hass, entry())
    submit_handler = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]
    result = await submit_handler(
        SimpleNamespace(
            data={"text": "duplicate", ATTR_SOURCE_ID: "message-1"},
            context=Context(user_id=None),
        )
    )

    assert result == {
        "pending": None,
        "duplicate": True,
        "duplicate_source": True,
        "duplicate_events": 0,
    }
    parse.assert_not_awaited()
    pending_store.async_add.assert_not_awaited()


async def test_submit_text_authorizes_before_source_lookup(monkeypatch):
    permissions = FakePermissions(allowed=False)
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    parse = AsyncMock()
    is_source_duplicate = Mock()
    pending_store = SimpleNamespace(
        async_load=AsyncMock(),
        is_source_duplicate=is_source_duplicate,
        async_add=AsyncMock(),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_parse_text", parse)
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: pending_store,
    )

    await async_setup_entry(hass, entry())
    submit_handler = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]

    with pytest.raises(Unauthorized):
        await submit_handler(
            SimpleNamespace(
                data={"text": "probe", ATTR_SOURCE_ID: "known-or-guessed"},
                context=Context(user_id="denied-user"),
            )
        )

    assert permissions.calls == [("ai_task.test", POLICY_CONTROL)]
    is_source_duplicate.assert_not_called()
    parse.assert_not_awaited()
    pending_store.async_add.assert_not_awaited()


@pytest.mark.parametrize(
    ("add_result", "expected_duplicate"),
    [
        (
            PendingImportAddResult(
                pending=pending(),
                duplicate_source=False,
                duplicate_events=1,
            ),
            True,
        ),
        (
            PendingImportAddResult(
                pending=None,
                duplicate_source=True,
                duplicate_events=0,
            ),
            True,
        ),
    ],
)
async def test_submit_text_reports_deduplication_races(
    monkeypatch,
    add_result,
    expected_duplicate,
):
    hass = FakeHass()
    parse = AsyncMock(return_value=[draft()])
    pending_store = SimpleNamespace(
        async_load=AsyncMock(),
        is_source_duplicate=Mock(return_value=False),
        async_add=AsyncMock(return_value=add_result),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_parse_text", parse)
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: pending_store,
    )

    await async_setup_entry(hass, entry())
    submit_handler = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]
    result = await submit_handler(
        SimpleNamespace(
            data={"text": "hello", ATTR_SOURCE_ID: "message-race"},
            context=Context(user_id=None),
        )
    )

    assert result["duplicate"] is expected_duplicate
    assert result["duplicate_source"] is add_result.duplicate_source
    assert result["duplicate_events"] == add_result.duplicate_events
    assert result["pending"] == (
        add_result.pending.as_service_dict()
        if add_result.pending is not None
        else None
    )


async def test_approve_and_reject_missing_pending_raise(monkeypatch):
    permissions = FakePermissions(allowed=True)
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    pending_store = SimpleNamespace(
        async_load=AsyncMock(),
        async_process_events=AsyncMock(return_value=None),
        async_remove=AsyncMock(return_value=False),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: pending_store,
    )

    await async_setup_entry(hass, entry())
    context = Context(user_id="reviewer")
    approve_handler = hass.services.handlers[(DOMAIN, SERVICE_APPROVE_PENDING)][0]
    reject_handler = hass.services.handlers[(DOMAIN, SERVICE_REJECT_PENDING)][0]

    with pytest.raises(ServiceValidationError, match="Pending import not found: missing"):
        await approve_handler(
            SimpleNamespace(data={ATTR_PENDING_ID: "missing"}, context=context)
        )

    with pytest.raises(ServiceValidationError, match="Pending import not found: missing"):
        await reject_handler(
            SimpleNamespace(data={ATTR_PENDING_ID: "missing"}, context=context)
        )

    assert permissions.calls == [
        ("calendar.family", POLICY_CONTROL),
        ("calendar.family", POLICY_CONTROL),
    ]
    assert hass.services.calls == []


async def test_approve_uncertain_pending_raises_validation_error(monkeypatch):
    permissions = FakePermissions(allowed=True)
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    pending_store = SimpleNamespace(
        async_load=AsyncMock(),
        async_process_events=AsyncMock(
            side_effect=PendingImportApprovalUncertainError("pending-1")
        ),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: pending_store,
    )

    await async_setup_entry(hass, entry())
    approve_handler = hass.services.handlers[(DOMAIN, SERVICE_APPROVE_PENDING)][0]

    with pytest.raises(
        ServiceValidationError,
        match="unfinished approval attempt",
    ):
        await approve_handler(
            SimpleNamespace(
                data={ATTR_PENDING_ID: "pending-1"},
                context=Context(user_id="reviewer"),
            )
        )

    assert permissions.calls == [("calendar.family", POLICY_CONTROL)]
    assert hass.services.calls == []


def test_service_schemas_validate_source_and_pending_ids():
    validated = SUBMIT_SCHEMA(
        {ATTR_TEXT: "text", ATTR_SOURCE_ID: "  message-1  "}
    )
    assert validated[ATTR_SOURCE_ID] == "message-1"

    with pytest.raises(vol.Invalid):
        SUBMIT_SCHEMA({ATTR_TEXT: "text", ATTR_SOURCE_ID: "   "})
    with pytest.raises(vol.Invalid):
        SUBMIT_SCHEMA({ATTR_TEXT: "text", ATTR_SOURCE_ID: "x" * 2049})
    with pytest.raises(vol.Invalid):
        PENDING_SCHEMA({ATTR_PENDING_ID: ""})


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


async def test_remove_entry_deletes_pending_storage(monkeypatch):
    hass = FakeHass()
    pending_store = SimpleNamespace(async_remove_storage=AsyncMock())
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: pending_store,
    )

    await async_remove_entry(hass, entry())

    pending_store.async_remove_storage.assert_awaited_once_with()
