"""Diagnostics expose bounded aggregates without touching private source state."""

from dataclasses import replace
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from homeassistant.exceptions import HomeAssistantError
import pytest

from custom_components.daylight_calendar_import import diagnostics, providers
from custom_components.daylight_calendar_import.const import CONF_AI_TASK_ENTITY, DOMAIN
from custom_components.daylight_calendar_import.notification_journal import NotificationRecord
from custom_components.daylight_calendar_import.storage import PendingImport
from tests.test_storage import FakeStoreBackend, draft, make_store


def entry(data=None, options=None):
    return SimpleNamespace(entry_id="private entry id", data=data or {}, options=options or {})


async def test_real_runtime_snapshot_is_aggregate_only_and_read_only(monkeypatch):
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    private_draft = replace(draft(), title="private medical event", description="private patient data")
    pending = PendingImport.create(source_text="private source", events=[private_draft] * 3)
    pending = replace(pending, events=tuple(
        replace(event, status=status, calendar_entity="calendar.private", write_attempt="private attempt")
        for event, status in zip(pending.events, ("pending", "write_uncertain", "private status"), strict=True)
    ))
    store._items = {pending.id: pending}
    store._activity = (
        {"id": "private import", "status": "failed", "failure_code": "provider_timeout",
         "title": "private title", "guidance": "private provider response", "warnings": ["private"],
         "transitions": [{"type": "received", "at": "private time"}, {"type": "failed"}]},
        {"status": "review_ready", "transitions": [{"type": "source_reprocessed"}]},
        {"status": "private lifecycle", "failure_code": "private category", "transitions": "private steps"},
        {"status": ["private"], "failure_code": {"private": "code"},
         "transitions": [None, "private transition", {"type": ["private"]}]},
        "private malformed activity",
        None,
        {"transitions": None, "failure_code": None},
    )
    store._notifications = tuple(
        NotificationRecord("private import", "review_ready", "private time", "private event",
                           target="notify.private", status=status)
        for status in ("pending", "sent", "failed", "cancelled", "private status")
    )
    store.accepting_services = True
    component = SimpleNamespace(get_entity=Mock(return_value=SimpleNamespace(
        supported_features=providers.AITaskEntityFeature.SUPPORT_ATTACHMENTS,
    )))
    hass = SimpleNamespace(data={DOMAIN: {"private entry id": store}, providers.DATA_COMPONENT: component})
    configured = entry({CONF_AI_TASK_ENTITY: "ai_task.private_old", "password": "private secret"},
                       {CONF_AI_TASK_ENTITY: "ai_task.private_selected", "mailbox": "private mailbox"})
    generate = AsyncMock(side_effect=AssertionError("Diagnostics must not invoke AI"))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    previous_activity = store.list_activity()
    result = await diagnostics.async_get_config_entry_diagnostics(hass, configured)
    assert result["runtime"] == {"loaded": True, "accepting_services": True}
    assert result["queue"] == {"imports": 1, "events": 3,
                                "event_status": {"pending": 1, "write_uncertain": 1, "unknown": 1}}
    assert result["activity"]["records"] == 7
    assert result["activity"]["status"]["failed"] == 1
    assert result["activity"]["status"]["review_ready"] == 1
    assert result["activity"]["status"]["unknown"] == 5
    assert result["activity"]["transitions"]["received"] == 1
    assert result["activity"]["transitions"]["failed"] == 1
    assert result["activity"]["transitions"]["source_reprocessed"] == 1
    assert result["activity"]["transitions"]["unknown"] == 7
    assert result["provider_failures"]["provider_timeout"] == 1
    assert result["provider_failures"]["unknown"] == 2
    assert result["notifications"] == {
        "records": 5, "status": {"pending": 1, "sent": 1, "failed": 1, "cancelled": 1, "unknown": 1},
    }
    assert result["parser"] == {
        "configured": True, "capability_error": None,
        "capabilities": {"text": True, "images": True, "pdfs": True,
                         "max_attachments": 4, "max_total_bytes": 10 * 1024 * 1024},
    }
    assert "private" not in json.dumps(result)
    assert pending.id not in json.dumps(result)
    component.get_entity.assert_called_once_with("ai_task.private_selected")
    generate.assert_not_awaited()
    assert backend.save_attempts == 0
    assert store.list_activity() == previous_activity
    assert store.list() == (pending,)
    assert len(store.list_notifications()) == 5


@pytest.mark.parametrize("entity_id", [None, False, 42, "", " "])
async def test_unloaded_and_unconfigured_entry_has_safe_zero_snapshot(entity_id):
    result = await diagnostics.async_get_config_entry_diagnostics(
        SimpleNamespace(data={}), entry({CONF_AI_TASK_ENTITY: entity_id, "secret": "private"}),
    )
    assert result["runtime"] == {"loaded": False, "accepting_services": False}
    assert result["parser"] == {"configured": False, "capabilities": None, "capability_error": None}
    assert result["queue"]["imports"] == result["queue"]["events"] == 0
    assert result["activity"]["records"] == result["notifications"]["records"] == 0
    assert not any(result["activity"]["status"].values())
    assert not any(result["provider_failures"].values())
    assert "private" not in json.dumps(result)


async def test_unloaded_configured_entry_reports_declared_text_fallback():
    result = await diagnostics.async_get_config_entry_diagnostics(
        SimpleNamespace(data={}), entry({CONF_AI_TASK_ENTITY: "ai_task.private"}),
    )
    assert result["parser"]["configured"] is True
    assert result["parser"]["capabilities"]["text"] is True
    assert result["parser"]["capabilities"]["images"] is False
    assert result["parser"]["capabilities"]["pdfs"] is False
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize(("error", "expected"), [
    (HomeAssistantError("private provider capability error"), "provider_error"),
    (TimeoutError("private provider capability request"), "provider_timeout"),
])
async def test_real_capability_discovery_failure_is_redacted(error, expected):
    class Entity:
        @property
        def supported_features(self):
            raise error
    component = SimpleNamespace(get_entity=lambda _entity_id: Entity())
    result = await diagnostics.async_get_config_entry_diagnostics(
        SimpleNamespace(data={providers.DATA_COMPONENT: component}),
        entry({CONF_AI_TASK_ENTITY: "ai_task.private"}),
    )
    assert result["parser"] == {"configured": True, "capabilities": None, "capability_error": expected}
    assert "private" not in json.dumps(result)


@pytest.mark.parametrize("code", ["private unrecognized provider code", {"private": "unhashable code"}])
async def test_unrecognized_capability_error_category_cannot_escape(monkeypatch, code):
    class Provider:
        def __init__(self, _hass, _entity_id):
            pass
        @property
        def capabilities(self):
            raise providers.ProviderError(code, "private provider error")
    monkeypatch.setattr(diagnostics, "AITaskParserProvider", Provider)
    result = await diagnostics.async_get_config_entry_diagnostics(
        SimpleNamespace(data={}), entry(options={CONF_AI_TASK_ENTITY: "ai_task.private"}),
    )
    assert result["parser"]["capability_error"] == "provider_error"
    assert "private" not in json.dumps(result)


async def test_loaded_empty_runtime_can_be_paused(monkeypatch):
    store = make_store(monkeypatch, FakeStoreBackend())
    store.accepting_services = False
    result = await diagnostics.async_get_config_entry_diagnostics(
        SimpleNamespace(data={DOMAIN: {"private entry id": store}}), entry(),
    )
    assert result["runtime"] == {"loaded": True, "accepting_services": False}
    assert result["activity"]["records"] == 0
    assert result["notifications"]["records"] == 0
