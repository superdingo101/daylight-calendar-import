"""v0.6 release acceptance tests spanning Home Assistant review boundaries.

These tests deliberately assert user-visible effects and durable state, not
private helper calls: surviving mutations must change an observable result.
"""

from unittest.mock import AsyncMock

import pytest

from homeassistant.config_entries import ConfigEntryState
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daylight_calendar_import.const import (
    ATTR_SOURCE_ID,
    ATTR_TEXT,
    CONF_AI_TASK_ENTITY,
    CONF_CALENDAR_ALIASES,
    CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY,
    DOMAIN,
    SERVICE_SUBMIT_TEXT,
)
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.parser import ParseOutcome
from custom_components.daylight_calendar_import.sensor import pending_counts
from custom_components.daylight_calendar_import.storage import PendingEventEditError


def _entry():
    return MockConfigEntry(
        domain=DOMAIN,
        title="Daylight Calendar Import",
        data={
            CONF_AI_TASK_ENTITY: "ai_task.test",
            CONF_CALENDAR_ENTITY: "calendar.family",
            CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.kids"],
        },
        options={CONF_CALENDAR_ALIASES: {"kids": "calendar.kids"}},
    )


def _events():
    return [
        EventDraft("Practice", "2026-10-08T17:00:00-07:00",
                   "2026-10-08T18:00:00-07:00", False),
        EventDraft("Game", "2026-10-09T17:00:00-07:00",
                   "2026-10-09T18:00:00-07:00", False),
    ]


async def _setup(hass, entry):
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return hass.data[DOMAIN][entry.entry_id]


async def test_routed_multievent_submission_keeps_disclosures_and_queue_after_restart(
    hass, monkeypatch,
):
    """Source control is stripped for AI but durable routing/notes remain in review."""
    events = _events()
    parser = AsyncMock(return_value=ParseOutcome(
        events, [], [("Resolved relative day from reference date",), ()],
    ))
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.parse_source_with_provider", parser,
    )
    config_entry = _entry()
    store = await _setup(hass, config_entry)
    assert pending_counts(store) == (0, 0)

    emitted = []
    cancel = hass.bus.async_listen(
        f"{DOMAIN}_pending_added", lambda event: emitted.append(event.data),
    )
    try:
        text = "Calendar: Kids\nPractice and game schedules"
        request = {ATTR_TEXT: text, ATTR_SOURCE_ID: "v060-two-event-source"}
        response = await hass.services.async_call(
            DOMAIN, SERVICE_SUBMIT_TEXT, request,
            blocking=True, return_response=True,
        )
        await hass.async_block_till_done()

        parser.assert_awaited_once()
        assert parser.await_args.kwargs["source"].text == "Practice and game schedules"
        assert response["duplicate_source"] is False
        assert response["duplicate_events"] == 0
        assert response["warnings"] == []
        pending = response["pending"]
        assert pending["source_text"] == text
        assert [event["title"] for event in pending["events"]] == ["Practice", "Game"]
        assert [event["calendar_entity"] for event in pending["events"]] == [
            "calendar.kids", "calendar.kids",
        ]
        assert [event.get("routing_unresolved", False) for event in pending["events"]] == [
            False, False,
        ]
        assert [event.get("date_time_assumptions", []) for event in pending["events"]] == [
            ["Resolved relative day from reference date"], [],
        ]
        assert pending_counts(store) == (1, 2)
        assert emitted == [{"pending_id": pending["id"], "event_count": 2}]
        assert "Practice and game schedules" not in str(emitted)

        # Loading an already-committed queue does not emit another review-ready event.
        assert await hass.config_entries.async_unload(config_entry.entry_id)
        await hass.async_block_till_done()
        assert await hass.config_entries.async_setup(config_entry.entry_id)
        await hass.async_block_till_done()
        reloaded = hass.data[DOMAIN][config_entry.entry_id]
        persisted = reloaded.get(pending["id"])
        assert persisted is not None
        assert persisted.source_text == text
        assert tuple(event.id for event in persisted.events) == tuple(
            event["id"] for event in pending["events"]
        )
        assert tuple(event.calendar_entity for event in persisted.events) == (
            "calendar.kids", "calendar.kids",
        )
        assert tuple(event.date_time_assumptions for event in persisted.events) == (
            ("Resolved relative day from reference date",), (),
        )
        assert pending_counts(reloaded) == (1, 2)
        assert len(emitted) == 1

        repeated = await hass.services.async_call(
            DOMAIN, SERVICE_SUBMIT_TEXT, request,
            blocking=True, return_response=True,
        )
        assert repeated["duplicate_source"] is True
        assert repeated["pending"] is None
        parser.assert_awaited_once()
        assert pending_counts(reloaded) == (1, 2)

        assert await reloaded.async_remove(pending["id"])
        assert pending_counts(reloaded) == (0, 0)
    finally:
        cancel()


@pytest.mark.parametrize("directive", [
    "Calendar: unknown\nPractice and game schedules",
    "Calendar: kids\nCalendar: work\nPractice and game schedules",
])
async def test_ambiguous_routes_cannot_be_bulk_approved_without_confirmation(
    hass, monkeypatch, directive,
):
    """A preview fallback never becomes authority to create either event."""
    parser = AsyncMock(return_value=ParseOutcome(_events(), []))
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.parse_source_with_provider", parser,
    )
    config_entry = _entry()
    store = await _setup(hass, config_entry)
    response = await hass.services.async_call(
        DOMAIN, SERVICE_SUBMIT_TEXT,
        {ATTR_TEXT: directive, ATTR_SOURCE_ID: "v060-ambiguous-source"},
        blocking=True, return_response=True,
    )
    pending = response["pending"]
    assert pending is not None
    assert len(pending["events"]) == 2
    assert [event["calendar_entity"] for event in pending["events"]] == [
        "calendar.family", "calendar.family",
    ]
    assert [event["routing_unresolved"] for event in pending["events"]] == [
        True, True,
    ]
    assert len(response["warnings"]) == 1
    assert "Calendar:" not in parser.await_args.kwargs["source"].text
    assert pending_counts(store) == (1, 2)

    writer = AsyncMock()
    with pytest.raises(PendingEventEditError, match="Confirm all event destinations"):
        await store.async_process_events(pending["id"], writer)
    writer.assert_not_awaited()
    assert pending_counts(store) == (1, 2)
    assert all(event.status == "pending" for event in store.get(pending["id"]).events)
