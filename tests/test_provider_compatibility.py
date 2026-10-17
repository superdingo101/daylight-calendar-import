"""AI Task compatibility through HA's real entity, chat and media resolution.

The development entity returns recorded corpus data: this proves the HA adapter
contract, not model extraction quality. Live evaluation remains an explicit gate.
"""

import json
from datetime import UTC, datetime
from pathlib import Path

import pytest
from homeassistant.components.ai_task import AITaskEntity, GenDataTaskResult
from homeassistant.components.ai_task.const import AITaskEntityFeature, DATA_COMPONENT
from homeassistant.setup import async_setup_component

from custom_components.daylight_calendar_import.parser import PROMPT_TEMPLATE, TASK_NAME
from custom_components.daylight_calendar_import.providers import AITaskParserProvider, SourceValidationError
from custom_components.daylight_calendar_import.sources import SourceAttachment, SourceDocument, SourceKind

CORPUS = Path(__file__).parent / "fixtures/extraction"
CASES = sorted(CORPUS.glob("*.json"))


class DevelopmentTask(AITaskEntity):
    """Recording development entity; no mocked HA generation function."""

    _attr_name = "Daylight development reference"
    _attr_supported_features = AITaskEntityFeature.GENERATE_DATA | AITaskEntityFeature.SUPPORT_ATTACHMENTS

    def __init__(self, response):
        self.response = response
        self.tasks = []

    async def _async_generate_data(self, task, chat_log):
        self.tasks.append(task)
        return GenDataTaskResult(conversation_id=chat_log.conversation_id, data=self.response)


@pytest.mark.parametrize("path", CASES, ids=lambda path: path.stem)
async def test_corpus_through_real_home_assistant_ai_task(hass, tmp_path, path):
    case = json.loads(path.read_text())
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    hass.config.media_dirs = {"local": str(media_dir)}
    assert await async_setup_component(hass, "media_source", {})
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "ai_task", {})
    entity = DevelopmentTask(case["provider_response"])
    await hass.data[DATA_COMPONENT].async_add_entities([entity])
    await hass.async_block_till_done()
    attachments = []
    for index, attachment in enumerate(case.get("attachments", [])):
        content = (CORPUS / attachment["path"]).read_bytes()
        name = f"fixture-{index}{Path(attachment['path']).suffix}"
        (media_dir / name).write_bytes(content)
        attachments.append(SourceAttachment(
            id=str(index), filename=name, media_type=attachment["media_type"],
            size_bytes=len(content), content_ref=f"media-source://media_source/local/{name}",
        ))
    text = case.get("parser_source_text", case["source_text"])
    source = SourceDocument(id=case["id"], received_at=datetime.now(UTC), kind=SourceKind(case.get("source_kind", "manual_text")), text=text, attachments=tuple(attachments))
    provider = AITaskParserProvider(hass, entity.entity_id)
    result = await provider.async_parse(source, reference_datetime=case["reference_datetime"], time_zone=case["time_zone"])
    assert [event.as_dict() for event in result.events] == case["expected"]["events"]
    assert result.warnings == case["expected"]["warnings"]
    assert [list(notes) for notes in result.event_assumptions] == case["expected"]["event_assumptions"]
    assert len(entity.tasks) == 1
    task = entity.tasks[0]
    assert task.name == TASK_NAME
    assert task.instructions == PROMPT_TEMPLATE.format(text=text.strip(), reference_datetime=case["reference_datetime"], time_zone=case["time_zone"])
    assert len(task.attachments or []) == len(attachments)
    for resolved, original in zip(task.attachments or [], attachments):
        assert resolved.mime_type == original.media_type
        assert resolved.path.read_bytes() == (media_dir / original.filename).read_bytes()
    # The schema is handed through HA to the real entity implementation.
    assert task.structure is not None


async def test_text_only_entity_rejects_media_without_calling_provider(hass):
    assert await async_setup_component(hass, "homeassistant", {})
    assert await async_setup_component(hass, "ai_task", {})
    entity = DevelopmentTask({"unsupported_recurrence": False, "events": []})
    entity._attr_supported_features = AITaskEntityFeature.GENERATE_DATA
    await hass.data[DATA_COMPONENT].async_add_entities([entity])
    provider = AITaskParserProvider(hass, entity.entity_id)
    assert provider.capabilities.text
    assert not provider.capabilities.images
    assert not provider.capabilities.pdfs
    for media_type in ("image/png", "application/pdf", "application/octet-stream"):
        source = SourceDocument(id="media", received_at=datetime.now(UTC), kind=SourceKind.IMAGE, text=None, attachments=(SourceAttachment(
            id="media", filename="input", media_type=media_type, size_bytes=1,
            content_ref="media-source://media_source/local/missing",
        ),))
        with pytest.raises(SourceValidationError) as caught:
            await provider.async_parse(source, reference_datetime="2026-10-15T12:00:00+00:00", time_zone="UTC")
        assert caught.value.code == ("unsupported_media" if media_type == "application/octet-stream" else "unsupported_capability")
    assert entity.tasks == []
