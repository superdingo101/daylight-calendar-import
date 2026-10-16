"""No silent truncation of substantive descriptions, including recovery paths."""

from copy import deepcopy
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from homeassistant.exceptions import ServiceValidationError

from custom_components.daylight_calendar_import import _async_create_calendar_event
from custom_components.daylight_calendar_import import providers, storage
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.parser import async_parse_text


# Cross a common 100k text boundary and retain Unicode, paragraphs and end markers.
LONG_DESCRIPTION = (
    "Preparation and agenda:\n\n"
    + "• Préparation: bring ID, water and a notebook 📝.\n" * 3000
    + "\nFinal instruction: check in at the east entrance.\n"
    "Join https://meet.google.com/abc-defg-hij?authuser=1"
)


@pytest.mark.parametrize("calendar_rejects", [False, True])
async def test_long_description_parse_edit_restart_and_calendar_result(
    monkeypatch, calendar_rejects,
):
    assert len(LONG_DESCRIPTION) > 100_000
    raw = {
        "title": "Workshop", "start": "2026-10-14T09:00:00-07:00",
        "end": "2026-10-14T10:00:00-07:00", "all_day": False,
        "description": f"  {LONG_DESCRIPTION}\n ", "confidence": 0.99,
        "assumptions": [],
    }
    # Description normalization is explicit: surrounding whitespace only.
    assert EventDraft.from_mapping(raw).description == LONG_DESCRIPTION
    generate = AsyncMock(return_value=SimpleNamespace(data={"events": [raw]}))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    calendar_call = AsyncMock()
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="America/Los_Angeles"),
        services=SimpleNamespace(async_call=calendar_call),
    )
    outcome = await async_parse_text(
        hass,
        text=f"Workshop: {raw['start']} to {raw['end']}\n{LONG_DESCRIPTION}",
        ai_task_entity="ai_task.test",
    )
    assert outcome.warnings == []
    assert outcome.events[0].description == LONG_DESCRIPTION
    instructions = generate.await_args.kwargs["instructions"]
    assert LONG_DESCRIPTION in json.loads(instructions.split("Untrusted source evidence (", 1)[1].split(":\n", 1)[1])
    assert "Do not summarize or silently truncate" in instructions
    assert "preserve paragraph breaks, lists and Unicode text" in instructions

    snapshot = None

    async def save(data):
        nonlocal snapshot
        snapshot = deepcopy(data)

    async def load():
        return deepcopy(snapshot)

    backend = SimpleNamespace(async_save=save, async_load=load)
    monkeypatch.setattr(storage, "_PendingStore", lambda *args, **kwargs: backend)
    queue = storage.PendingImportStore(hass)
    await queue.async_load()
    pending = (await queue.async_add(
        source_text=LONG_DESCRIPTION, events=outcome.events,
        calendar_entity="calendar.family",
    )).pending
    assert pending is not None
    assert pending.as_service_dict()["events"][0]["description"] == LONG_DESCRIPTION
    calendar_call.assert_not_awaited()

    edited_description = LONG_DESCRIPTION + "\nUser note: arrive fifteen minutes early."
    await queue.async_edit_event(
        pending.id, pending.events[0].id,
        replace(outcome.events[0], description=edited_description),
    )
    restarted = storage.PendingImportStore(hass)
    await restarted.async_load()
    restored = restarted.get(pending.id)
    assert restored is not None
    assert restored.as_service_dict()["events"][0]["description"] == edited_description

    async def approve(event):
        await _async_create_calendar_event(hass, event.calendar_entity, event.draft)

    if calendar_rejects:
        calendar_call.side_effect = ServiceValidationError("Destination description too long")
        with pytest.raises(ServiceValidationError, match="Destination description too long"):
            await restarted.async_process_events(pending.id, approve)
        failed = restarted.get(pending.id)
        assert failed is not None
        assert failed.events[0].status == "write_uncertain"
        assert failed.events[0].draft.description == edited_description
        # Persisted failure recovery retains the full editable source details.
        recovered = storage.PendingImportStore(hass)
        await recovered.async_load()
        assert recovered.get(pending.id).events[0].draft.description == edited_description
        assert recovered.get(pending.id).events[0].status == "write_uncertain"
    else:
        await restarted.async_process_events(pending.id, approve)
        assert restarted.get(pending.id) is None

    calendar_call.assert_awaited_once()
    domain, service, payload = calendar_call.await_args.args
    assert (domain, service) == ("calendar", "create_event")
    assert payload["description"] == edited_description
    assert payload["description"].endswith("User note: arrive fifteen minutes early.")
