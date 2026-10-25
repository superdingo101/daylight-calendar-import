"""Public beta routing and conflict journeys through authenticated HA services."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from homeassistant.components.calendar import CalendarEvent
from homeassistant.components.calendar.const import DATA_COMPONENT
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daylight_calendar_import.const import (
    CONF_AI_TASK_ENTITY, CONF_CALENDAR_ALIASES, CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY, CONF_CONFLICT_CALENDAR_ENTITIES, DOMAIN,
)
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.parser import ParseOutcome

DRAFT = EventDraft("Practice", "2026-11-12T17:00:00-08:00", "2026-11-12T18:00:00-08:00", False)


async def call(client, service, **data):
    await client.send_json_auto_id({"type": "call_service", "domain": DOMAIN,
                                   "service": service, "service_data": data,
                                   "return_response": True})
    return await client.receive_json()


@pytest.fixture
async def workflow(hass, hass_ws_client, monkeypatch):
    parser = AsyncMock(return_value=ParseOutcome([DRAFT], []))
    monkeypatch.setattr("custom_components.daylight_calendar_import.parse_source_with_provider", parser)
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_AI_TASK_ENTITY: "ai_task.test",
        CONF_CALENDAR_ENTITY: "calendar.family", CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.kids"]},
        options={CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.kids"], CONF_CALENDAR_ALIASES: {"kids": "calendar.kids"},
                 CONF_CONFLICT_CALENDAR_ENTITIES: ["calendar.work", "calendar.school"]})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    providers = {name: SimpleNamespace(available=True, async_get_events=AsyncMock(return_value=[]))
                 for name in ("calendar.family", "calendar.kids", "calendar.work", "calendar.school")}
    monkeypatch.setitem(hass.data, DATA_COMPONENT, SimpleNamespace(get_entity=providers.get))
    writes = AsyncMock()
    hass.services.async_register("calendar", "create_event", writes)
    return SimpleNamespace(client=await hass_ws_client(hass), parser=parser, providers=providers,
                           writes=writes, store=hass.data[DOMAIN][entry.entry_id])


@pytest.mark.parametrize(("hint", "destination", "unresolved", "changed"), [
    ("", "calendar.family", False, "calendar.kids"),
    ("Calendar: Kids\n", "calendar.kids", False, "calendar.family"),
    ("Calendar: unknown\n", "calendar.family", True, "calendar.kids"),
    ("Calendar: kids\nCalendar: work\n", "calendar.family", True, "calendar.kids"),
    ("Calendar: unknown\n", "calendar.family", True, "calendar.family"),
    ("Calendar: kids\nCalendar: work\n", "calendar.family", True, "calendar.family"),
])
async def test_route_preview_confirmation_and_destination_change(workflow, hint, destination, unresolved, changed):
    runtime = workflow
    submitted = await call(runtime.client, "submit_text", text=hint + "Practice details")
    assert submitted["success"], submitted
    pending = submitted["result"]["response"]["pending"]
    event = pending["events"][0]
    assert event["calendar_entity"] == destination
    assert event.get("routing_unresolved", False) is unresolved
    assert runtime.parser.await_args.kwargs["source"].text == "Practice details"
    ids = {"pending_id": pending["id"], "event_id": event["id"]}
    if unresolved:
        refused = await call(runtime.client, "approve_pending_event", **ids)
        assert refused["success"] is False
        assert "Confirm" in refused["error"]["message"]
        runtime.writes.assert_not_awaited()
        assert runtime.store.get_event(pending["id"], event["id"]).status == "pending"
    # An explicit edit confirms unresolved routing even when the fallback is kept.
    edited = await call(runtime.client, "edit_pending_event", **ids,
                        event=DRAFT.as_dict(), calendar_entity=changed)
    assert edited["success"], edited
    assert edited["result"]["response"]["event"].get("routing_unresolved", False) is False
    approved = await call(runtime.client, "approve_pending_event", **ids)
    assert approved["success"], approved
    runtime.writes.assert_awaited_once()
    assert runtime.writes.await_args.args[0].data["entity_id"] == changed
    assert runtime.store.get(pending["id"]) is None


def existing(title, start=DRAFT.start, end=DRAFT.end):
    return CalendarEvent(start=datetime.fromisoformat(start), end=datetime.fromisoformat(end), summary=title)


async def test_multicalendar_matches_and_exact_duplicate_approval_guard(workflow):
    runtime = workflow
    submitted = await call(runtime.client, "submit_text", text="Practice details")
    pending = submitted["result"]["response"]["pending"]
    ids = {"pending_id": pending["id"], "event_id": pending["events"][0]["id"]}
    runtime.providers["calendar.family"].async_get_events.return_value = [existing(" Practice ")]
    runtime.providers["calendar.work"].async_get_events.return_value = [existing("Practice", end="2026-11-12T18:30:00-08:00")]
    runtime.providers["calendar.school"].async_get_events.return_value = [existing("Meeting")]
    checked = await call(runtime.client, "check_pending_event", **ids)
    assert checked["success"], checked
    result = checked["result"]["response"]
    assert result["observed_calendars"] == ["calendar.family", "calendar.work", "calendar.school"]
    interval = {"existing_start": "2026-11-12T17:00:00-08:00",
                "existing_end": "2026-11-12T18:00:00-08:00", "existing_all_day": False,
                "overlap_start": "2026-11-12T17:00:00-08:00",
                "overlap_end": "2026-11-12T18:00:00-08:00"}
    assert result["matches"] == [
        {**interval, "kind": "exact_duplicate", "calendar_entity": "calendar.family",
         "existing_title": " Practice "},
        {**interval, "kind": "possible_duplicate", "calendar_entity": "calendar.work",
         "existing_title": "Practice", "existing_end": "2026-11-12T18:30:00-08:00"},
        {**interval, "kind": "conflict", "calendar_entity": "calendar.school",
         "existing_title": "Meeting"},
    ]
    runtime.writes.assert_not_awaited()
    refused = await call(runtime.client, "approve_pending_event", **ids)
    assert refused["success"] is False
    assert "Exact duplicate" in refused["error"]["message"]
    assert runtime.store.get_event(**ids).status == "pending"
    runtime.writes.assert_not_awaited()
    # Explicitly move to a clear destination; other calendars' conflicts remain advisory.
    edited = await call(runtime.client, "edit_pending_event", **ids,
                        event=DRAFT.as_dict(), calendar_entity="calendar.kids")
    assert edited["success"], edited
    approved = await call(runtime.client, "approve_pending_event", **ids)
    assert approved["success"], approved
    runtime.writes.assert_awaited_once()
    assert runtime.writes.await_args.args[0].data["entity_id"] == "calendar.kids"


@pytest.mark.parametrize("failure", ["unavailable", "missing", "exception"])
async def test_incomplete_observation_fails_visibly_without_changing_review(workflow, failure):
    runtime = workflow
    submitted = await call(runtime.client, "submit_text", text="Practice details")
    pending = submitted["result"]["response"]["pending"]
    ids = {"pending_id": pending["id"], "event_id": pending["events"][0]["id"]}
    before = runtime.store.get_event(**ids)
    if failure == "unavailable":
        runtime.providers["calendar.school"].available = False
    elif failure == "missing":
        runtime.providers.pop("calendar.school")
    else:
        runtime.providers["calendar.school"].async_get_events.side_effect = TimeoutError("private provider error")
    failed = await call(runtime.client, "check_pending_event", **ids)
    assert failed["success"] is False
    assert "Calendar check incomplete" in failed["error"]["message"]
    assert "private provider error" not in str(failed)
    assert runtime.store.get_event(**ids) == before
    runtime.writes.assert_not_awaited()


async def test_approval_rechecks_destination_after_clear_preview(workflow):
    runtime = workflow
    submitted = await call(runtime.client, "submit_text", text="Practice details")
    assert submitted["success"], submitted
    pending = submitted["result"]["response"]["pending"]
    ids = {"pending_id": pending["id"], "event_id": pending["events"][0]["id"]}
    checked = await call(runtime.client, "check_pending_event", **ids)
    assert checked["success"], checked
    assert checked["result"]["response"]["matches"] == []
    destination_reads = runtime.providers["calendar.family"].async_get_events
    destination_reads.assert_awaited_once()
    # The calendar changes after preview and before approval. A cached clear
    # verdict must never authorize a duplicate write.
    destination_reads.return_value = [existing("Practice")]
    refused = await call(runtime.client, "approve_pending_event", **ids)
    assert refused["success"] is False
    assert "Exact duplicate" in refused["error"]["message"]
    assert destination_reads.await_count == 2
    assert runtime.store.get_event(**ids).status == "pending"
    runtime.writes.assert_not_awaited()
