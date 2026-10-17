"""Deterministic meeting-detail fidelity across parsing and approved calendar writes.

The provider output is controlled; these tests verify our contract and transport,
not a claim about an external model's extraction accuracy.
"""

from copy import deepcopy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import import _async_create_calendar_event
from custom_components.daylight_calendar_import import providers, storage
from custom_components.daylight_calendar_import.parser import async_parse_text


MEETINGS = [
    (
        "Zoom",
        "Join https://us02web.zoom.us/j/12345678901?pwd=AbC%2B123&from=addon\n"
        "Meeting ID: 123 4567 8901\nPasscode: 004201\n"
        "Dial +1 669 900 6833,,12345678901# US. Enter passcode 004201#.",
    ),
    (
        "Google Meet",
        "Join https://meet.google.com/abc-defg-hij?authuser=1\n"
        "Dial +1 650-555-0100; PIN: 001 234 567#\n"
        "More numbers: https://tel.meet/abc-defg-hij?pin=001234567",
    ),
    (
        "Microsoft Teams",
        "Join https://teams.microsoft.com/l/meetup-join/19%3ameeting_ABC%40thread.v2/0?context=%7B%22Tid%22%3A%22abc%22%7D\n"
        "Meeting ID: 987 654 321\nPasscode: A0b!2C\n"
        "Dial +1 619-555-0100,,987654321#; conference ID: 987 654 321#",
    ),
]


@pytest.mark.parametrize("title,details", MEETINGS)
async def test_meeting_details_survive_parse_review_restart_and_approval(
    monkeypatch, title, details,
):
    raw = {
        "title": title, "start": "2026-10-14T09:00:00-07:00",
        "end": "2026-10-14T10:00:00-07:00", "all_day": False,
        "location": "Conference room A", "description": details,
        "confidence": 0.99, "assumptions": [],
    }
    generate = AsyncMock(return_value=SimpleNamespace(data={"unsupported_recurrence": False, "events": [raw]}))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    calendar_call = AsyncMock()
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="America/Los_Angeles"),
        services=SimpleNamespace(async_call=calendar_call),
    )
    outcome = await async_parse_text(
        hass, text=f"{title}\n{raw['start']} to {raw['end']}\n{details}",
        ai_task_entity="ai_task.test",
    )
    assert outcome.warnings == []
    assert outcome.events[0].description == details
    instructions = generate.await_args.kwargs["instructions"]
    assert "URLs (including query parameters)" in instructions
    assert "meeting IDs, passcodes and dial-in numbers/instructions" in instructions
    assert "Copy these details exactly" in instructions
    assert "do not invent missing credentials or links" in instructions
    assert details in json.loads(instructions.split("Untrusted source evidence (", 1)[1].split(":\n", 1)[1])

    saved = None

    async def save(value):
        nonlocal saved
        saved = deepcopy(value)

    async def load():
        return deepcopy(saved)

    backend = SimpleNamespace(async_save=save, async_load=load)
    monkeypatch.setattr(storage, "_PendingStore", lambda *args, **kwargs: backend)
    queue = storage.PendingImportStore(hass)
    await queue.async_load()
    pending = (await queue.async_add(
        source_text=details, events=outcome.events, calendar_entity="calendar.family",
    )).pending
    assert pending is not None
    calendar_call.assert_not_awaited()
    assert pending.as_service_dict()["events"][0]["description"] == details

    restarted = storage.PendingImportStore(hass)
    await restarted.async_load()
    restored = restarted.get(pending.id)
    assert restored is not None
    assert restored.as_service_dict()["events"][0]["description"] == details

    async def approve(event):
        await _async_create_calendar_event(hass, event.calendar_entity, event.draft)

    await restarted.async_process_events(pending.id, approve)
    calendar_call.assert_awaited_once()
    domain, service, payload = calendar_call.await_args.args
    assert (domain, service) == ("calendar", "create_event")
    assert payload["description"] == details
    assert payload["location"] == "Conference room A"
    assert payload["summary"] == title
    assert payload["entity_id"] == "calendar.family"
    assert restarted.get(pending.id) is None
