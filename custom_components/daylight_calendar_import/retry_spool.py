"""Private, bounded source-file spool for durable image and PDF retries.

Only opaque, randomly generated identifiers are stored; exposed media copies
exist for an AI request only and are always removed on context exit.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import datetime
from hashlib import sha256
import os
from pathlib import Path
import re
import stat
import tempfile
from uuid import uuid4

from homeassistant.core import HomeAssistant

from .sources import SourceAttachment, SourceDocument, SourceKind
from .retry_jobs import RetryJob

_MAX_BYTES = 10 * 1024 * 1024
_REFERENCE = re.compile(r"^[0-9a-f]{32}\.bin$")


class RetryAttachmentError(ValueError):
    """Saved attachment is invalid, missing, or cannot be safely replayed."""


def _root(hass: HomeAssistant) -> Path:
    return Path(hass.config.path("daylight_calendar_import_retry"))


def _private_root(hass: HomeAssistant) -> Path:
    root = _root(hass)
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.is_symlink():
        raise RetryAttachmentError("Unsafe private retry directory")
    os.chmod(root, 0o700)
    return root


def _spool_copy(hass: HomeAssistant, source_path: str) -> tuple[str, str, int]:
    path = Path(source_path)
    if not path.name.startswith("daylight-"):
        raise RetryAttachmentError("Temporary upload is unavailable")
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as source:
            if not stat.S_ISREG(os.fstat(source.fileno()).st_mode):
                raise RetryAttachmentError("Temporary upload is unavailable")
            data = source.read(_MAX_BYTES + 1)
    except (FileNotFoundError, IsADirectoryError, OSError) as err:
        raise RetryAttachmentError("Temporary upload is unavailable") from err
    if not data or len(data) > _MAX_BYTES:
        raise RetryAttachmentError("Saved upload exceeds the retry size limit")
    digest = sha256(data).hexdigest()
    root = _private_root(hass)
    name = f"{uuid4().hex}.bin"
    destination = root / name
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        destination.unlink(missing_ok=True)
        raise
    return name, digest, len(data)


async def async_spool_upload(hass: HomeAssistant, source: SourceDocument) -> tuple[str, str, int]:
    """Capture original bytes before the upload's temporary context closes."""
    path = source.metadata.get("retry_stage_path")
    if not isinstance(path, str):
        raise RetryAttachmentError("Uploaded file is not available for a durable retry")
    # Executor work is not cancelled with its awaiting coroutine.  Retire
    # a late file before propagating cancellation to the caller.
    copying = asyncio.ensure_future(hass.async_add_executor_job(_spool_copy, hass, path))
    try:
        return await asyncio.shield(copying)
    except asyncio.CancelledError:
        try:
            name, _, _ = await copying
        except Exception:
            pass
        else:
            await asyncio.shield(async_delete_saved(hass, name))
        raise


def _saved_path(hass: HomeAssistant, name: str) -> Path:
    if not isinstance(name, str) or not _REFERENCE.fullmatch(name):
        raise RetryAttachmentError("Saved attachment identifier is invalid")
    return _root(hass) / name


def _read_saved(hass: HomeAssistant, job: RetryJob) -> bytes:
    path = _saved_path(hass, job.attachment_path)
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
                raise RetryAttachmentError("Saved attachment is missing")
            data = handle.read(_MAX_BYTES + 1)
    except (FileNotFoundError, IsADirectoryError, OSError) as err:
        raise RetryAttachmentError("Saved attachment is missing") from err
    if (not data or len(data) > _MAX_BYTES or len(data) != job.attachment_size
            or sha256(data).hexdigest() != job.attachment_sha256):
        raise RetryAttachmentError("Saved attachment failed integrity verification")
    return data


async def async_delete_saved(hass: HomeAssistant, name: str | None) -> None:
    if name is None:
        return
    path = _saved_path(hass, name)
    await hass.async_add_executor_job(path.unlink, True)


def _stage_for_ai(hass: HomeAssistant, data: bytes, suffix: str) -> tuple[str, str]:
    if not hass.config.media_dirs:
        raise RetryAttachmentError("No local media directory is configured")
    alias, directory = next(iter(hass.config.media_dirs.items()))
    media_dir = Path(directory)
    media_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix="daylight-retry-", suffix=suffix,
                                     dir=media_dir, delete=False) as handle:
        try:
            handle.write(data)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
    return alias, handle.name


@asynccontextmanager
async def async_replay_source(
    hass: HomeAssistant, job: RetryJob,
) -> AsyncIterator[SourceDocument]:
    """Reconstruct exactly the original parser evidence for a saved source."""
    kind = SourceKind(job.source_kind)
    received_at = datetime.fromisoformat(job.received_at)
    attachments: tuple[SourceAttachment, ...] = ()
    staged_path: str | None = None
    if job.attachment_path is not None:
        data = await hass.async_add_executor_job(_read_saved, hass, job)
        if job.attachment_in_ai:
            suffix = ".pdf" if kind is SourceKind.PDF else (
                ".png" if job.attachment_media_type == "image/png" else
                ".webp" if job.attachment_media_type == "image/webp" else ".jpg"
            )
            staging = asyncio.ensure_future(
                hass.async_add_executor_job(_stage_for_ai, hass, data, suffix)
            )
            try:
                alias, staged_path = await asyncio.shield(staging)
            except asyncio.CancelledError:
                # A cancelled await cannot stop an executor thread. Consume
                # the late path and unlink its media-visible copy.
                try:
                    _, late_path = await staging
                except Exception:
                    pass
                else:
                    await asyncio.shield(
                        hass.async_add_executor_job(Path(late_path).unlink, True)
                    )
                raise
            attachments = (SourceAttachment(
                id=str(uuid4()), media_type=job.attachment_media_type,
                size_bytes=len(data), content_ref=(
                    f"media-source://media_source/{alias}/{Path(staged_path).name}"
                ), filename=job.source_title, sha256=job.attachment_sha256,
            ),)
    try:
        yield SourceDocument(
            id=job.id, kind=kind, received_at=received_at,
            text=job.source_text or None, title=job.parser_title,
            upstream_source_id=job.source_id, attachments=attachments,
        )
    finally:
        if staged_path is not None:
            await asyncio.shield(hass.async_add_executor_job(Path(staged_path).unlink, True))


def _reconcile(hass: HomeAssistant, referenced: frozenset[str]) -> None:
    root = _root(hass)
    if not root.exists():
        return
    if root.is_symlink():
        raise RetryAttachmentError("Unsafe private retry directory")
    for path in root.iterdir():
        if not _REFERENCE.fullmatch(path.name):
            continue
        if path.is_symlink():
            path.unlink(missing_ok=True)
        elif path.name not in referenced and path.is_file():
            path.unlink(missing_ok=True)


async def async_reconcile_spool(
    hass: HomeAssistant,
    jobs: tuple[RetryJob, ...] | Callable[[], tuple[RetryJob, ...]],
    *, lock: asyncio.Lock | None = None,
) -> None:
    """Reconcile against a live snapshot, serialized with source admission."""
    async def run() -> None:
        # No disk operation is required when no attachment spool exists.
        # This also keeps recovery of text-only installs inexpensive.
        if not _root(hass).exists():
            return
        current = jobs() if callable(jobs) else jobs
        references = frozenset(
            job.attachment_path for job in current if job.attachment_path
        )
        await hass.async_add_executor_job(_reconcile, hass, references)

    if lock is None:
        await run()
    else:
        async with lock:
            await run()


def _clear_spool(hass: HomeAssistant) -> None:
    """Erase owned private uploads on integration removal, never follow links."""
    root = _root(hass)
    if root.is_symlink():
        raise RetryAttachmentError("Unsafe private retry directory")
    if not root.exists():
        return
    for path in root.iterdir():
        if _REFERENCE.fullmatch(path.name) and (path.is_file() or path.is_symlink()):
            path.unlink(missing_ok=True)
    if not any(root.iterdir()):
        root.rmdir()


async def async_clear_spool(hass: HomeAssistant) -> None:
    """Remove retained attachments after all source handlers have retired."""
    await hass.async_add_executor_job(_clear_spool, hass)
