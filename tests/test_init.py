"""Tests for Home Assistant service wiring."""

import asyncio
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock

import pytest
import voluptuous as vol

from homeassistant.auth.permissions.const import POLICY_CONTROL
from homeassistant.core import Context, SupportsResponse
from homeassistant.exceptions import ServiceValidationError, Unauthorized, UnknownUser

from custom_components.daylight_calendar_import import (
    EDIT_EVENT_SCHEMA,
    RESOLVE_EVENT_SCHEMA,
    PENDING_EVENT_SCHEMA,
    PENDING_SCHEMA,
    SUBMIT_SCHEMA,
    SUBMIT_IMAGE_SCHEMA,
    SUBMIT_PDF_SCHEMA,
    _ai_task_configuration,
    _async_check_entity_control_permission,
    _async_create_calendar_event,
    _async_parse_source,
    _calendar_configuration,
    _expected_event,
    _parse_text_with_ai_task,
    async_remove_entry,
    async_setup_entry,
    async_unload_entry,
)
from custom_components.daylight_calendar_import.const import (
    ATTR_EVENT_ID,
    ATTR_FILE_ID,
    ATTR_PENDING_ID,
    ATTR_SOURCE_ID,
    ATTR_TEXT,
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY,
    DOMAIN,
    SERVICE_APPROVE_PENDING,
    SERVICE_APPROVE_PENDING_EVENT,
    SERVICE_EDIT_PENDING_EVENT,
    SERVICE_GET_PENDING,
    SERVICE_GET_PENDING_EVENT,
    SERVICE_IMPORT_TEXT,
    SERVICE_LIST_PENDING,
    SERVICE_LIST_ACTIVITY,
    SERVICE_GET_ACTIVITY,
    SERVICE_PARSE_TEXT,
    SERVICE_REJECT_PENDING,
    SERVICE_REJECT_PENDING_EVENT,
    SERVICE_RESOLVE_PENDING_EVENT,
    SERVICE_SUBMIT_TEXT,
    SERVICE_SUBMIT_IMAGE,
    SERVICE_SUBMIT_PDF,
)
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.parser import ParseOutcome
from custom_components.daylight_calendar_import.providers import SourceValidationError
from custom_components.daylight_calendar_import.sources import SourceAttachment, SourceDocument, SourceKind, TextSourceAdapter
from custom_components.daylight_calendar_import.storage import (
    PendingEvent,
    PendingEventEditError,
    PendingEventResolutionError,
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
        return self.allowed.get(entity_id, False) if isinstance(self.allowed, dict) else self.allowed


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
        self.bus = SimpleNamespace(async_fire=Mock())
        self.config_entries = SimpleNamespace(
            async_forward_entry_setups=AsyncMock(),
            async_unload_platforms=AsyncMock(return_value=True),
        )


@pytest.fixture(autouse=True)
def fake_review_panel_for_service_unit_tests(monkeypatch):
    """Service-only fakes exercise the panel separately from registration."""
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_register_review_panel", AsyncMock())
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_remove_review_panel", Mock())


async def test_panel_registration_failure_leaves_no_services_or_store(monkeypatch):
    failure = AsyncMock(side_effect=RuntimeError("panel unavailable"))
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_register_review_panel", failure)
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore",
                        lambda _hass: SimpleNamespace(async_load=AsyncMock()))
    hass = FakeHass()
    with pytest.raises(RuntimeError, match="panel unavailable"):
        await async_setup_entry(hass, entry())
    assert hass.services.handlers == {}
    assert DOMAIN not in hass.data


def entry(options=None):
    return SimpleNamespace(
        entry_id="test-entry",
        data={
            CONF_AI_TASK_ENTITY: "ai_task.test",
            CONF_CALENDAR_ENTITY: "calendar.family",
        },
        options=options or {},
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
    parse = AsyncMock(return_value=ParseOutcome([draft()], []))
    submitted = pending(draft())
    approval = pending(draft(), draft(True))
    is_source_duplicate = Mock(return_value=False)

    async def process_pending_events(pending_id, processor):
        assert pending_id == approval.id
        for event in approval.events:
            await processor(event)
        return approval

    pending_store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        is_source_duplicate=is_source_duplicate,
        async_add=AsyncMock(
            return_value=PendingImportAddResult(
                pending=submitted,
                duplicate_source=False,
                duplicate_events=0,
            )
        ),
        async_process_events=AsyncMock(side_effect=process_pending_events),
        get=Mock(return_value=approval),
        async_remove=AsyncMock(return_value=True),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
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
    assert import_result["events"] == [draft().as_dict()]

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
        "warnings": [],
    }
    is_source_duplicate.assert_called_once_with("message-1")
    pending_store.async_add.assert_awaited_once_with(
        source_text="hello",
        events=[draft()],
        source_id="message-1",
        calendar_entity="calendar.family",
        warnings=[],
        routing_unresolved=False,
        activity_id="activity-id",
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
        ("calendar.family", POLICY_CONTROL),
        ("calendar.family", POLICY_CONTROL),
    ]

    assert await async_unload_entry(hass, config_entry) is True
    assert hass.services.handlers == {}
    assert hass.data[DOMAIN] == {}


async def test_pending_read_actions_return_summaries_details_and_stable_event(monkeypatch):
    permissions = FakePermissions()
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    item = PendingImport.create(
        source_text="private invitation", events=[draft(), draft(True)],
        source_fingerprint="stored-source-hash",
    )
    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), list=Mock(return_value=(item,)),
        get=Mock(return_value=item),
        get_event=Mock(return_value=item.events[1]),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    await async_setup_entry(hass, entry())
    context = Context(user_id="reviewer")

    list_handler, list_options = hass.services.handlers[(DOMAIN, SERVICE_LIST_PENDING)]
    assert list_options["supports_response"] is SupportsResponse.ONLY
    summary = await list_handler(SimpleNamespace(data={}, context=context))
    assert summary == {"imports": [{
        "id": item.id, "created_at": item.created_at, "event_count": 2,
        "title": "Practice", "approval_in_flight": False,
        "source_kind": "manual_text", "source_title": None,
        "warnings": [], "duplicate_events": 0,
    }]}
    assert "private invitation" not in str(summary)

    get_handler, get_options = hass.services.handlers[(DOMAIN, SERVICE_GET_PENDING)]
    assert get_options["schema"] is PENDING_SCHEMA
    assert get_options["supports_response"] is SupportsResponse.ONLY
    details = await get_handler(SimpleNamespace(data={ATTR_PENDING_ID: item.id}, context=context))
    assert details["pending"]["source_text"] == "private invitation"
    assert details["pending"]["source_kind"] == "manual_text"
    assert details["pending"]["warnings"] == []
    assert details["pending"]["events"][1]["id"] == item.events[1].id
    assert details["pending"]["allowed_calendars"] == ["calendar.family"]
    assert details["pending"]["default_calendar"] == "calendar.family"
    assert "source_fingerprint" not in details["pending"]

    event_handler, event_options = hass.services.handlers[(DOMAIN, SERVICE_GET_PENDING_EVENT)]
    assert event_options["schema"] is PENDING_EVENT_SCHEMA
    assert event_options["supports_response"] is SupportsResponse.ONLY
    event_result = await event_handler(SimpleNamespace(
        data={ATTR_PENDING_ID: item.id, ATTR_EVENT_ID: item.events[1].id}, context=context,
    ))
    assert event_result == {"pending_id": item.id, "event": item.events[1].as_service_dict()}
    store.get_event.assert_called_once_with(item.id, item.events[1].id)
    assert permissions.calls == [
        (entity, POLICY_CONTROL)
        for _ in range(3)
        for entity in ("ai_task.test", "calendar.family")
    ]
    await async_unload_entry(hass, entry())
    assert hass.services.handlers == {}


async def test_activity_reads_are_scoped_and_omit_transition_detail_from_list(monkeypatch):
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions()))
    item = pending()
    record = {"id": item.id, "status": "calendar_created",
              "transitions": [{"type": "review_ready"}, {"type": "calendar_created"}]}
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), list_activity=Mock(return_value=(record,)),
                            get_activity=Mock(side_effect=lambda key: record if key == item.id else None))
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    await async_setup_entry(hass, entry())
    context = Context(user_id="reviewer")
    list_handler, options = hass.services.handlers[(DOMAIN, SERVICE_LIST_ACTIVITY)]
    assert options["supports_response"] is SupportsResponse.ONLY
    assert await list_handler(SimpleNamespace(data={}, context=context)) == {
        "activity": [{"id": item.id, "status": "calendar_created"}],
    }
    detail, options = hass.services.handlers[(DOMAIN, SERVICE_GET_ACTIVITY)]
    assert options["schema"] is PENDING_SCHEMA
    assert await detail(SimpleNamespace(data={ATTR_PENDING_ID: item.id}, context=context)) == {
        "activity": record,
    }
    with pytest.raises(ServiceValidationError, match="Activity not found"):
        await detail(SimpleNamespace(data={ATTR_PENDING_ID: "absent"}, context=context))
    with pytest.raises(Unauthorized):
        await list_handler(SimpleNamespace(data={}, context=None))
    store.list_activity.assert_called_once_with()


@pytest.mark.parametrize("missing", [SERVICE_GET_PENDING, SERVICE_GET_PENDING_EVENT])
async def test_pending_read_actions_report_missing_id(monkeypatch, missing):
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions()))
    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), get=Mock(return_value=None),
        get_event=Mock(return_value=None),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    await async_setup_entry(hass, entry())
    handler = hass.services.handlers[(DOMAIN, missing)][0]
    with pytest.raises(ServiceValidationError, match="not found"):
        await handler(SimpleNamespace(
            data={ATTR_PENDING_ID: "missing", ATTR_EVENT_ID: "missing"},
            context=Context(user_id="reviewer"),
        ))


@pytest.mark.parametrize("context", [None, Context(user_id=None)])
async def test_pending_reads_reject_anonymous_calls_before_store_access(monkeypatch, context):
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), list=Mock())
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    hass = FakeHass()
    await async_setup_entry(hass, entry())
    handler = hass.services.handlers[(DOMAIN, SERVICE_LIST_PENDING)][0]
    with pytest.raises(Unauthorized):
        await handler(SimpleNamespace(data={}, context=context))
    store.list.assert_not_called()


@pytest.mark.parametrize("allowed", [
    {"ai_task.test": False, "calendar.family": True},
    {"ai_task.test": True, "calendar.family": False},
])
async def test_pending_reads_require_control_of_both_entities(monkeypatch, allowed):
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), list=Mock())
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions(allowed)))
    await async_setup_entry(hass, entry())
    handler = hass.services.handlers[(DOMAIN, SERVICE_LIST_PENDING)][0]
    with pytest.raises(Unauthorized):
        await handler(SimpleNamespace(data={}, context=Context(user_id="reviewer")))
    store.list.assert_not_called()


async def test_pending_reads_reject_unknown_user(monkeypatch):
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), list=Mock())
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    hass = FakeHass()
    await async_setup_entry(hass, entry())
    handler = hass.services.handlers[(DOMAIN, SERVICE_LIST_PENDING)][0]
    with pytest.raises(UnknownUser):
        await handler(SimpleNamespace(data={}, context=Context(user_id="missing")))
    store.list.assert_not_called()


async def test_edit_pending_event_validates_and_checks_permissions(monkeypatch):
    item = pending()
    original = item.events[0]
    replacement = draft(True)
    edited = type(original)(original.id, replacement)
    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), async_edit_event=AsyncMock(return_value=edited),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions()))
    await async_setup_entry(hass, entry())
    handler, options = hass.services.handlers[(DOMAIN, SERVICE_EDIT_PENDING_EVENT)]
    assert options["schema"] is EDIT_EVENT_SCHEMA
    assert options["supports_response"] is SupportsResponse.ONLY
    call = SimpleNamespace(
        data={ATTR_PENDING_ID: item.id, ATTR_EVENT_ID: original.id, "event": replacement.as_dict()},
        context=Context(user_id="editor"),
    )
    assert await handler(call) == {"pending_id": item.id, "event": edited.as_service_dict()}
    store.async_edit_event.assert_awaited_once_with(
        item.id, original.id, replacement, calendar_entity=None, expected_event=None
    )

    store.async_edit_event.reset_mock()
    call.data["expected_event"] = original.as_service_dict()
    await handler(call)
    store.async_edit_event.assert_awaited_once_with(
        item.id, original.id, replacement, calendar_entity=None, expected_event=original
    )
    call.data["expected_event"]["id"] = "wrong"
    with pytest.raises(ServiceValidationError, match="changed since"):
        await handler(call)
    call.data["expected_event"] = {
        **original.as_service_dict(), "status": "write_uncertain"
    }
    store.async_edit_event.reset_mock()
    await handler(call)
    assert store.async_edit_event.await_args.kwargs["expected_event"].status == "write_uncertain"
    call.data.pop("expected_event")
    store.async_edit_event.reset_mock()

    call.data["event"] = {**replacement.as_dict(), "end": "invalid"}
    with pytest.raises(ServiceValidationError, match="valid ISO"):
        await handler(call)
    store.async_edit_event.assert_not_awaited()

    call.data["event"] = replacement.as_dict()
    store.async_edit_event.reset_mock()
    store.async_edit_event.return_value = None
    with pytest.raises(ServiceValidationError, match="not found"):
        await handler(call)
    store.async_edit_event.side_effect = PendingEventEditError("duplicates another event")
    with pytest.raises(ServiceValidationError, match="duplicates"):
        await handler(call)

    store.async_edit_event.reset_mock(side_effect=True)
    call.context = Context(user_id=None)
    with pytest.raises(Unauthorized):
        await handler(call)
    store.async_edit_event.assert_not_awaited()


async def test_reject_pending_event_action_checks_permissions_and_uncertainty(monkeypatch):
    item = pending()
    event_id = item.events[0].id
    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), async_reject_event=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions()))
    await async_setup_entry(hass, entry())
    handler, options = hass.services.handlers[(DOMAIN, SERVICE_REJECT_PENDING_EVENT)]
    assert options["schema"] is PENDING_EVENT_SCHEMA
    assert options["supports_response"] is SupportsResponse.OPTIONAL
    call = SimpleNamespace(
        data={ATTR_PENDING_ID: item.id, ATTR_EVENT_ID: event_id},
        context=Context(user_id="reviewer"),
    )
    assert await handler(call) == {
        "pending_id": item.id, "event_id": event_id, "rejected": True,
    }
    store.async_reject_event.assert_awaited_once_with(item.id, event_id)

    store.async_reject_event.return_value = False
    with pytest.raises(ServiceValidationError, match="not found"):
        await handler(call)
    store.async_reject_event.side_effect = PendingImportApprovalUncertainError(item.id)
    with pytest.raises(ServiceValidationError, match="uncertain calendar write"):
        await handler(call)

    store.async_reject_event.reset_mock(side_effect=True)
    call.context = Context(user_id=None)
    with pytest.raises(Unauthorized):
        await handler(call)
    store.async_reject_event.assert_not_awaited()


async def test_approve_pending_event_action_writes_only_selected_event(monkeypatch):
    item = pending(draft(), draft(True))
    selected = item.events[1]

    async def approve(pending_id, event_id, processor):
        assert (pending_id, event_id) == (item.id, selected.id)
        await processor(selected)
        return selected

    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), async_approve_event=AsyncMock(side_effect=approve), get_event=Mock(return_value=selected),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions()))
    await async_setup_entry(hass, entry())
    handler, options = hass.services.handlers[(DOMAIN, SERVICE_APPROVE_PENDING_EVENT)]
    assert options["schema"] is PENDING_EVENT_SCHEMA
    assert options["supports_response"] is SupportsResponse.OPTIONAL
    call = SimpleNamespace(
        data={ATTR_PENDING_ID: item.id, ATTR_EVENT_ID: selected.id},
        context=Context(user_id="reviewer"),
    )
    assert await handler(call) == {
        "pending_id": item.id, "event_id": selected.id, "approved": True,
        "event": {**selected.as_service_dict(), "status": "approved"},
    }
    assert len(hass.services.calls) == 1
    assert hass.services.calls[0][0:2] == ("calendar", "create_event")
    assert hass.services.calls[0][2]["start_date"] == selected.draft.start

    store.async_approve_event.side_effect = None
    store.async_approve_event.return_value = None
    store.get_event.return_value = None
    with pytest.raises(ServiceValidationError, match="not found"):
        await handler(call)
    store.async_approve_event.side_effect = PendingImportApprovalUncertainError(item.id)
    with pytest.raises(ServiceValidationError) as error:
        await handler(call)
    assert str(error.value) == (
        "This event has an uncertain calendar write; verify it before retrying"
    )

    store.async_approve_event.reset_mock(side_effect=True)
    call.context = Context(user_id=None)
    with pytest.raises(Unauthorized):
        await handler(call)
    store.async_approve_event.assert_not_awaited()


async def test_event_decisions_validate_and_forward_optional_snapshot(monkeypatch):
    item = pending()
    selected = item.events[0]
    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        async_reject_event=AsyncMock(return_value=True),
        async_approve_event=AsyncMock(return_value=selected),
        get_event=Mock(return_value=selected),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store,
    )
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions()))
    await async_setup_entry(hass, entry())
    data = {ATTR_PENDING_ID: item.id, ATTR_EVENT_ID: selected.id,
            "expected_event": selected.as_service_dict()}
    call = SimpleNamespace(data=data, context=Context(user_id="reviewer"))
    reject = hass.services.handlers[(DOMAIN, SERVICE_REJECT_PENDING_EVENT)][0]
    approve = hass.services.handlers[(DOMAIN, SERVICE_APPROVE_PENDING_EVENT)][0]
    assert (await reject(call))["rejected"] is True
    store.async_reject_event.assert_awaited_once_with(
        item.id, selected.id, expected_event=selected,
    )
    assert (await approve(call))["approved"] is True
    assert store.async_approve_event.await_args.args[:2] == (item.id, selected.id)
    assert callable(store.async_approve_event.await_args.args[2])
    assert store.async_approve_event.await_args.kwargs == {"expected_event": selected}

    routed = {**selected.as_service_dict(), CONF_CALENDAR_ENTITY: "calendar.work"}
    decoded = _expected_event(routed, selected.id)
    assert decoded is not None and decoded.calendar_entity == "calendar.work"
    assert _expected_event(None, selected.id) is None

    for invalid in ({**selected.as_service_dict(), "id": "other"},
                    {**selected.as_service_dict(), "status": "write_uncertain"},
                    {**selected.as_service_dict(), "start": "invalid"}):
        call.data["expected_event"] = invalid
        for handler in (reject, approve):
            with pytest.raises(ServiceValidationError):
                await handler(call)
    with pytest.raises(PendingEventEditError) as mismatch:
        _expected_event({**selected.as_service_dict(), "id": "other"}, selected.id)
    assert str(mismatch.value) == "Event changed since it was loaded; refresh before deciding"
    call.data["expected_event"] = selected.as_service_dict()
    for handler, method in ((reject, store.async_reject_event),
                            (approve, store.async_approve_event)):
        method.side_effect = PendingEventEditError("Event changed since it was loaded")
        with pytest.raises(ServiceValidationError, match="Event changed"):
            await handler(call)
        method.side_effect = None


async def test_resolve_uncertain_action_enforces_permissions_and_state(monkeypatch):
    item = pending()
    event_id = item.events[0].id
    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), async_resolve_uncertain=AsyncMock(return_value=True),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions()))
    await async_setup_entry(hass, entry())
    handler, options = hass.services.handlers[(DOMAIN, SERVICE_RESOLVE_PENDING_EVENT)]
    assert options["schema"] is RESOLVE_EVENT_SCHEMA
    assert options["supports_response"] is SupportsResponse.OPTIONAL
    call = SimpleNamespace(
        data={ATTR_PENDING_ID: item.id, ATTR_EVENT_ID: event_id, "resolution": "created"},
        context=Context(user_id="reviewer"),
    )
    assert await handler(call) == {
        "pending_id": item.id, "event_id": event_id, "resolution": "created",
    }
    store.async_resolve_uncertain.assert_awaited_once_with(item.id, event_id, "created")
    uncertain = PendingEvent(event_id, item.events[0].draft, "write_uncertain",
                             item.events[0].calendar_entity, "attempt-a")
    call.data["expected_event"] = uncertain.as_service_dict()
    assert await handler(call) == {"pending_id": item.id, "event_id": event_id,
                                   "resolution": "created"}
    store.async_resolve_uncertain.assert_awaited_with(
        item.id, event_id, "created", expected_event=uncertain,
    )
    for snapshot in ({**uncertain.as_service_dict(), "id": "other"},
                     {**uncertain.as_service_dict(), "status": "pending"}):
        call.data["expected_event"] = snapshot
        with pytest.raises(ServiceValidationError, match="Event changed"):
            await handler(call)
    call.data["expected_event"] = {**uncertain.as_service_dict(), "title": ""}
    with pytest.raises(ServiceValidationError):
        await handler(call)
    call.data.pop("expected_event")
    with pytest.raises(vol.Invalid):
        RESOLVE_EVENT_SCHEMA({**call.data, "resolution": "retry"})

    store.async_resolve_uncertain.return_value = False
    with pytest.raises(ServiceValidationError, match="not found"):
        await handler(call)
    store.async_resolve_uncertain.side_effect = PendingEventResolutionError("not uncertain")
    with pytest.raises(ServiceValidationError, match="not uncertain"):
        await handler(call)

    store.async_resolve_uncertain.reset_mock(side_effect=True)
    call.context = Context(user_id=None)
    with pytest.raises(Unauthorized):
        await handler(call)
    store.async_resolve_uncertain.assert_not_awaited()


async def test_reject_all_requires_resolution_of_uncertain_write(monkeypatch):
    item = pending()
    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        async_remove=AsyncMock(side_effect=PendingImportApprovalUncertainError(item.id)),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions()))
    await async_setup_entry(hass, entry())
    handler = hass.services.handlers[(DOMAIN, SERVICE_REJECT_PENDING)][0]
    with pytest.raises(ServiceValidationError) as error:
        await handler(SimpleNamespace(
            data={ATTR_PENDING_ID: item.id}, context=Context(user_id="reviewer"),
        ))
    assert str(error.value) == (
        "Pending import has an uncertain calendar write; resolve it before rejecting"
    )


async def test_submit_text_without_events_records_source_handling(monkeypatch):
    hass = FakeHass()
    parse = AsyncMock(return_value=ParseOutcome([], []))
    pending_store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        is_source_duplicate=Mock(return_value=False),
        async_add=AsyncMock(
            return_value=PendingImportAddResult(
                pending=None,
                duplicate_source=False,
                duplicate_events=0,
            )
        ),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
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
        "warnings": [],
    }
    pending_store.is_source_duplicate.assert_not_called()
    pending_store.async_add.assert_awaited_once_with(
        source_text="nothing here",
        events=[],
        source_id=None,
        calendar_entity="calendar.family",
        warnings=[],
        routing_unresolved=False,
        activity_id="activity-id",
    )


async def test_submit_text_parse_failure_records_activity_and_reraises(monkeypatch):
    hass = FakeHass()
    parse = AsyncMock(side_effect=ServiceValidationError("parser unavailable"))
    pending_store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
                                    async_record_parse_failure=AsyncMock(return_value="failed-id"))
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore",
                        lambda _hass: pending_store)
    await async_setup_entry(hass, entry())
    submit = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]
    with pytest.raises(ServiceValidationError, match="parser unavailable"):
        await submit(SimpleNamespace(data={ATTR_TEXT: "private details"}, context=Context(user_id=None)))
    pending_store.async_record_parse_failure.assert_awaited_once_with("activity-id")
    assert pending_store.async_begin_submission.await_args.kwargs["source_kind"] == "manual_text"
    pending_store.async_record_parse_failure.side_effect = RuntimeError("disk full")
    with pytest.raises(ServiceValidationError, match="parser unavailable"):
        await submit(SimpleNamespace(data={ATTR_TEXT: "private details"}, context=Context(user_id=None)))


async def test_canceled_submission_finalizes_activity(monkeypatch):
    hass = FakeHass()
    started = asyncio.Event()

    async def parse(_hass, _entry, _source):
        started.set()
        await asyncio.Future()

    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
                            async_record_parse_failure=AsyncMock(), is_source_duplicate=Mock(return_value=False))
    monkeypatch.setattr("custom_components.daylight_calendar_import._async_parse_source", parse)
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store)
    await async_setup_entry(hass, entry())
    submit = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]
    task = asyncio.create_task(submit(SimpleNamespace(data={ATTR_TEXT: "private details"}, context=Context(user_id=None))))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    store.async_record_parse_failure.assert_awaited_once_with("activity-id")
    assert store.active_submissions == set()


async def test_entry_unload_waits_for_live_submission(monkeypatch):
    hass = FakeHass()
    started = asyncio.Event()
    release = asyncio.Event()

    async def parse(_hass, _entry, _source):
        started.set()
        await release.wait()
        return ParseOutcome([draft()], [])

    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
                            async_add=AsyncMock(return_value=PendingImportAddResult(pending=pending(),
                                duplicate_source=False, duplicate_events=0)),
                            is_source_duplicate=Mock(return_value=False))
    monkeypatch.setattr("custom_components.daylight_calendar_import._async_parse_source", parse)
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store)
    config_entry = entry()
    await async_setup_entry(hass, config_entry)
    submit = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]
    submitted = asyncio.create_task(submit(SimpleNamespace(data={ATTR_TEXT: "source"}, context=Context(user_id=None))))
    await started.wait()
    unloading = asyncio.create_task(async_unload_entry(hass, config_entry))
    await asyncio.sleep(0)
    assert not unloading.done()
    release.set()
    assert (await submitted)["pending"] is not None
    assert await unloading is True
    assert store.active_submissions == set()


async def test_submit_attachment_parse_failure_records_activity(monkeypatch):
    hass = FakeHass()
    source = SourceDocument(id="submission", kind=SourceKind.IMAGE,
                            received_at=datetime.now(UTC), text="private details",
                            title="flyer.png", attachments=())
    @asynccontextmanager
    async def image_source(_hass, _file_id):
        yield source
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
                            async_record_parse_failure=AsyncMock(return_value="failed-id"))
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_image_source", image_source)
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider",
                        AsyncMock(side_effect=ServiceValidationError("parser unavailable")))
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore",
                        lambda _hass: store)
    await async_setup_entry(hass, entry())
    submit = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_IMAGE)][0]
    with pytest.raises(ServiceValidationError, match="parser unavailable"):
        await submit(SimpleNamespace(data={ATTR_FILE_ID: "a" * 32, ATTR_TEXT: "private details"},
                                     context=Context(user_id=None)))
    store.async_record_parse_failure.assert_awaited_once_with("activity-id")
    assert store.async_begin_submission.await_args.kwargs == {
        "source_kind": "image", "source_title": "flyer.png", "received_at": source.received_at,
    }
    store.async_record_parse_failure.side_effect = RuntimeError("disk full")
    with pytest.raises(ServiceValidationError, match="parser unavailable"):
        await submit(SimpleNamespace(data={ATTR_FILE_ID: "a" * 32, ATTR_TEXT: "private details"},
                                     context=Context(user_id=None)))


async def test_submit_text_skips_ai_for_known_source(monkeypatch):
    hass = FakeHass()
    parse = AsyncMock()
    pending_store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        is_source_duplicate=Mock(return_value=True),
        async_add=AsyncMock(),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
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
        "warnings": [],
    }
    parse.assert_not_awaited()
    pending_store.async_add.assert_not_awaited()


async def test_submit_text_authorizes_before_source_lookup(monkeypatch):
    permissions = FakePermissions(allowed=False)
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    parse = AsyncMock()
    is_source_duplicate = Mock()
    pending_store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        is_source_duplicate=is_source_duplicate,
        async_add=AsyncMock(),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
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
    parse = AsyncMock(return_value=ParseOutcome([draft()], []))
    pending_store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        is_source_duplicate=Mock(return_value=False),
        async_add=AsyncMock(return_value=add_result),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
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
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        async_process_events=AsyncMock(return_value=None), get=Mock(return_value=None),
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
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        get=Mock(return_value=None), async_process_events=AsyncMock(
            side_effect=PendingImportApprovalUncertainError("pending-1")
        ),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: pending_store,
    )

    await async_setup_entry(hass, entry())
    approve_handler = hass.services.handlers[(DOMAIN, SERVICE_APPROVE_PENDING)][0]

    with pytest.raises(ServiceValidationError) as error:
        await approve_handler(
            SimpleNamespace(
                data={ATTR_PENDING_ID: "pending-1"},
                context=Context(user_id="reviewer"),
            )
        )
    assert str(error.value) == (
        "Pending import has an unfinished approval attempt; "
        "automatic retry is blocked to avoid duplicates. Check the "
        "calendar and use resolve_pending_event for the uncertain event"
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


def test_ai_task_configuration_prefers_options_and_falls_back_to_data():
    assert _ai_task_configuration(entry()) == "ai_task.test"
    assert _ai_task_configuration(
        entry({CONF_AI_TASK_ENTITY: "ai_task.updated"})
    ) == "ai_task.updated"


async def test_parse_text_with_updated_ai_option(monkeypatch):
    permissions = FakePermissions(allowed=True)
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    parse = AsyncMock(return_value=ParseOutcome([], []))
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.parse_source_with_provider",
        parse,
    )
    context = Context(user_id="allowed-user")
    configured = entry({CONF_AI_TASK_ENTITY: "ai_task.updated"})

    assert await _parse_text_with_ai_task(
        hass, _ai_task_configuration(configured), "text", context=context
    ) == ParseOutcome([], [])
    assert permissions.calls == [("ai_task.updated", POLICY_CONTROL)]
    assert parse.await_args.kwargs["ai_task_entity"] == "ai_task.updated"


async def test_parse_text_with_ai_task(monkeypatch):
    permissions = FakePermissions(allowed=True)
    user = SimpleNamespace(permissions=permissions)
    hass = FakeHass(user=user)
    parse = AsyncMock(return_value=ParseOutcome([], []))
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
    context = Context(user_id="allowed-user")

    assert await _parse_text_with_ai_task(
        hass, "ai_task.test", "text", context=context
    ) == ParseOutcome([], [])
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

async def test_calendar_event_payload_contract_is_exact():
    context = Context(user_id="calendar-user")
    hass = FakeHass()

    await _async_create_calendar_event(
        hass, "calendar.family", draft(), context=context
    )
    assert hass.services.calls[-1] == (
        "calendar",
        "create_event",
        {
            "entity_id": "calendar.family",
            "summary": "Practice",
            "description": "",
            "location": "Park",
            "start_date_time": "2026-10-08T17:30:00-07:00",
            "end_date_time": "2026-10-08T18:30:00-07:00",
        },
        True,
        context,
    )

    await _async_create_calendar_event(
        hass, "calendar.family", draft(True), context=context
    )
    assert hass.services.calls[-1] == (
        "calendar",
        "create_event",
        {
            "entity_id": "calendar.family",
            "summary": "Practice",
            "description": "",
            "location": "Park",
            "start_date": "2026-10-08",
            "end_date": "2026-10-09",
        },
        True,
        context,
    )


async def test_service_registration_contracts(monkeypatch):
    from custom_components.daylight_calendar_import import PARSE_SCHEMA

    hass = FakeHass()
    pending_store = SimpleNamespace(async_load=AsyncMock())
    received_hass = []

    def store_factory(value):
        received_hass.append(value)
        return pending_store

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        store_factory,
    )
    await async_setup_entry(hass, entry())
    assert received_hass == [hass]

    expected = {
        SERVICE_PARSE_TEXT: (PARSE_SCHEMA, SupportsResponse.ONLY),
        SERVICE_IMPORT_TEXT: (PARSE_SCHEMA, SupportsResponse.OPTIONAL),
        SERVICE_SUBMIT_TEXT: (SUBMIT_SCHEMA, SupportsResponse.ONLY),
        SERVICE_SUBMIT_IMAGE: (SUBMIT_IMAGE_SCHEMA, SupportsResponse.ONLY),
        SERVICE_SUBMIT_PDF: (SUBMIT_PDF_SCHEMA, SupportsResponse.ONLY),
        SERVICE_APPROVE_PENDING: (PENDING_SCHEMA, SupportsResponse.OPTIONAL),
        SERVICE_REJECT_PENDING: (PENDING_SCHEMA, SupportsResponse.OPTIONAL),
        SERVICE_LIST_PENDING: (None, SupportsResponse.ONLY),
        SERVICE_GET_PENDING: (PENDING_SCHEMA, SupportsResponse.ONLY),
        SERVICE_GET_PENDING_EVENT: (PENDING_EVENT_SCHEMA, SupportsResponse.ONLY),
        SERVICE_EDIT_PENDING_EVENT: (EDIT_EVENT_SCHEMA, SupportsResponse.ONLY),
        SERVICE_REJECT_PENDING_EVENT: (
            PENDING_EVENT_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        SERVICE_APPROVE_PENDING_EVENT: (
            PENDING_EVENT_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
        SERVICE_RESOLVE_PENDING_EVENT: (
            RESOLVE_EVENT_SCHEMA,
            SupportsResponse.OPTIONAL,
        ),
    }
    for service, (schema, supports_response) in expected.items():
        _handler, options = hass.services.handlers[(DOMAIN, service)]
        assert options.get("schema") is schema
        assert options["supports_response"] is supports_response


async def test_parse_import_and_submit_preserve_handler_arguments(monkeypatch):
    config_entry = entry()
    hass = FakeHass()
    event = draft()
    submitted = pending(event)
    parse_text_with_ai_task = AsyncMock(return_value=ParseOutcome([event], []))
    parse_text = AsyncMock(return_value=ParseOutcome([event], []))
    create_calendar_event = AsyncMock()
    pending_store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        is_source_duplicate=Mock(return_value=False),
        async_add=AsyncMock(
            return_value=PendingImportAddResult(
                pending=submitted,
                duplicate_source=False,
                duplicate_events=0,
            )
        ),
    )

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda value: pending_store if value is hass else None,
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import._parse_text_with_ai_task",
        parse_text_with_ai_task,
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.parse_source_with_provider",
        parse_text,
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import._async_create_calendar_event",
        create_calendar_event,
    )
    await async_setup_entry(hass, config_entry)
    config_entry.options[CONF_AI_TASK_ENTITY] = "ai_task.changed_after_setup"

    context = Context(user_id=None)
    parse_handler = hass.services.handlers[(DOMAIN, SERVICE_PARSE_TEXT)][0]
    await parse_handler(SimpleNamespace(data={ATTR_TEXT: "parse me"}, context=context))
    parse_text_with_ai_task.assert_awaited_once_with(
        hass, "ai_task.test", "parse me", context=context
    )

    import_handler = hass.services.handlers[(DOMAIN, SERVICE_IMPORT_TEXT)][0]
    await import_handler(
        SimpleNamespace(data={ATTR_TEXT: "import me"}, context=context)
    )
    assert parse_text_with_ai_task.await_args_list[-1].args == (
        hass,
        "ai_task.test",
        "import me",
    )
    assert parse_text_with_ai_task.await_args_list[-1].kwargs == {"context": context}
    create_calendar_event.assert_awaited_once_with(
        hass, "calendar.family", event, context=context
    )

    submit_handler = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]
    await submit_handler(
        SimpleNamespace(
            data={ATTR_TEXT: "submit me", ATTR_SOURCE_ID: "message-42"},
            context=context,
        )
    )
    parse_text.assert_awaited_once_with(
        hass,
        source=ANY,
        ai_task_entity="ai_task.test",
    )
    pending_store.async_add.assert_awaited_once_with(
        source_text="submit me",
        events=[event],
        source_id="message-42",
        calendar_entity="calendar.family",
        warnings=[],
        routing_unresolved=False,
        activity_id="activity-id",
    )


async def test_parse_text_preserves_parser_arguments(monkeypatch):
    parse = AsyncMock(return_value=ParseOutcome([], []))
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.parse_source_with_provider", parse
    )
    hass = FakeHass()

    assert await _parse_text_with_ai_task(
        hass, "ai_task.test", "source text"
    ) == ParseOutcome([], [])
    parse.assert_awaited_once_with(
        hass,
        source=ANY,
        ai_task_entity="ai_task.test",
    )


async def test_pending_summary_and_lookup_preserve_identity(monkeypatch):
    permissions = FakePermissions(allowed=True)
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    first = draft()
    second = EventDraft(
        title="Picture Day",
        start="2026-10-09",
        end="2026-10-10",
        all_day=True,
        confidence=1,
    )
    item = PendingImport.create(
        source_text="private invitation",
        events=[first, second],
    )
    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        list=Mock(return_value=(item,)),
        get=Mock(return_value=item),
        get_event=Mock(return_value=item.events[0]),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: store,
    )
    await async_setup_entry(hass, entry())
    context = Context(user_id="reviewer")

    list_handler = hass.services.handlers[(DOMAIN, SERVICE_LIST_PENDING)][0]
    result = await list_handler(SimpleNamespace(data={}, context=context))
    assert result["imports"][0]["title"] == "Practice"

    get_handler = hass.services.handlers[(DOMAIN, SERVICE_GET_PENDING)][0]
    details = await get_handler(
        SimpleNamespace(data={ATTR_PENDING_ID: item.id}, context=context)
    )
    assert details["pending"]["id"] == item.id
    assert "source_fingerprint" not in details["pending"]
    store.get.assert_called_once_with(item.id)


async def test_remove_entry_uses_current_hass_instance(monkeypatch):
    hass = FakeHass()
    remove_storage = AsyncMock()
    received_hass = []

    def store_factory(value):
        received_hass.append(value)
        return SimpleNamespace(async_remove_storage=remove_storage)

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        store_factory,
    )
    await async_remove_entry(hass, entry())

    assert received_hass == [hass]
    remove_storage.assert_awaited_once_with()


async def test_event_calendar_edit_and_approvals_route_to_selected_destination(monkeypatch):
    from custom_components.daylight_calendar_import.storage import PendingEvent

    item = PendingImport.create(source_text="two events", events=[draft(), draft(True)])
    first, second = item.events
    selected = PendingEvent(second.id, second.draft, calendar_entity="calendar.work")

    async def approve_one(_pending_id, _event_id, processor):
        await processor(selected)
        return selected

    async def approve_all(_pending_id, processor):
        await processor(first)
        await processor(selected)
        return item

    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), get=Mock(return_value=item),
        get_event=Mock(return_value=selected),
        async_edit_event=AsyncMock(return_value=selected),
        async_approve_event=AsyncMock(side_effect=approve_one),
        async_process_events=AsyncMock(side_effect=approve_all),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    config = entry()
    config.data[CONF_CALENDAR_ENTITIES] = ["calendar.family", "calendar.work"]
    permissions = FakePermissions()
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    await async_setup_entry(hass, config)
    context = Context(user_id="reviewer")
    edit = hass.services.handlers[(DOMAIN, SERVICE_EDIT_PENDING_EVENT)][0]
    edit_call = SimpleNamespace(data={
        ATTR_PENDING_ID: item.id, ATTR_EVENT_ID: second.id,
        "event": second.draft.as_dict(), CONF_CALENDAR_ENTITY: "calendar.work",
    }, context=context)
    assert (await edit(edit_call))["event"][CONF_CALENDAR_ENTITY] == "calendar.work"
    store.async_edit_event.assert_awaited_once_with(
        item.id, second.id, second.draft, calendar_entity="calendar.work", expected_event=None
    )
    edit_call.data[CONF_CALENDAR_ENTITY] = "calendar.unlisted"
    with pytest.raises(ServiceValidationError, match="allowed calendars"):
        await edit(edit_call)
    store.async_edit_event.assert_awaited_once()

    approve_event = hass.services.handlers[(DOMAIN, SERVICE_APPROVE_PENDING_EVENT)][0]
    await approve_event(SimpleNamespace(
        data={ATTR_PENDING_ID: item.id, ATTR_EVENT_ID: second.id}, context=context
    ))
    assert hass.services.calls[-1][2]["entity_id"] == "calendar.work"
    approve_all_handler = hass.services.handlers[(DOMAIN, SERVICE_APPROVE_PENDING)][0]
    await approve_all_handler(SimpleNamespace(data={ATTR_PENDING_ID: item.id}, context=context))
    assert [call[2]["entity_id"] for call in hass.services.calls] == [
        "calendar.work", "calendar.family", "calendar.work"
    ]
    assert ("calendar.work", POLICY_CONTROL) in permissions.calls


async def test_other_calendar_requires_control_before_batch_write(monkeypatch):
    from custom_components.daylight_calendar_import.storage import PendingEvent

    item = pending()
    selected = PendingEvent(item.events[0].id, item.events[0].draft,
                            calendar_entity="calendar.work")
    item = PendingImport(item.id, item.created_at, item.source_text, (selected,))
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), get=Mock(return_value=item),
                            async_process_events=AsyncMock())
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    config = entry()
    config.data[CONF_CALENDAR_ENTITIES] = ["calendar.family", "calendar.work"]
    permissions = FakePermissions({"calendar.family": True, "calendar.work": False})
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    await async_setup_entry(hass, config)
    handler = hass.services.handlers[(DOMAIN, SERVICE_APPROVE_PENDING)][0]
    with pytest.raises(Unauthorized):
        await handler(SimpleNamespace(data={ATTR_PENDING_ID: item.id},
                                      context=Context(user_id="reviewer")))
    store.async_process_events.assert_not_awaited()
    assert hass.services.calls == []


async def test_routed_batch_does_not_require_unused_default_calendar(monkeypatch):
    from custom_components.daylight_calendar_import.storage import PendingEvent

    item = pending()
    selected = PendingEvent(item.events[0].id, item.events[0].draft,
                            calendar_entity="calendar.work")
    item = PendingImport(item.id, item.created_at, item.source_text, (selected,))

    async def approve(_pending_id, processor):
        await processor(selected)
        return item

    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), get=Mock(return_value=item),
                            async_process_events=AsyncMock(side_effect=approve))
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    config = entry()
    config.data[CONF_CALENDAR_ENTITIES] = ["calendar.family", "calendar.work"]
    permissions = FakePermissions({"calendar.family": False, "calendar.work": True})
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    await async_setup_entry(hass, config)
    handler = hass.services.handlers[(DOMAIN, SERVICE_APPROVE_PENDING)][0]
    assert (await handler(SimpleNamespace(
        data={ATTR_PENDING_ID: item.id}, context=Context(user_id="reviewer")
    )))["approved"] is True
    assert permissions.calls == [("calendar.work", POLICY_CONTROL)] * 2
    assert hass.services.calls[0][2]["entity_id"] == "calendar.work"
    store.get.assert_called_once_with(item.id)


@pytest.mark.parametrize("single", [False, True])
async def test_approval_rechecks_destination_changed_after_preflight(monkeypatch, single):
    from custom_components.daylight_calendar_import.storage import PendingEvent

    item = pending()
    original = item.events[0]
    changed = PendingEvent(original.id, original.draft, calendar_entity="calendar.work")

    async def process(*args):
        await args[-1](changed)
        return changed

    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), get=Mock(return_value=item),
        get_event=Mock(return_value=original),
        async_process_events=AsyncMock(side_effect=process),
        async_approve_event=AsyncMock(side_effect=process),
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    config = entry()
    config.data[CONF_CALENDAR_ENTITIES] = ["calendar.family", "calendar.work"]
    permissions = FakePermissions({
        "ai_task.test": True, "calendar.family": True, "calendar.work": False,
    })
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    await async_setup_entry(hass, config)
    service = SERVICE_APPROVE_PENDING_EVENT if single else SERVICE_APPROVE_PENDING
    data = {ATTR_PENDING_ID: item.id}
    if single:
        data[ATTR_EVENT_ID] = original.id
    with pytest.raises(Unauthorized):
        await hass.services.handlers[(DOMAIN, service)][0](SimpleNamespace(
            data=data, context=Context(user_id="reviewer")
        ))
    assert ("calendar.work", POLICY_CONTROL) in permissions.calls
    assert hass.services.calls == []


async def test_unlisted_persisted_destination_blocks_calendar_write(monkeypatch):
    from custom_components.daylight_calendar_import.storage import PendingEvent

    item = pending()
    selected = PendingEvent(item.events[0].id, item.events[0].draft,
                            calendar_entity="calendar.unlisted")
    item = PendingImport(item.id, item.created_at, item.source_text, (selected,))
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), get=Mock(return_value=item),
                            async_process_events=AsyncMock())
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions()))
    await async_setup_entry(hass, entry())
    handler = hass.services.handlers[(DOMAIN, SERVICE_APPROVE_PENDING)][0]
    with pytest.raises(ServiceValidationError, match="^Event calendar is not in the allowed calendars$"):
        await handler(SimpleNamespace(data={ATTR_PENDING_ID: item.id},
                                      context=Context(user_id="reviewer")))
    store.async_process_events.assert_not_awaited()


async def test_partial_parser_warnings_reach_parse_import_and_submit(monkeypatch):
    warning = "event 1 is invalid: start must be a non-empty string"
    parse = AsyncMock(return_value=ParseOutcome([draft()], [warning]))
    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        async_add=AsyncMock(return_value=PendingImportAddResult(pending(), False, 0)),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    hass = FakeHass()
    await async_setup_entry(hass, entry())
    context = Context(user_id=None)

    parse_response = await hass.services.handlers[(DOMAIN, SERVICE_PARSE_TEXT)][0](
        SimpleNamespace(data={ATTR_TEXT: "two events"}, context=context)
    )
    assert parse_response == {"events": [draft().as_dict()], "warnings": [warning]}

    import_response = await hass.services.handlers[(DOMAIN, SERVICE_IMPORT_TEXT)][0](
        SimpleNamespace(data={ATTR_TEXT: "two events"}, context=context)
    )
    assert import_response == {
        "events": [draft().as_dict()], "imported": 1, "warnings": [warning]
    }
    assert len(hass.services.calls) == 1

    submit_response = await hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0](
        SimpleNamespace(data={ATTR_TEXT: "two events"}, context=context)
    )
    assert submit_response["warnings"] == [warning]
    store.async_add.assert_awaited_once()
    assert store.async_add.await_args.kwargs["events"] == [draft()]


async def test_all_invalid_events_do_not_write_calendar(monkeypatch):
    parse = AsyncMock(return_value=ParseOutcome([], ["event 0 must be an object"]))
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
    hass = FakeHass()
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), is_source_duplicate=Mock(return_value=False),
                            async_add=AsyncMock(
        return_value=PendingImportAddResult(None, False, 0)
    ))
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    await async_setup_entry(hass, entry())
    context = Context(user_id=None)
    response = await hass.services.handlers[(DOMAIN, SERVICE_IMPORT_TEXT)][0](
        SimpleNamespace(data={ATTR_TEXT: "bad"}, context=context)
    )
    assert response == {"events": [], "imported": 0,
                        "warnings": ["event 0 must be an object"]}
    assert hass.services.calls == []
    submit = await hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0](
        SimpleNamespace(data={ATTR_TEXT: "bad", ATTR_SOURCE_ID: "message-bad"},
                        context=context)
    )
    assert submit["pending"] is None
    assert submit["warnings"] == ["event 0 must be an object"]
    assert store.async_add.await_args.kwargs["events"] == []
    assert store.async_add.await_args.kwargs["source_id"] == "message-bad"


async def test_text_services_normalize_before_parser_and_storage(monkeypatch):
    parse = AsyncMock(return_value=ParseOutcome([draft()], []))
    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"),
        is_source_duplicate=Mock(return_value=False),
        async_add=AsyncMock(return_value=PendingImportAddResult(pending(), False, 0)),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store
    )
    hass = FakeHass()
    await async_setup_entry(hass, entry())
    context = Context(user_id=None)
    await hass.services.handlers[(DOMAIN, SERVICE_PARSE_TEXT)][0](
        SimpleNamespace(data={ATTR_TEXT: "  hello  "}, context=context)
    )
    await hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0](
        SimpleNamespace(data={ATTR_TEXT: "  hello  ", ATTR_SOURCE_ID: "source-1"}, context=context)
    )
    assert [call.kwargs["source"].text for call in parse.await_args_list] == ["hello", "hello"]
    assert store.async_add.await_args.kwargs["source_text"] == "hello"
    assert store.async_add.await_args.kwargs["source_id"] == "source-1"


async def test_text_parser_boundary_rejects_attachment_only_source():
    text_source = TextSourceAdapter().create("x")
    attachment_only = SourceDocument(
        id=text_source.id, kind=SourceKind.PDF, received_at=text_source.received_at
    )
    with pytest.raises(SourceValidationError, match="no text") as caught:
        await _async_parse_source(FakeHass(), attachment_only, "ai_task.test")
    assert caught.value.code == "empty_source"


async def test_submit_image_routes_attachment_and_text_into_review(monkeypatch):
    image_seed = TextSourceAdapter().create("seed")
    image = SourceDocument(
        image_seed.id, SourceKind.IMAGE, image_seed.received_at,
        attachments=(SourceAttachment("image", "image/png", 42, "media-source://media_source/local/image.png", sha256="abc"),),
    )
    config_entry = entry({CONF_AI_TASK_ENTITY: "ai_task.old"})
    @asynccontextmanager
    async def image_source(_hass, file_id):
        assert _hass is hass
        assert file_id == "a" * 32
        config_entry.options[CONF_AI_TASK_ENTITY] = "ai_task.new"
        yield image

    parse = AsyncMock(return_value=ParseOutcome([draft()], []))
    store = SimpleNamespace(
        async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), is_source_duplicate=Mock(return_value=False),
        async_add=AsyncMock(return_value=PendingImportAddResult(pending(), False, 0)),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_image_source", image_source)
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store)
    hass = FakeHass()
    await async_setup_entry(hass, config_entry)
    handler = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_IMAGE)][0]
    result = await handler(SimpleNamespace(data={ATTR_FILE_ID: "a" * 32, ATTR_TEXT: "  Please read  ", ATTR_SOURCE_ID: "upstream"}, context=Context(user_id=None)))
    source = parse.await_args.kwargs["source"]
    assert parse.await_args.args == (hass,)
    assert parse.await_args.kwargs["ai_task_entity"] == "ai_task.old"
    assert source.text == "Please read"
    assert source.attachments == image.attachments
    assert store.async_add.await_args.kwargs["source_text"] == "Please read\n\nImage attachment (SHA-256: abc)"
    assert store.async_add.await_args.kwargs["source_id"] == "upstream"
    assert store.async_add.await_args.kwargs["events"] == [draft()]
    assert store.async_add.await_args.kwargs["calendar_entity"] == "calendar.family"
    assert result["pending"] is not None
    store.is_source_duplicate.return_value = True
    duplicate = await handler(SimpleNamespace(data={ATTR_FILE_ID: "a" * 32, ATTR_SOURCE_ID: "upstream"}, context=Context(user_id=None)))
    assert duplicate == {"pending": None, "duplicate": True, "duplicate_source": True, "duplicate_events": 0, "warnings": []}
    assert parse.await_count == 1
    store.is_source_duplicate.assert_called_with("upstream")


async def test_submit_image_checks_control_permission_and_event_duplicate(monkeypatch):
    permissions = FakePermissions(allowed=True)
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    seed = TextSourceAdapter().create("seed")
    image = SourceDocument(seed.id, SourceKind.IMAGE, seed.received_at,
                           attachments=(SourceAttachment("a", "image/png", 10, "ref", sha256="digest"),))
    @asynccontextmanager
    async def image_source(_hass, _file_id):
        yield image
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), is_source_duplicate=Mock(return_value=False),
                            async_add=AsyncMock(return_value=PendingImportAddResult(None, False, 1)))
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_image_source", image_source)
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", AsyncMock(return_value=ParseOutcome([draft()], [])))
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store)
    await async_setup_entry(hass, entry())
    result = await hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_IMAGE)][0](
        SimpleNamespace(data={ATTR_FILE_ID: "a" * 32}, context=Context(user_id="reviewer")))
    assert result["duplicate"] is True
    assert result["duplicate_events"] == 1
    assert permissions.calls == [("ai_task.test", POLICY_CONTROL)]


async def test_submit_image_only_persists_digest_not_bytes(monkeypatch):
    seed = TextSourceAdapter().create("seed")
    image = SourceDocument(seed.id, SourceKind.IMAGE, seed.received_at,
                           attachments=(SourceAttachment("a", "image/png", 10, "media-source://media_source/local/staged.png", sha256="digest"),))
    @asynccontextmanager
    async def image_source(_hass, _file_id):
        yield image
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), async_add=AsyncMock(return_value=PendingImportAddResult(None, False, 0)))
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_image_source", image_source)
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", AsyncMock(return_value=ParseOutcome([], ["no events"])))
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store)
    hass = FakeHass()
    await async_setup_entry(hass, entry())
    result = await hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_IMAGE)][0](
        SimpleNamespace(data={ATTR_FILE_ID: "a" * 32}, context=Context(user_id=None)))
    assert result == {"pending": None, "duplicate": False, "duplicate_source": False, "duplicate_events": 0, "warnings": ["no events"]}
    assert store.async_add.await_args.kwargs["source_text"] == "Image attachment (SHA-256: digest)"


async def test_submit_pdf_text_layer_uses_review_pipeline(monkeypatch):
    seed = TextSourceAdapter().create("seed")
    pdf = SourceDocument(seed.id, SourceKind.PDF, seed.received_at,
                         text="Context\n\nExtracted schedule", metadata={"sha256": "digest"})
    @asynccontextmanager
    async def pdf_source(_hass, file_id, context):
        assert file_id == "a" * 32
        assert context == "Context"
        yield pdf

    parse = AsyncMock(return_value=ParseOutcome([draft()], []))
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), is_source_duplicate=Mock(return_value=False),
                            async_add=AsyncMock(return_value=PendingImportAddResult(pending(), False, 0)))
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_pdf_source", pdf_source)
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parse)
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store)
    hass = FakeHass()
    await async_setup_entry(hass, entry())
    response = await hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_PDF)][0](
        SimpleNamespace(data={ATTR_FILE_ID: "a" * 32, ATTR_TEXT: "Context", ATTR_SOURCE_ID: "pdf-upstream"}, context=Context(user_id=None)))
    assert parse.await_args.kwargs["source"].kind is SourceKind.PDF
    assert store.async_add.await_args.kwargs["source_text"] == "Context\n\nExtracted schedule"
    assert store.async_add.await_args.kwargs["source_kind"] == "pdf"
    assert store.async_add.await_args.kwargs["warnings"] == []
    assert store.async_add.await_args.kwargs["source_id"] == "pdf-upstream"
    assert response["pending"] is not None


async def test_submit_scanned_pdf_persists_digest_only(monkeypatch):
    seed = TextSourceAdapter().create("seed")
    pdf = SourceDocument(seed.id, SourceKind.PDF, seed.received_at,
                         attachments=(SourceAttachment("a", "application/pdf", 1024,
                                                       "media-source://media_source/local/temp.pdf", sha256="digest"),))
    @asynccontextmanager
    async def pdf_source(_hass, _file_id, context):
        assert context == ""
        yield pdf
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), async_add=AsyncMock(return_value=PendingImportAddResult(None, False, 0)))
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_pdf_source", pdf_source)
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", AsyncMock(return_value=ParseOutcome([], [])))
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store)
    hass = FakeHass()
    await async_setup_entry(hass, entry())
    response = await hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_PDF)][0](
        SimpleNamespace(data={ATTR_FILE_ID: "a" * 32}, context=Context(user_id=None)))
    assert response["pending"] is None
    assert store.async_add.await_args.kwargs["source_text"] == "PDF attachment (SHA-256: digest)"
    assert store.async_add.await_args.kwargs["source_kind"] == "pdf"


async def test_submit_mixed_pdf_preserves_text_and_digest_without_media_reference(monkeypatch):
    seed = TextSourceAdapter().create("seed")
    pdf = SourceDocument(seed.id, SourceKind.PDF, seed.received_at,
                         text="Context\n\nExtracted schedule",
                         attachments=(SourceAttachment("a", "application/pdf", 1024,
                                                       "media-source://media_source/local/temp.pdf", sha256="digest"),))
    @asynccontextmanager
    async def pdf_source(_hass, _file_id, _context):
        yield pdf
    store = SimpleNamespace(async_load=AsyncMock(), async_begin_submission=AsyncMock(return_value="activity-id"), async_add=AsyncMock(return_value=PendingImportAddResult(pending(), False, 0)),
                            is_source_duplicate=Mock(return_value=False))
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_pdf_source", pdf_source)
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider",
                        AsyncMock(return_value=ParseOutcome([draft()], [])))
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore", lambda _hass: store)
    hass = FakeHass()
    await async_setup_entry(hass, entry())

    await hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_PDF)][0](
        SimpleNamespace(data={ATTR_FILE_ID: "a" * 32}, context=Context(user_id=None)))

    assert store.async_add.await_args.kwargs["source_text"] == (
        "Context\n\nExtracted schedule\n\nPDF attachment (SHA-256: digest)")


def test_image_upload_schema_preserves_home_assistant_file_id():
    file_id = "a" * 32
    assert SUBMIT_IMAGE_SCHEMA({ATTR_FILE_ID: file_id})[ATTR_FILE_ID] == file_id
    with pytest.raises(vol.Invalid):
        SUBMIT_IMAGE_SCHEMA({ATTR_FILE_ID: "../../some-file"})



async def test_setup_email_runtime_reuses_parser_store_and_default_calendar(
    monkeypatch,
):
    hass = FakeHass()
    runtime = SimpleNamespace(async_stop=AsyncMock())
    captured = {}
    setup_runtime = AsyncMock(return_value=runtime)
    parse = AsyncMock(return_value=ParseOutcome([draft()], ["Review time"]))
    pending_store = SimpleNamespace(
        async_load=AsyncMock(),
        async_record_parse_failure=AsyncMock(),
        async_add=AsyncMock(
            return_value=PendingImportAddResult(
                pending=pending(draft()),
                duplicate_source=False,
                duplicate_events=0,
            )
        ),
    )

    async def capture_runtime(hass_arg, entry_arg, store_arg, processor):
        captured["args"] = (hass_arg, entry_arg, store_arg)
        captured["processor"] = processor
        return await setup_runtime(hass_arg, entry_arg, store_arg, processor)

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: pending_store,
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.parse_source_with_provider",
        parse,
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_setup_email_runtime",
        capture_runtime,
    )
    config_entry = entry({CONF_AI_TASK_ENTITY: "ai_task.updated"})

    assert await async_setup_entry(hass, config_entry) is True
    assert captured["args"] == (hass, config_entry, pending_store)
    assert pending_store.email_runtime is runtime

    document = SourceDocument(
        id="email-doc",
        kind=SourceKind.EMAIL,
        received_at=datetime(2026, 10, 3, 12, 0, tzinfo=UTC),
        text="Friday at 5",
        title="School notice",
        metadata={"sender": "Teacher <teacher@example.test>"},
        upstream_source_id="<school@example.test>",
    )
    await captured["processor"](document, "email-activity")

    parse.assert_awaited_once_with(
        hass,
        source=document,
        ai_task_entity="ai_task.updated",
    )
    pending_store.async_add.assert_awaited_once_with(
        source_text="Friday at 5",
        events=[draft()],
        source_id="<school@example.test>",
        calendar_entity="calendar.family",
        source_kind="email",
        source_title="School notice",
        source_sender="Teacher <teacher@example.test>",
        warnings=["Review time"],
        routing_unresolved=False,
        activity_id="email-activity",
    )

    assert await async_unload_entry(hass, config_entry) is True
    runtime.async_stop.assert_awaited_once_with()



def test_calendar_configuration_prefers_options_and_preserves_allowed_calendars():
    entry = SimpleNamespace(
        data={
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
        },
        options={CONF_CALENDAR_ENTITY: "calendar.new"},
    )

    assert _calendar_configuration(entry) == (
        "calendar.new",
        ("calendar.family", "calendar.work", "calendar.new"),
    )


def test_calendar_configuration_uses_exact_allowed_calendar_option():
    configured = SimpleNamespace(
        data={
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: [
                "calendar.family",
                "calendar.work",
                "calendar.stale",
            ],
        },
        options={
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.work"],
        },
    )

    assert _calendar_configuration(configured) == (
        "calendar.family",
        ("calendar.family", "calendar.work"),
    )


def test_calendar_configuration_falls_back_to_entry_data():
    entry = SimpleNamespace(
        data={CONF_CALENDAR_ENTITY: "calendar.family"},
        options={},
    )

    assert _calendar_configuration(entry) == (
        "calendar.family",
        ("calendar.family",),
    )


async def test_unload_keeps_store_if_platform_unload_fails():
    hass = FakeHass()
    config_entry = entry()
    store = SimpleNamespace(active_submissions=set(), email_runtime=None)
    hass.data[DOMAIN] = {config_entry.entry_id: store}
    hass.config_entries.async_unload_platforms.return_value = False
    assert await async_unload_entry(hass, config_entry) is False
    assert hass.data[DOMAIN][config_entry.entry_id] is store


async def test_failed_sensor_forwarding_rolls_back_without_service_leaks(monkeypatch):
    hass = FakeHass()
    config_entry = entry()
    store = SimpleNamespace(async_load=AsyncMock())
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore",
        lambda _hass: store,
    )
    hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("sensor setup failed")
    with pytest.raises(RuntimeError, match="sensor setup failed"):
        await async_setup_entry(hass, config_entry)
    assert DOMAIN not in hass.data or config_entry.entry_id not in hass.data[DOMAIN]
    assert hass.services.handlers == {}


def _fake_lifecycle_store(monkeypatch):
    """Return a fresh fake on retries, preserving the first for assertions."""
    store = SimpleNamespace(async_load=AsyncMock())
    created = False

    def factory(_hass):
        nonlocal created
        if not created:
            created = True
            return store
        return SimpleNamespace(async_load=AsyncMock())

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.PendingImportStore", factory,
    )
    return store


async def test_unload_blocks_new_service_calls_and_drains_accepted_before_store_removal(monkeypatch):
    """A handler awaiting its first permission check cannot outlive its store."""
    hass = FakeHass()
    _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    parsing = asyncio.Event()
    finish_parsing = asyncio.Event()
    unloading = asyncio.Event()
    finish_platform = asyncio.Event()

    async def stalled_parse(*args, **kwargs):
        parsing.set()
        await finish_parsing.wait()
        return ParseOutcome([draft()], [])

    async def delayed_platform_unload(*args):
        unloading.set()
        await finish_platform.wait()
        return True

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import._parse_text_with_ai_task",
        stalled_parse,
    )
    hass.config_entries.async_unload_platforms.side_effect = delayed_platform_unload
    await async_setup_entry(hass, config_entry)
    old_store = hass.data[DOMAIN][config_entry.entry_id]
    handler = hass.services.handlers[(DOMAIN, SERVICE_PARSE_TEXT)][0]
    call = SimpleNamespace(data={ATTR_TEXT: "Practice"}, context=Context(user_id=None))

    accepted = asyncio.create_task(handler(call))
    await parsing.wait()
    assert accepted in old_store.active_service_handlers

    unloading_task = asyncio.create_task(async_unload_entry(hass, config_entry))
    await unloading.wait()
    assert old_store.accepting_services is False
    with pytest.raises(ServiceValidationError) as error:
        await handler(call)
    assert str(error.value) == "Daylight is unloading; retry after reload"
    assert old_store.active_service_handlers == {accepted}

    finish_platform.set()
    await asyncio.sleep(0)
    assert not unloading_task.done()
    assert hass.data[DOMAIN][config_entry.entry_id] is old_store

    finish_parsing.set()
    assert (await accepted)["events"][0]["title"] == "Practice"
    assert await unloading_task is True
    assert config_entry.entry_id not in hass.data[DOMAIN]
    assert hass.services.handlers == {}


async def test_email_setup_failure_cleans_registered_services_and_panel(monkeypatch):
    hass = FakeHass()
    _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_setup_email_runtime",
        AsyncMock(side_effect=ValueError("invalid persisted mailbox")),
    )
    remove_panel = Mock()
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_remove_review_panel",
        remove_panel,
    )
    with pytest.raises(ValueError, match="invalid persisted mailbox"):
        await async_setup_entry(hass, config_entry)
    assert hass.services.handlers == {}
    assert config_entry.entry_id not in hass.data[DOMAIN]
    remove_panel.assert_called_once_with(hass)
    hass.config_entries.async_forward_entry_setups.assert_not_awaited()
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        config_entry, ["sensor"]
    )


async def test_unload_platform_exception_restores_service_admission(monkeypatch):
    hass = FakeHass()
    _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    await async_setup_entry(hass, config_entry)
    store = hass.data[DOMAIN][config_entry.entry_id]
    hass.config_entries.async_unload_platforms.side_effect = RuntimeError("cannot unload")
    with pytest.raises(RuntimeError, match="cannot unload"):
        await async_unload_entry(hass, config_entry)
    assert store.accepting_services is True
    assert hass.services.handlers
    assert hass.data[DOMAIN][config_entry.entry_id] is store


async def test_failing_platform_forwarding_unloads_started_email_runtime(monkeypatch):
    hass = FakeHass()
    _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    runtime = SimpleNamespace(async_stop=AsyncMock())
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_setup_email_runtime",
        AsyncMock(return_value=runtime),
    )
    hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("forward failed")
    with pytest.raises(RuntimeError, match="forward failed"):
        await async_setup_entry(hass, config_entry)
    runtime.async_stop.assert_awaited_once_with()
    assert hass.services.handlers == {}
    assert config_entry.entry_id not in hass.data[DOMAIN]
    hass.config_entries.async_unload_platforms.assert_awaited_once_with(
        config_entry, ["sensor"]
    )


async def test_forwarding_failure_and_rollback_unload_error_preserves_first_cause(
    monkeypatch, caplog,
):
    hass = FakeHass()
    _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("forward failed")
    hass.config_entries.async_unload_platforms.side_effect = RuntimeError("rollback failed")
    with pytest.raises(RuntimeError, match="forward failed"):
        await async_setup_entry(hass, config_entry)
    assert any(
        record.message == "Failed to roll back sensor setup"
        for record in caplog.records
    )
    assert hass.services.handlers == {}
    assert hass.data[DOMAIN][config_entry.entry_id].rollback_pending is True


async def test_cancelled_platform_forwarding_does_not_leak_services(monkeypatch):
    hass = FakeHass()
    _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    hass.config_entries.async_forward_entry_setups.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await async_setup_entry(hass, config_entry)
    assert hass.services.handlers == {}
    assert config_entry.entry_id not in hass.data[DOMAIN]
    hass.config_entries.async_unload_platforms.assert_awaited_once()



async def test_failed_sensor_rollback_keeps_store_until_retry_cleanup(monkeypatch, caplog):
    hass = FakeHass()
    old_store = _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("forward interrupted")
    hass.config_entries.async_unload_platforms.return_value = False
    with pytest.raises(RuntimeError, match="forward interrupted"):
        await async_setup_entry(hass, config_entry)
    assert any(
        record.message == "Failed to roll back Daylight sensor platform"
        for record in caplog.records
    )
    assert hass.data[DOMAIN][config_entry.entry_id] is old_store
    assert old_store.rollback_pending is True

    # A failed cleanup on retry must not install a second store.
    with pytest.raises(RuntimeError) as error:
        await async_setup_entry(hass, config_entry)
    assert str(error.value) == "Previous Daylight sensor rollback is incomplete"
    assert hass.data[DOMAIN][config_entry.entry_id] is old_store

    # The next successful cleanup allows a fresh entry setup.
    hass.config_entries.async_unload_platforms.return_value = True
    hass.config_entries.async_forward_entry_setups.side_effect = None
    assert await async_setup_entry(hass, config_entry) is True
    assert hass.data[DOMAIN][config_entry.entry_id] is not old_store
    assert hass.data[DOMAIN][config_entry.entry_id].accepting_services is True


async def test_duplicate_setup_never_discards_live_store(monkeypatch):
    hass = FakeHass()
    _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    await async_setup_entry(hass, config_entry)
    existing = hass.data[DOMAIN][config_entry.entry_id]
    with pytest.raises(RuntimeError) as error:
        await async_setup_entry(hass, config_entry)
    assert str(error.value) == "Daylight entry is already initialized"
    assert hass.data[DOMAIN][config_entry.entry_id] is existing


async def test_rollback_email_stop_failure_keeps_runtime_for_retry(monkeypatch, caplog):
    hass = FakeHass()
    _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    runtime = SimpleNamespace(async_stop=AsyncMock(side_effect=RuntimeError("email stop failed")))
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_setup_email_runtime",
        AsyncMock(return_value=runtime),
    )
    hass.config_entries.async_forward_entry_setups.side_effect = RuntimeError("sensor failed")
    with pytest.raises(RuntimeError, match="sensor failed"):
        await async_setup_entry(hass, config_entry)
    assert any(
        record.message == "Failed to stop email runtime during setup rollback"
        for record in caplog.records
    )
    previous = hass.data[DOMAIN][config_entry.entry_id]
    assert previous.rollback_pending is True
    assert previous.email_runtime is runtime

    runtime.async_stop.side_effect = None
    hass.config_entries.async_forward_entry_setups.side_effect = None
    await async_setup_entry(hass, config_entry)
    assert runtime.async_stop.await_count == 2
    assert hass.data[DOMAIN][config_entry.entry_id] is not previous


async def test_cancelled_unload_finishes_draining_without_cancelling_accepted_handler(monkeypatch):
    hass = FakeHass()
    _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    handler_started = asyncio.Event()
    handler_release = asyncio.Event()

    async def slow_parse(*args, **kwargs):
        handler_started.set()
        await handler_release.wait()
        return ParseOutcome([draft()], [])

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import._parse_text_with_ai_task",
        slow_parse,
    )
    await async_setup_entry(hass, config_entry)
    handler = hass.services.handlers[(DOMAIN, SERVICE_PARSE_TEXT)][0]
    call = SimpleNamespace(data={ATTR_TEXT: "Practice"}, context=Context(user_id=None))
    in_flight = asyncio.create_task(handler(call))
    await handler_started.wait()

    unload = asyncio.create_task(async_unload_entry(hass, config_entry))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    unload.cancel()
    await asyncio.sleep(0)
    assert not in_flight.cancelled()
    assert not unload.done()
    assert config_entry.entry_id in hass.data[DOMAIN]

    handler_release.set()
    assert (await in_flight)["events"][0]["title"] == "Practice"
    with pytest.raises(asyncio.CancelledError):
        await unload
    assert config_entry.entry_id not in hass.data[DOMAIN]
    assert not hass.services.handlers


async def test_cancelled_unload_waiting_for_platform_does_not_orphan_entry(monkeypatch):
    hass = FakeHass()
    _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    await async_setup_entry(hass, config_entry)
    platform_started = asyncio.Event()
    platform_release = asyncio.Event()

    async def wait_for_sensor_unload(*args):
        platform_started.set()
        await platform_release.wait()
        return True

    hass.config_entries.async_unload_platforms.side_effect = wait_for_sensor_unload
    unloading = asyncio.create_task(async_unload_entry(hass, config_entry))
    await platform_started.wait()
    unloading.cancel()
    await asyncio.sleep(0)
    assert not unloading.done()
    platform_release.set()
    with pytest.raises(asyncio.CancelledError):
        await unloading
    assert config_entry.entry_id not in hass.data[DOMAIN]


async def test_new_services_reject_requests_until_sensor_setup_finishes(monkeypatch):
    hass = FakeHass()
    _fake_lifecycle_store(monkeypatch)
    config_entry = entry()
    forwarding = asyncio.Event()
    finish_forwarding = asyncio.Event()

    async def delayed_forward(*args):
        forwarding.set()
        await finish_forwarding.wait()

    hass.config_entries.async_forward_entry_setups.side_effect = delayed_forward
    setup = asyncio.create_task(async_setup_entry(hass, config_entry))
    await forwarding.wait()
    handler = hass.services.handlers[(DOMAIN, SERVICE_PARSE_TEXT)][0]
    call = SimpleNamespace(data={ATTR_TEXT: "Practice"}, context=Context(user_id=None))
    with pytest.raises(ServiceValidationError) as error:
        await handler(call)
    assert str(error.value) == "Daylight is unloading; retry after reload"
    finish_forwarding.set()
    assert await setup is True
    assert hass.data[DOMAIN][config_entry.entry_id].accepting_services is True


async def test_routing_review_ingestion_uses_allowed_aliases_for_text_and_email(monkeypatch):
    from custom_components.daylight_calendar_import.sources import SourceKind

    permissions = FakePermissions(allowed=True)
    hass = FakeHass(user=SimpleNamespace(permissions=permissions))
    parser = AsyncMock(return_value=ParseOutcome([draft()], ["AI note"]))
    pending_item = pending(draft())
    store = SimpleNamespace(
        async_load=AsyncMock(),
        async_begin_submission=AsyncMock(return_value="activity-id"),
        is_source_duplicate=Mock(return_value=False),
        async_add=AsyncMock(return_value=PendingImportAddResult(
            pending=pending_item, duplicate_source=False, duplicate_events=0)),
    )
    email_processor = None

    async def capture_runtime(_hass, _entry, _store, process):
        nonlocal email_processor
        email_processor = process
        return None

    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore",
                        lambda _: store)
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parser)
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_setup_email_runtime",
                        capture_runtime)
    cfg = entry({
        "calendar_entities": ["calendar.family", "calendar.kids"],
        "calendar_aliases": {"Kids": "calendar.kids"},
    })
    assert await async_setup_entry(hass, cfg)
    call = SimpleNamespace(
        data={ATTR_TEXT: "Calendar: KIDS\nSoccer at five"},
        context=Context(user_id="test-user"),
    )
    handler = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]
    response = await handler(call)
    assert response["warnings"] == ["AI note"]
    assert parser.await_args.kwargs["source"].text == "Soccer at five"
    args = store.async_add.await_args.kwargs
    assert args["calendar_entity"] == "calendar.kids"
    assert args["source_text"] == "Calendar: KIDS\nSoccer at five"
    assert args["warnings"] == ["AI note"]

    source = TextSourceAdapter().create("Calendar: nope\nPlay at seven")
    source = replace(source, kind=SourceKind.EMAIL,
                     title="Calendar: KIDS", metadata={"sender": "school@example.test"})
    assert email_processor is not None
    await email_processor(source, "activity-id")
    assert parser.await_args.kwargs["source"].text == "Play at seven"
    args = store.async_add.await_args.kwargs
    assert args["calendar_entity"] == "calendar.family"
    assert "Conflicting calendar routing hints" in args["warnings"][1]
    assert args["source_sender"] == "school@example.test"


@pytest.mark.parametrize("kind, service", [
    ("image", SERVICE_SUBMIT_IMAGE), ("pdf", SERVICE_SUBMIT_PDF),
])
async def test_attachment_review_routing_strips_only_text_directive(
    monkeypatch, kind, service,
):
    from custom_components.daylight_calendar_import.sources import SourceKind, SourceAttachment

    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions(allowed=True)))
    parser = AsyncMock(return_value=ParseOutcome([draft()], []))
    store = SimpleNamespace(
        async_load=AsyncMock(),
        async_begin_submission=AsyncMock(return_value="activity-id"),
        is_source_duplicate=Mock(return_value=False),
        async_add=AsyncMock(return_value=PendingImportAddResult(
            pending=pending(draft()), duplicate_source=False, duplicate_events=0)),
    )
    attachment = SourceAttachment(
        id="file-id", media_type="image/png" if kind == "image" else "application/pdf",
        size_bytes=100, content_ref="file-ref", sha256="a" * 64,
    )
    @asynccontextmanager
    async def attachment_source(*_args):
        yield replace(
            TextSourceAdapter().create("unused"), kind=SourceKind(kind),
            text=_args[-1] if kind == "pdf" else None,
            title="Calendar: kids", attachments=(attachment,),
        )

    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore",
                        lambda _: store)
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parser)
    module_name = "async_image_source" if kind == "image" else "async_pdf_source"
    monkeypatch.setattr(f"custom_components.daylight_calendar_import.{module_name}",
                        attachment_source)
    assert await async_setup_entry(hass, entry({
        "calendar_entities": ["calendar.family", "calendar.kids"],
        "calendar_aliases": {"kids": "calendar.kids"},
    }))
    handler = hass.services.handlers[(DOMAIN, service)][0]
    call = SimpleNamespace(data={
        ATTR_FILE_ID: "a" * 32,
        ATTR_TEXT: "Calendar: kids\nParty next week",
    }, context=Context(user_id="test-user"))
    result = await handler(call)
    assert result["warnings"] == []
    assert parser.await_args.kwargs["source"].text == "Party next week"
    assert store.async_add.await_args.kwargs["calendar_entity"] == "calendar.kids"
    assert "Calendar: kids" in store.async_add.await_args.kwargs["source_text"]


async def test_unresolved_manual_hint_is_review_warning_not_writable_override(monkeypatch):
    hass = FakeHass(user=SimpleNamespace(permissions=FakePermissions(allowed=True)))
    parser = AsyncMock(return_value=ParseOutcome([draft()], []))
    store = SimpleNamespace(
        async_load=AsyncMock(),
        async_begin_submission=AsyncMock(return_value="activity-id"),
        is_source_duplicate=Mock(return_value=False),
        async_add=AsyncMock(return_value=PendingImportAddResult(
            pending=pending(draft()), duplicate_source=False, duplicate_events=0)),
    )
    monkeypatch.setattr("custom_components.daylight_calendar_import.PendingImportStore",
                        lambda _: store)
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider",
                        parser)
    assert await async_setup_entry(hass, entry())
    handler = hass.services.handlers[(DOMAIN, SERVICE_SUBMIT_TEXT)][0]
    response = await handler(SimpleNamespace(
        data={ATTR_TEXT: "Calendar: private\nSoccer practice"},
        context=Context(user_id="test-user"),
    ))
    assert "not configured or allowed" in response["warnings"][0]
    assert store.async_add.await_args.kwargs["calendar_entity"] == "calendar.family"
    assert store.async_add.await_args.kwargs["warnings"] == response["warnings"]
    assert parser.await_args.kwargs["source"].text == "Soccer practice"


def test_routing_snapshot_preserves_confirmation_state():
    original = PendingEvent("event-id", draft(), routing_unresolved=True)
    assert _expected_event(original.as_service_dict(), original.id) == original
