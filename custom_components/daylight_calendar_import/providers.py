"""Parser provider boundary for normalized calendar sources."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from homeassistant.components import ai_task
from homeassistant.components.ai_task.const import AITaskEntityFeature, DATA_COMPONENT
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError

from .parser import EVENTS_STRUCTURE, PROMPT_TEMPLATE, TASK_NAME, ParseOutcome, parse_ai_data
from .sources import SourceDocument

IMAGE_MEDIA_TYPES = frozenset({"image/jpeg", "image/png", "image/webp"})
PDF_MEDIA_TYPE = "application/pdf"


class SourceValidationError(ValueError):
    """A source cannot be processed by the selected parser provider."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class ProviderError(HomeAssistantError):
    """A stable category for an upstream parser failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class ParserCapabilities:
    """Media types and bounded attachment sizes a provider can process."""

    text: bool
    images: bool
    pdfs: bool
    max_attachments: int
    max_total_bytes: int

    def validate(self, source: SourceDocument) -> None:
        """Reject unsupported or invalid inputs before invoking a provider."""
        attachments = source.attachments
        if len(attachments) > self.max_attachments:
            raise SourceValidationError("too_many_attachments", "Too many source attachments")
        total = 0
        for attachment in attachments:
            if attachment.size_bytes <= 0:
                raise SourceValidationError("empty_attachment", "Source attachment is empty")
            if attachment.media_type in IMAGE_MEDIA_TYPES:
                supported = self.images
            elif attachment.media_type == PDF_MEDIA_TYPE:
                supported = self.pdfs
            else:
                raise SourceValidationError("unsupported_media", "Unsupported attachment media type")
            if not supported:
                raise SourceValidationError("unsupported_capability", "Parser does not support this attachment")
            total += attachment.size_bytes
            if total > self.max_total_bytes:
                raise SourceValidationError("source_too_large", "Source attachments exceed the size limit")
        if source.text and source.text.strip():
            if not self.text:
                raise SourceValidationError("unsupported_capability", "Parser does not support text")
        elif not attachments:
            raise SourceValidationError("empty_source", "Source must contain text or attachments")


class ParserProvider(Protocol):
    """Convert one normalized source to candidate event drafts."""

    @property
    def capabilities(self) -> ParserCapabilities:
        """Return supported source kinds and attachment limits."""

    async def async_parse(
        self, source: SourceDocument, *, reference_datetime: str, time_zone: str
    ) -> ParseOutcome:
        """Return valid drafts and warnings about individual candidates."""


class AITaskParserProvider:
    """Use Home Assistant's configured AI Task entity to parse text sources."""

    def __init__(self, hass: HomeAssistant, entity_id: str) -> None:
        self._hass = hass
        self._entity_id = entity_id

    @property
    def capabilities(self) -> ParserCapabilities:
        """Discover whether the configured AI Task entity accepts attachments."""
        component = getattr(self._hass, "data", {}).get(DATA_COMPONENT)
        entity = component.get_entity(self._entity_id) if component else None
        images = bool(entity and entity.supported_features & AITaskEntityFeature.SUPPORT_ATTACHMENTS)
        return ParserCapabilities(text=True, images=images, pdfs=images, max_attachments=4, max_total_bytes=10 * 1024 * 1024)

    async def async_parse(
        self, source: SourceDocument, *, reference_datetime: str, time_zone: str
    ) -> ParseOutcome:
        """Ask AI Task for structured event candidates, then validate each one."""
        self.capabilities.validate(source)
        text = (source.text or "").strip()
        attachments = [
            {"media_content_id": item.content_ref, "media_content_type": item.media_type}
            for item in source.attachments
        ]

        try:
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
                **({"attachments": attachments} if attachments else {}),
            )
        except HomeAssistantError as err:
            raise ProviderError("provider_error", "AI Task could not parse the source") from err
        return parse_ai_data(result.data)
