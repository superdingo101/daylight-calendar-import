"""Temporary staging for supported MIME attachments from email sources."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterable
import logging
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
from .providers import (
    AI_TASK_MAX_ATTACHMENTS,
    AI_TASK_MAX_TOTAL_BYTES,
    SourceValidationError,
)
from .sources import SourceAttachment, SourceDocument

_LOGGER = logging.getLogger(__name__)

_SUPPORTED_MEDIA_SUFFIXES = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
}


class EmailAttachmentError(ValueError):
    """Supported MIME attachments could not be staged safely."""


class EmailAttachmentCleanupError(OSError):
    """One or more staged email attachment files could not be removed."""

    def __init__(self, failed_paths: tuple[Path, ...]) -> None:
        super().__init__(
            f"Failed to remove {len(failed_paths)} staged email attachment file(s)"
        )
        self.failed_paths = failed_paths


def _report_cleanup_failure(context: str, error: Exception) -> None:
    """Emit an actionable cleanup error without masking the primary failure."""
    if isinstance(error, EmailAttachmentCleanupError):
        paths = ", ".join(str(path) for path in error.failed_paths)
        _LOGGER.error(
            "Failed to remove staged email attachment(s) after %s: %s",
            context,
            paths,
        )
        return
    _LOGGER.error(
        "Failed to clean staged email attachments after %s: %s",
        context,
        error,
    )


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


def _decoded_payload_upper_bound(part: Message) -> int:
    """Bound decoded bytes from the transfer-encoded payload without decoding it."""
    payload = part.get_payload(decode=False)
    if isinstance(payload, bytes):
        return len(payload)
    if not isinstance(payload, str):
        return AI_TASK_MAX_TOTAL_BYTES + 1
    if str(part.get("Content-Transfer-Encoding", "")).casefold() == "base64":
        encoded_chars = sum(1 for char in payload if not char.isspace())
        padding = 0
        for char in reversed(payload):
            if char.isspace():
                continue
            if char == "=" and padding < 2:
                padding += 1
                continue
            break
        return max(0, ((encoded_chars + 3) // 4) * 3 - padding)
    return len(payload)


def _stage_email_attachments(
    raw_message: bytes,
    media_dirs: dict[str, str],
    existing_attachments: tuple[SourceAttachment, ...] = (),
    max_attachments: int = AI_TASK_MAX_ATTACHMENTS,
    max_total_bytes: int = AI_TASK_MAX_TOTAL_BYTES,
) -> tuple[tuple[SourceAttachment, ...], tuple[Path, ...]]:
    """Stage supported email attachments under Home Assistant local media."""
    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
        parts: list[Message] = []
        total_bound = sum(item.size_bytes for item in existing_attachments)
        for part in _supported_leaf_parts(message):
            if len(existing_attachments) + len(parts) >= max_attachments:
                raise SourceValidationError(
                    "too_many_attachments",
                    "Too many source attachments",
                )
            part_bound = _decoded_payload_upper_bound(part)
            if total_bound + part_bound > max_total_bytes:
                raise SourceValidationError(
                    "source_too_large",
                    "Source attachments exceed the size limit",
                )
            total_bound += part_bound
            parts.append(part)
    except SourceValidationError:
        raise
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
    actual_total = sum(item.size_bytes for item in existing_attachments)

    try:
        for part in parts:
            media_type = part.get_content_type().casefold()
            data = _attachment_payload(part)
            actual_total += len(data)
            if actual_total > max_total_bytes:
                raise SourceValidationError(
                    "source_too_large",
                    "Source attachments exceed the size limit",
                )
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
        except Exception as cleanup_error:
            _report_cleanup_failure("staging failure", cleanup_error)
        raise

    return tuple(attachments), tuple(paths)


def _cleanup_paths(paths: tuple[Path, ...]) -> None:
    """Attempt every staged-file deletion, then report all paths that failed."""
    first_error: Exception | None = None
    failed_paths: list[Path] = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            failed_paths.append(path)
            if first_error is None:
                first_error = exc
    if failed_paths:
        raise EmailAttachmentCleanupError(tuple(failed_paths)) from first_error


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
        except Exception as cleanup_error:
            _report_cleanup_failure("cleanup cancellation", cleanup_error)
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
    except asyncio.CancelledError:
        pass
    except Exception as cleanup_error:
        _report_cleanup_failure("late staging cancellation", cleanup_error)


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
            document.attachments,
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
        except asyncio.CancelledError:
            pass
        except Exception as cleanup_error:
            _report_cleanup_failure("processor failure", cleanup_error)
        raise
    else:
        try:
            await _async_cleanup_paths(hass, paths)
        except asyncio.CancelledError:
            raise
        except Exception as cleanup_error:
            _report_cleanup_failure("successful processing", cleanup_error)
