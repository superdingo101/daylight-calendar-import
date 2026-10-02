"""Temporary staging for supported MIME attachments from email sources."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import replace
from email import policy
from email.message import Message
from email.parser import BytesParser
from hashlib import sha256
from pathlib import Path
import tempfile
from uuid import uuid4

from homeassistant.core import HomeAssistant

from .email_source import EmailEnvelope
from .providers import SourceValidationError
from .sources import SourceAttachment, SourceDocument

_SUPPORTED_MEDIA_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


class EmailAttachmentError(ValueError):
    """Supported MIME attachments could not be staged safely."""


def _supported_leaf_parts(part: Message) -> Iterable[Message]:
    """Yield supported leaf parts without descending into attached messages."""
    if part.get_content_maintype().casefold() == "message":
        return
    if part.is_multipart():
        for child in part.iter_parts():
            yield from _supported_leaf_parts(child)
        return
    if part.get_content_type().casefold() in _SUPPORTED_MEDIA_SUFFIXES:
        yield part


def _attachment_payload(part: Message) -> bytes:
    """Decode one MIME attachment payload without inventing missing bytes."""
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    raise EmailAttachmentError("Email attachment payload could not be decoded")


def _stage_email_attachments(
    raw_message: bytes,
    media_dirs: dict[str, str],
) -> tuple[tuple[SourceAttachment, ...], tuple[Path, ...]]:
    """Stage supported email attachments under Home Assistant local media."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
        parts = tuple(_supported_leaf_parts(message))
    except Exception as exc:
        raise EmailAttachmentError("Email attachments could not be parsed") from exc

    if not parts:
        return (), ()
    if not media_dirs:
        raise SourceValidationError(
            "media_storage_unavailable",
            "No local media directory configured",
        )

    media_alias, directory = next(iter(media_dirs.items()))
    media_path = Path(directory)
    media_path.mkdir(parents=True, exist_ok=True)
    attachments: list[SourceAttachment] = []
    paths: list[Path] = []

    try:
        for part in parts:
            media_type = part.get_content_type().casefold()
            data = _attachment_payload(part)
            filename = part.get_filename()
            staged_file = tempfile.NamedTemporaryFile(
                prefix="daylight-email-",
                suffix=_SUPPORTED_MEDIA_SUFFIXES[media_type],
                dir=media_path,
                delete=False,
            )
            staged_path = Path(staged_file.name)
            paths.append(staged_path)
            with staged_file as staged:
                staged.write(data)
            attachments.append(
                SourceAttachment(
                    id=str(uuid4()),
                    filename=filename,
                    media_type=media_type,
                    size_bytes=len(data),
                    content_ref=(
                        f"media-source://media_source/{media_alias}/"
                        f"{staged_path.name}"
                    ),
                    sha256=sha256(data).hexdigest(),
                )
            )
    except BaseException:
        try:
            _cleanup_paths(tuple(paths))
        except Exception:
            pass
        raise

    return tuple(attachments), tuple(paths)


def _cleanup_paths(paths: tuple[Path, ...]) -> None:
    """Attempt every staged-file deletion, then raise the first cleanup error."""
    first_error: Exception | None = None
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None:
        raise first_error


async def _async_cleanup_paths(
    hass: HomeAssistant,
    paths: tuple[Path, ...],
) -> None:
    """Finish the complete cleanup batch before propagating cancellation."""
    cleanup = asyncio.ensure_future(
        hass.async_add_executor_job(_cleanup_paths, paths)
    )
    cancelled = False
    while not cleanup.done():
        try:
            await asyncio.wait({cleanup})
        except asyncio.CancelledError:
            cancelled = True
            asyncio.current_task().uncancel()  # type: ignore[union-attr]

    if cancelled:
        try:
            cleanup.result()
        except Exception:
            pass
        raise asyncio.CancelledError

    cleanup.result()


async def _async_cleanup_late_staging(
    hass: HomeAssistant,
    staging: asyncio.Future,
) -> None:
    """Finish late staging/cleanup without replacing the original cancellation."""
    while not staging.done():
        try:
            await asyncio.wait({staging})
        except asyncio.CancelledError:
            asyncio.current_task().uncancel()  # type: ignore[union-attr]

    try:
        _, paths = staging.result()
    except BaseException:
        return
    try:
        await _async_cleanup_paths(hass, paths)
    except (asyncio.CancelledError, Exception):
        pass


@asynccontextmanager
async def async_email_attachments(
    hass: HomeAssistant,
    envelope: EmailEnvelope,
    document: SourceDocument,
) -> AsyncIterator[SourceDocument]:
    """Yield an email document with temporary image/PDF attachment media refs."""
    staging = asyncio.ensure_future(
        hass.async_add_executor_job(
            _stage_email_attachments,
            envelope.raw_message,
            hass.config.media_dirs,
        )
    )
    try:
        attachments, paths = await asyncio.shield(staging)
    except asyncio.CancelledError:
        await _async_cleanup_late_staging(hass, staging)
        raise

    staged_document = (
        replace(document, attachments=document.attachments + attachments)
        if attachments
        else document
    )
    try:
        yield staged_document
    except BaseException:
        try:
            await _async_cleanup_paths(hass, paths)
        except (asyncio.CancelledError, Exception):
            pass
        raise
    else:
        await _async_cleanup_paths(hass, paths)
