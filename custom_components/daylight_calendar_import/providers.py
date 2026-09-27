"""Parser provider boundary for normalized calendar sources."""

from __future__ import annotations

from typing import Protocol

from homeassistant.components import ai_task
from homeassistant.core import HomeAssistant

from .parser import EVENTS_STRUCTURE, PROMPT_TEMPLATE, TASK_NAME, ParseOutcome, parse_ai_data
from .sources import SourceDocument


class ParserProvider(Protocol):
    """Convert one normalized source to candidate event drafts."""

    async def async_parse(
        self, source: SourceDocument, *, reference_datetime: str, time_zone: str
    ) -> ParseOutcome:
        """Return valid drafts and warnings about individual candidates."""


class AITaskParserProvider:
    """Use Home Assistant's configured AI Task entity to parse text sources."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self._hass = hass
        self._entity_id = entity_id

    async def async_parse(
        self, source: SourceDocument, *, reference_datetime: str, time_zone: str
    ) -> ParseOutcome:
        """Ask AI Task for structured event candidates, then validate each one."""
        text = (source.text or "").strip()
        if not text:
            raise ValueError("source must contain text")

        result = await ai_task.async_generate_data(
            self._hass,
            task_name=TASK_NAME,
            entity_id=self._entity_id,
            instructions=PROMPT_TEMPLATE.format(
                text=text,
                reference_datetime=reference_datetime,
                time_zone=time_zone,
            ),
            structure=EVENTS_STRUCTURE,
        )
        return parse_ai_data(result.data)
