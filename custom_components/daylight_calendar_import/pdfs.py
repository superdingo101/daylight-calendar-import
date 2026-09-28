"""Bounded PDF upload ingestion with text-layer extraction."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
import tempfile
import subprocess
import sys
import json
from uuid import uuid4

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import HomeAssistant

from .providers import SourceValidationError
from .sources import SourceAttachment, SourceDocument, SourceKind
from .uploads import MAX_UPLOAD_BYTES, _cleanup_late_staging

MAX_PDF_PAGES = 30
MAX_PDF_TEXT_CHARS = 100_000
PDF_WORKER_LIMIT_KEY = "daylight_calendar_import_pdf_worker_limit"


def _extract_pdf_text(data: bytes) -> tuple[str, bool]:
    """Run untrusted PDF parsing with memory and CPU limits in a child process."""
    try:
        result = subprocess.run(
            [sys.executable, str(Path(__file__).with_name("pdf_worker.py"))],
            input=data, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
            timeout=20, check=False,
        )
    except subprocess.TimeoutExpired as err:
        raise SourceValidationError("invalid_pdf", "PDF extraction timed out") from err
    if result.returncode:
        raise SourceValidationError("invalid_pdf", "PDF could not be read")
    try:
        payload = json.loads(result.stdout)
        if "error" not in payload:
            return payload["text"], payload["needs_attachment"]
        code, message = payload["error"], payload["message"]
    except (ValueError, KeyError, TypeError) as err:
        raise SourceValidationError("invalid_pdf", "PDF could not be read") from err
    raise SourceValidationError(code, message)


def _stage_pdf(
    hass: HomeAssistant, file_id: str, media_dirs: dict[str, str], context: str
) -> tuple[SourceDocument, Path | None]:
    """Consume an uploaded PDF; stage a temporary copy only for image fallback."""
    with process_uploaded_file(hass, file_id) as uploaded:
        with uploaded.open("rb") as handle:
            data = handle.read(MAX_UPLOAD_BYTES + 1)
        if not data:
            raise SourceValidationError("empty_attachment", "Source attachment is empty")
        if len(data) > MAX_UPLOAD_BYTES:
            raise SourceValidationError("source_too_large", "Source attachments exceed the size limit")
        filename = uploaded.name
        extracted, needs_attachment = _extract_pdf_text(data)

    digest = sha256(data).hexdigest()
    text = "\n\n".join(part for part in (context.strip(), extracted) if part) or None
    if not needs_attachment:
        return SourceDocument(
            id=str(uuid4()), kind=SourceKind.PDF, received_at=datetime.now(UTC),
            text=text, title=filename, metadata={"sha256": digest},
        ), None

    if not media_dirs:
        raise SourceValidationError("media_storage_unavailable", "No local media directory configured")
    media_alias, directory = next(iter(media_dirs.items()))
    media_path = Path(directory)
    media_path.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="daylight-", suffix=".pdf", dir=media_path, delete=False) as staged:
        try:
            staged.write(data)
        except BaseException:
            Path(staged.name).unlink(missing_ok=True)
            raise
    staged_path = Path(staged.name)
    return SourceDocument(
        id=str(uuid4()), kind=SourceKind.PDF, received_at=datetime.now(UTC), text=text,
        title=filename, attachments=(SourceAttachment(
            id=str(uuid4()), filename=filename, media_type="application/pdf",
            size_bytes=len(data), sha256=digest,
            content_ref=f"media-source://media_source/{media_alias}/{staged_path.name}",
        ),),
    ), staged_path


@asynccontextmanager
async def async_pdf_source(
    hass: HomeAssistant, file_id: str, context: str = ""
) -> AsyncIterator[SourceDocument]:
    """Expose a PDF source while ensuring the temporary copy is removed."""
    limit = hass.data.setdefault(PDF_WORKER_LIMIT_KEY, asyncio.Semaphore(1))
    async with limit:
        staging = asyncio.ensure_future(hass.async_add_executor_job(
            _stage_pdf, hass, file_id, hass.config.media_dirs, context
        ))
        try:
            source, path = await asyncio.shield(staging)
        except asyncio.CancelledError:
            await _cleanup_late_staging(hass, staging)
            raise
    try:
        yield source
    finally:
        if path is not None:
            await asyncio.shield(hass.async_add_executor_job(path.unlink, True))
