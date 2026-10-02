"""Bounded temporary staging for supported MIME attachments from email sources."""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from email import policy
from email.message import Message
from email.parser import BytesParser
from hashlib import sha256
import logging
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
EMAIL_STAGING_MAX_RAW_BYTES = (
    (AI_TASK_MAX_TOTAL_BYTES * 4 + 2) // 3
    + 1024 * 1024
)


class EmailAttachmentError(ValueError):
    """Supported MIME attachments could not be staged safely."""


class EmailAttachmentCleanupError(OSError):
    """One or more staged email attachment files could not be removed."""

    def __init__(
        self,
        failed_paths: tuple[Path, ...],
        first_error: Exception,
    ) -> None:
        super().__init__(
            f"{first_error}; failed staged email attachment path(s): "
            + ", ".join(str(path) for path in failed_paths)
        )
        self.failed_paths = failed_paths


def _report_cleanup_failure(context: str, error: Exception) -> None:
    """Emit an actionable cleanup error without masking the primary failure."""
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


def _cleanup_paths(paths: tuple[Path, ...]) -> None:
    """Attempt every staged-file deletion, then report all paths that failed."""
    failures: list[tuple[Path, Exception]] = []
    for path in paths:
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            failures.append((path, exc))
    if failures:
        failed_paths = tuple(path for path, _ in failures)
        first_error = failures[0][1]
        raise EmailAttachmentCleanupError(
            failed_paths,
            first_error,
        ) from first_error


async def _async_cleanup_paths(
    hass: HomeAssistant,
    paths: tuple[Path, ...],
    *,
    context: str,
) -> None:
    """Finish the cleanup batch before propagating cancellation."""
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
            _report_cleanup_failure(context, cleanup_error)
        raise asyncio.CancelledError

    cleanup.result()


@dataclass(frozen=True, slots=True)
class EmailAttachmentStage:
    """A staged email document and the temp files that back its media refs."""

    document: SourceDocument
    _hass: HomeAssistant = field(repr=False, compare=False)
    _paths: tuple[Path, ...] = field(repr=False, compare=False)

    async def async_cleanup(self, context: str) -> None:
        """Remove staged files; log ordinary cleanup failures."""
        try:
            await _async_cleanup_paths(
                self._hass,
                self._paths,
                context=context,
            )
        except asyncio.CancelledError:
            raise
        except Exception as cleanup_error:
            _report_cleanup_failure(context, cleanup_error)


def _stage_email_attachments(
    raw_message: bytes,
    media_dirs: dict[str, str],
    existing_attachments: tuple[SourceAttachment, ...] = (),
    max_attachments: int = AI_TASK_MAX_ATTACHMENTS,
    max_total_bytes: int = AI_TASK_MAX_TOTAL_BYTES,
    max_raw_bytes: int = EMAIL_STAGING_MAX_RAW_BYTES,
) -> tuple[tuple[SourceAttachment, ...], tuple[Path, ...]]:
    """Stage supported email attachments under Home Assistant local media."""
    if len(raw_message) > max_raw_bytes:
        raise SourceValidationError(
            "source_too_large",
            "Source attachments exceed the size limit",
        )

    existing_count = len(existing_attachments)
    if existing_count > max_attachments:
        raise SourceValidationError(
            "too_many_attachments",
            "Too many source attachments",
        )

    total_bytes = sum(item.size_bytes for item in existing_attachments)
    if total_bytes > max_total_bytes:
        raise SourceValidationError(
            "source_too_large",
            "Source attachments exceed the size limit",
        )

    try:
        message = BytesParser(policy=policy.default).parsebytes(raw_message)
        parts: list[Message] = []
        for part in _supported_leaf_parts(message):
            if existing_count + len(parts) >= max_attachments:
                raise SourceValidationError(
                    "too_many_attachments",
                    "Too many source attachments",
                )
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

    try:
        for part in parts:
            media_type = part.get_content_type().casefold()
            data = _attachment_payload(part)
            total_bytes += len(data)
            if total_bytes > max_total_bytes:
                raise SourceValidationError(
                    "source_too_large",
                    "Source attachments exceed the size limit",
                )

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
                    filename=part.get_filename(),
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


async def _async_wait_for_staging(
    staging: asyncio.Future,
) -> tuple[
    tuple[tuple[SourceAttachment, ...], tuple[Path, ...]],
    bool,
]:
    """Let synchronous staging reach a definite result before cancellation escapes."""
    cancelled = False
    while not staging.done():
        try:
            await asyncio.wait({staging})
        except asyncio.CancelledError:
            cancelled = True
            asyncio.current_task().uncancel()  # type: ignore[union-attr]

    if staging.cancelled():
        raise asyncio.CancelledError

    try:
        result = staging.result()
    except Exception:
        if cancelled:
            raise asyncio.CancelledError from None
        raise
    return result, cancelled


async def async_stage_email_attachments(
    hass: HomeAssistant,
    envelope: EmailEnvelope,
    document: SourceDocument,
) -> EmailAttachmentStage:
    """Stage supported MIME attachments and return explicit resource ownership."""
    staging = asyncio.ensure_future(
        hass.async_add_executor_job(
            _stage_email_attachments,
            envelope.raw_message,
            hass.config.media_dirs,
            document.attachments,
        )
    )
    (attachments, paths), cancelled = await _async_wait_for_staging(staging)

    staged_document = (
        replace(document, attachments=document.attachments + attachments)
        if attachments
        else document
    )
    stage = EmailAttachmentStage(
        document=staged_document,
        _hass=hass,
        _paths=paths,
    )
    if cancelled:
        try:
            await stage.async_cleanup("staging cancellation")
        except asyncio.CancelledError:
            pass
        raise asyncio.CancelledError
    return stage
