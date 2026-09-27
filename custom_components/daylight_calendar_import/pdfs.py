"""Bounded PDF upload ingestion with text-layer extraction."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from hashlib import sha256
from io import BytesIO
from pathlib import Path
import tempfile
from uuid import uuid4
import zlib

from homeassistant.components.file_upload import process_uploaded_file
from homeassistant.core import HomeAssistant
from pypdf import PdfReader
from pypdf.errors import PdfReadError

from .providers import SourceValidationError
from .sources import SourceAttachment, SourceDocument, SourceKind
from .uploads import MAX_UPLOAD_BYTES, _cleanup_late_staging

MAX_PDF_PAGES = 30
MAX_PDF_TEXT_CHARS = 100_000
MAX_PDF_PAGE_CONTENT_BYTES = 1_000_000


def _bounded_text_page(page: object) -> bool:
    """Avoid decoding a large or unsupported page stream in pypdf."""
    resources = page.get("/Resources") or {}
    if hasattr(resources, "get_object"):
        resources = resources.get_object()
    if resources.get("/XObject"):
        # Images and forms may carry event details alongside selectable text.
        return False
    contents = page.get("/Contents")
    if contents is None:
        return True
    resolved = contents.get_object()
    streams = resolved if isinstance(resolved, list) else [resolved]
    if len(streams) > 100:
        return False
    total = 0
    for item in streams:
        stream = item.get_object()
        raw = stream._data
        if len(raw) > MAX_PDF_PAGE_CONTENT_BYTES:
            return False
        encoding = stream.get("/Filter")
        if encoding is None:
            size = len(raw)
        elif encoding in ("/FlateDecode", "/Fl"):
            try:
                decoder = zlib.decompressobj()
                size = len(decoder.decompress(raw, MAX_PDF_PAGE_CONTENT_BYTES + 1))
                if decoder.unconsumed_tail or not decoder.eof:
                    return False
            except zlib.error:
                return False
        else:
            return False
        total += size
        if total > MAX_PDF_PAGE_CONTENT_BYTES:
            return False
    return True


def _extract_pdf_text(data: bytes) -> tuple[str, bool]:
    """Read a bounded PDF text layer without invoking image AI."""
    if not data.startswith(b"%PDF-"):
        raise SourceValidationError("unsupported_media", "Unsupported attachment media type")
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise SourceValidationError("unsupported_pdf", "Encrypted PDF is not supported")
        if not reader.pages:
            raise SourceValidationError("invalid_pdf", "PDF has no pages")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise SourceValidationError("too_many_pages", "PDF exceeds the page limit")
        pages: list[str] = []
        length = 0
        needs_attachment = False
        for page in reader.pages:
            safe = _bounded_text_page(page)
            text = (page.extract_text() or "") if safe else ""
            needs_attachment |= not text.strip()
            length += len(text) + (2 if pages else 0)
            if length > MAX_PDF_TEXT_CHARS:
                raise SourceValidationError("source_too_large", "PDF text exceeds the size limit")
            pages.append(text)
    except PdfReadError as err:
        raise SourceValidationError("invalid_pdf", "PDF could not be read") from err
    return "\n\n".join(pages).strip(), needs_attachment


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
