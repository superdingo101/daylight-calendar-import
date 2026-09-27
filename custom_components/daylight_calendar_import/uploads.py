"""Bounded, temporary Home Assistant file-upload ingestion."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import tempfile
from uuid import uuid4

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import HomeAssistant

from .providers import SourceValidationError
from .sources import SourceAttachment, SourceDocument, SourceKind

MAX_UPLOAD_BYTES = 10 * 1024 * 1024


def _image_type(data: bytes) -> tuple[str, str]:
    """Identify an image from its contents instead of trusting a filename."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png", ".png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg", ".jpg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "image/webp", ".webp"
    raise SourceValidationError("unsupported_media", "Unsupported attachment media type")


def _stage_image(
    hass: HomeAssistant, file_id: str, media_dirs: dict[str, str]
) -> tuple[SourceDocument, Path]:
    """Consume an uploaded file and stage a bounded copy under local media."""
    with process_uploaded_file(hass, file_id) as uploaded:
        with uploaded.open("rb") as handle:
            data = handle.read(MAX_UPLOAD_BYTES + 1)
        if not data:
            raise SourceValidationError("empty_attachment", "Source attachment is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise SourceValidationError("source_too_large", "Source attachments exceed the size limit")
        media_type, suffix = _image_type(data)
        filename = uploaded.name

    if not media_dirs:
        raise SourceValidationError("media_storage_unavailable", "No local media directory configured")
    media_alias, directory = next(iter(media_dirs.items()))
    media_path = Path(directory)
    media_path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="daylight-", suffix=suffix, dir=media_path, delete=False) as staged:
        try:
            staged.write(data)
        except BaseException:
            Path(staged.name).unlink(missing_ok=True)
            raise

    staged_path = Path(staged.name)
    source = SourceDocument(
        id=str(uuid4()),
        kind=SourceKind.IMAGE,
        received_at=datetime.now(UTC),
        attachments=(SourceAttachment(
            id=str(uuid4()), filename=filename, media_type=media_type,
            size_bytes=len(data),
            content_ref=f"media-source://media_source/{media_alias}/{staged_path.name}",
            sha256=sha256(data).hexdigest(),
        ),),
    )
    return source, staged_path


@asynccontextmanager
async def async_image_source(
    hass: HomeAssistant, file_id: str
) -> AsyncIterator[SourceDocument]:
    """Stage an upload only while a provider is using it, then remove it."""
    source, path = await hass.async_add_executor_job(
        _stage_image, hass, file_id, hass.config.media_dirs
    )
    try:
        yield source
    finally:
        await hass.async_add_executor_job(path.unlink, True)
