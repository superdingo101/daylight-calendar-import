"""Private upload spool ownership, integrity, and safe source replay."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import.retry_jobs import RetryJob
from custom_components.daylight_calendar_import.retry_spool import (
    RetryAttachmentError, async_delete_saved, async_reconcile_spool,
    async_replay_source, async_spool_upload,
)
from custom_components.daylight_calendar_import.sources import SourceAttachment, SourceDocument, SourceKind


@pytest.fixture
def spool_hass(tmp_path):
    config = SimpleNamespace(
        path=lambda name: str(tmp_path / name),
        media_dirs={"local": str(tmp_path / "media")},
    )
    return SimpleNamespace(config=config, async_add_executor_job=asyncio.to_thread)


def source_for_stage(path, kind=SourceKind.IMAGE, *, use_ai=True):
    data = path.read_bytes()
    attachment = SourceAttachment(
        id="a", filename=path.name, media_type="image/png" if kind is SourceKind.IMAGE else "application/pdf",
        size_bytes=len(data), content_ref="media-source://media_source/local/daylight-file",
        sha256=sha256(data).hexdigest(),
    )
    return SourceDocument(
        id="source", kind=kind, received_at=datetime(2026, 11, 18, tzinfo=UTC),
        text="Dinner from 6 to 8", title="daylight-upload.png",
        attachments=(attachment,) if use_ai else (),
        metadata={"retry_stage_path": str(path)},
    )


def job_for_spool(source, name, digest, size, *, use_ai=True):
    return RetryJob.create(
        activity_id="job", source_text=source.text or "", source_id="manual",
        received_at=source.received_at, provider_entity_id="ai_task.test",
        calendar_entity="calendar.family", owner_user_id="user",
        source_kind=source.kind.value, source_title="Original family flyer.png",
        parser_title=source.title, attachment_path=name,
        attachment_sha256=digest, attachment_size=size,
        attachment_media_type="image/png" if source.kind is SourceKind.IMAGE else "application/pdf",
        attachment_in_ai=use_ai,
    )


async def test_image_spool_is_private_and_replay_media_copy_is_temporary(spool_hass, tmp_path):
    path = tmp_path / "daylight-original.png"
    data = b"\x89PNG\r\n\x1a\nprivate flyer bytes"
    path.write_bytes(data)
    source = source_for_stage(path)
    name, digest, size = await async_spool_upload(spool_hass, source)
    stored = tmp_path / "daylight_calendar_import_retry" / name
    assert stored.read_bytes() == data
    assert stored.stat().st_mode & 0o777 == 0o600
    job = job_for_spool(source, name, digest, size)
    async with async_replay_source(spool_hass, job) as replayed:
        assert replayed.received_at == source.received_at
        assert replayed.title == source.title
        assert replayed.attachments[0].media_type == "image/png"
        assert replayed.attachments[0].sha256 == digest
        assert replayed.attachments[0].content_ref.startswith("media-source://media_source/local/daylight-retry-")
        assert list((tmp_path / "media").iterdir())
    assert list((tmp_path / "media").iterdir()) == []
    assert stored.exists()
    await async_delete_saved(spool_hass, name)
    assert not stored.exists()


async def test_pdf_with_text_only_keeps_original_bytes_without_resending_to_ai(spool_hass, tmp_path):
    path = tmp_path / "daylight-text.pdf"
    path.write_bytes(b"%PDF-1.7 private text-based flyer")
    source = source_for_stage(path, kind=SourceKind.PDF, use_ai=False)
    name, digest, size = await async_spool_upload(spool_hass, source)
    job = job_for_spool(source, name, digest, size, use_ai=False)
    async with async_replay_source(spool_hass, job) as replayed:
        assert replayed.kind is SourceKind.PDF
        assert replayed.attachments == ()
        assert replayed.text == source.text
    await async_delete_saved(spool_hass, name)


async def test_corrupt_or_missing_saved_attachment_fails_before_provider(spool_hass, tmp_path):
    path = tmp_path / "daylight-original.png"
    path.write_bytes(b"valid bytes")
    source = source_for_stage(path)
    name, digest, size = await async_spool_upload(spool_hass, source)
    job = job_for_spool(source, name, digest, size)
    stored = tmp_path / "daylight_calendar_import_retry" / name
    stored.write_bytes(b"corrupted!!")
    with pytest.raises(RetryAttachmentError, match="integrity"):
        async with async_replay_source(spool_hass, job):
            pass
    stored.unlink()
    with pytest.raises(RetryAttachmentError, match="missing"):
        async with async_replay_source(spool_hass, job):
            pass
    with pytest.raises(RetryAttachmentError, match="identifier"):
        await async_delete_saved(spool_hass, "../escape")


async def test_private_spool_reconciles_orphaned_files_but_preserves_active(spool_hass, tmp_path):
    path = tmp_path / "daylight-original.png"
    path.write_bytes(b"private")
    source = source_for_stage(path)
    name, digest, size = await async_spool_upload(spool_hass, source)
    job = job_for_spool(source, name, digest, size)
    orphan = tmp_path / "daylight_calendar_import_retry" / ("f" * 32 + ".bin")
    orphan.write_bytes(b"orphan")
    await async_reconcile_spool(spool_hass, (job,))
    assert not orphan.exists()
    assert (orphan.parent / name).exists()
    await async_reconcile_spool(spool_hass, ())
    assert not (orphan.parent / name).exists()


@pytest.mark.parametrize("content", [b"", b"1" * (10 * 1024 * 1024 + 1)])
async def test_spool_rejects_empty_or_oversized_bytes(spool_hass, tmp_path, content):
    path = tmp_path / "daylight-file.png"
    path.write_bytes(content)
    with pytest.raises(RetryAttachmentError, match="size limit"):
        await async_spool_upload(spool_hass, source_for_stage(path))


async def test_non_upload_or_symlink_path_cannot_be_retained(spool_hass, tmp_path):
    other = tmp_path / "other-source.png"
    other.write_bytes(b"private")
    with pytest.raises(RetryAttachmentError, match="Temporary"):
        await async_spool_upload(spool_hass, source_for_stage(other))
    link = tmp_path / "daylight-symlink"
    link.symlink_to(other)
    with pytest.raises(RetryAttachmentError, match="Temporary"):
        await async_spool_upload(spool_hass, source_for_stage(link))
