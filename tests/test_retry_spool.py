"""Private upload spool ownership, integrity, and safe source replay."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path
from threading import Event

import custom_components.daylight_calendar_import.retry_spool as spool_module
from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import.retry_jobs import RetryJob
from custom_components.daylight_calendar_import.retry_spool import (
    RetryAttachmentError, async_clear_spool, async_delete_saved, async_reconcile_spool,
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



async def test_cancelled_spool_copy_does_not_leave_a_late_private_file(
    spool_hass, tmp_path, monkeypatch,
):
    path = tmp_path / "daylight-cancelled.png"
    path.write_bytes(b"private")
    entered, release = Event(), Event()
    real_copy = spool_module._spool_copy

    def delayed_copy(hass, source_path):
        entered.set()
        assert release.wait(timeout=5)
        return real_copy(hass, source_path)

    monkeypatch.setattr(spool_module, "_spool_copy", delayed_copy)
    task = asyncio.create_task(async_spool_upload(spool_hass, source_for_stage(path)))
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    root = tmp_path / "daylight_calendar_import_retry"
    assert list(root.glob("*.bin")) == []


async def test_cancelled_replay_does_not_leave_media_visible_copy(
    spool_hass, tmp_path, monkeypatch,
):
    path = tmp_path / "daylight-original.png"
    path.write_bytes(b"private")
    source = source_for_stage(path)
    name, digest, size = await async_spool_upload(spool_hass, source)
    job = job_for_spool(source, name, digest, size)
    entered, release = Event(), Event()
    real_stage = spool_module._stage_for_ai

    def delayed_stage(hass, data, suffix):
        entered.set()
        assert release.wait(timeout=5)
        return real_stage(hass, data, suffix)

    monkeypatch.setattr(spool_module, "_stage_for_ai", delayed_stage)

    async def replay():
        async with async_replay_source(spool_hass, job):
            pytest.fail("Cancelled replay must not reach the provider")

    task = asyncio.create_task(replay())
    assert await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "media").glob("daylight-retry-*")) == []


async def test_reconcile_waits_for_admission_and_reads_live_job_snapshot(
    spool_hass, tmp_path,
):
    path = tmp_path / "daylight-race.png"
    path.write_bytes(b"private")
    source = source_for_stage(path)
    gate = asyncio.Lock()
    admitted, finish = asyncio.Event(), asyncio.Event()
    jobs = []

    async def admit():
        async with gate:
            name, digest, size = await async_spool_upload(spool_hass, source)
            admitted.set()
            await finish.wait()
            jobs.append(job_for_spool(source, name, digest, size))
            return name

    task = asyncio.create_task(admit())
    await admitted.wait()
    cleanup = asyncio.create_task(async_reconcile_spool(
        spool_hass, lambda: tuple(jobs), lock=gate,
    ))
    await asyncio.sleep(0)
    assert not cleanup.done()
    finish.set()
    name = await task
    await cleanup
    assert (tmp_path / "daylight_calendar_import_retry" / name).read_bytes() == b"private"


async def test_remove_private_spool_deletes_retained_uploads(spool_hass, tmp_path):
    path = tmp_path / "daylight-original.png"
    path.write_bytes(b"private")
    name, _, _ = await async_spool_upload(spool_hass, source_for_stage(path))
    root = tmp_path / "daylight_calendar_import_retry"
    assert (root / name).exists()
    await async_clear_spool(spool_hass)
    assert not root.exists()
    # A subsequent removal is idempotent and never deletes the upload original.
    await async_clear_spool(spool_hass)
    assert path.read_bytes() == b"private"


@pytest.mark.parametrize("scenario", ["private_root_symlink", "source_not_regular", "saved_not_regular"])
async def test_spool_rejects_untrusted_disk_shapes(
    scenario, spool_hass, tmp_path, monkeypatch,
):
    path = tmp_path / "daylight-upload.png"
    path.write_bytes(b"source")
    root = tmp_path / "daylight_calendar_import_retry"
    if scenario == "private_root_symlink":
        target = tmp_path / "outside"
        target.mkdir()
        root.symlink_to(target, target_is_directory=True)
        with pytest.raises(RetryAttachmentError, match="Unsafe private retry directory"):
            await async_spool_upload(spool_hass, source_for_stage(path))
        assert list(target.iterdir()) == []
        return

    if scenario == "source_not_regular":
        with monkeypatch.context() as patch:
            patch.setattr(spool_module.stat, "S_ISREG", lambda _mode: False)
            with pytest.raises(RetryAttachmentError, match="Temporary upload is unavailable"):
                spool_module._spool_copy(spool_hass, str(path))
        assert not root.exists()
        return

    source = source_for_stage(path)
    name, digest, size = await async_spool_upload(spool_hass, source)
    job = job_for_spool(source, name, digest, size)
    with monkeypatch.context() as patch:
        patch.setattr(spool_module.stat, "S_ISREG", lambda _mode: False)
        with pytest.raises(RetryAttachmentError, match="Saved attachment is missing"):
            spool_module._read_saved(spool_hass, job)
    assert (root / name).read_bytes() == b"source"


async def test_failed_spool_fsync_removes_partially_written_private_bytes(
    spool_hass, tmp_path, monkeypatch,
):
    path = tmp_path / "daylight-source.png"
    path.write_bytes(b"source bytes")
    with monkeypatch.context() as patch:
        patch.setattr(spool_module.os, "fsync", lambda _fd: (_ for _ in ()).throw(OSError("disk failure")))
        with pytest.raises(OSError, match="disk failure"):
            await async_spool_upload(spool_hass, source_for_stage(path))
    root = tmp_path / "daylight_calendar_import_retry"
    assert root.is_dir()
    assert list(root.iterdir()) == []
    assert path.read_bytes() == b"source bytes"


async def test_spool_requires_a_private_staging_path(spool_hass, tmp_path):
    path = tmp_path / "daylight-source.png"
    path.write_bytes(b"source bytes")
    source = replace(source_for_stage(path), metadata={})
    with pytest.raises(RetryAttachmentError, match="not available for a durable retry"):
        await async_spool_upload(spool_hass, source)
    assert not (tmp_path / "daylight_calendar_import_retry").exists()


async def test_cancelled_spool_executor_failure_is_consumed(
    spool_hass, tmp_path,
):
    """Deliver the executor error only after cancellation owns the future."""
    path = tmp_path / "daylight-source.png"
    path.write_bytes(b"source bytes")
    pending = asyncio.get_running_loop().create_future()
    spool_hass.async_add_executor_job = lambda *_args: pending
    task = asyncio.create_task(async_spool_upload(spool_hass, source_for_stage(path)))
    await asyncio.sleep(0)
    assert not task.done()
    task.cancel()
    # Force the cancelled coroutine to attach its exception-consuming await
    # before the controlled executor fails; never race teardown.
    await asyncio.sleep(0)
    assert not task.done()
    pending.set_exception(RetryAttachmentError("source vanished"))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert pending.done()
    assert not (tmp_path / "daylight_calendar_import_retry").exists()


async def test_replay_requires_configured_media_for_visual_bytes(
    spool_hass, tmp_path,
):
    path = tmp_path / "daylight-source.png"
    path.write_bytes(b"private bytes")
    source = source_for_stage(path)
    name, digest, size = await async_spool_upload(spool_hass, source)
    spool_hass.config.media_dirs = {}
    with pytest.raises(RetryAttachmentError, match="No local media directory"):
        async with async_replay_source(spool_hass, job_for_spool(source, name, digest, size)):
            pytest.fail("AI must not receive a local source without configured media")
    assert (tmp_path / "daylight_calendar_import_retry" / name).exists()


def test_failed_media_write_cleans_the_temporary_ai_file(
    spool_hass, tmp_path, monkeypatch,
):
    original = spool_module.tempfile.NamedTemporaryFile
    names = []

    class FailedWrite:
        def __init__(self, *args, **kwargs):
            self._handle = original(*args, **kwargs)
            self.name = self._handle.name
            names.append(Path(self.name))

        def __enter__(self):
            self._handle.__enter__()
            return self

        def __exit__(self, *args):
            return self._handle.__exit__(*args)

        def write(self, _data):
            raise OSError("media write failed")

    with monkeypatch.context() as patch:
        patch.setattr(spool_module.tempfile, "NamedTemporaryFile", FailedWrite)
        with pytest.raises(OSError, match="media write failed"):
            spool_module._stage_for_ai(spool_hass, b"private", ".png")
    assert len(names) == 1
    assert not names[0].exists()


async def test_cancelled_media_stager_failure_is_consumed(
    spool_hass, tmp_path,
):
    """A stage failure after cancellation is observed and leaves no media."""
    path = tmp_path / "daylight-source.png"
    path.write_bytes(b"private")
    source = source_for_stage(path)
    name, digest, size = await async_spool_upload(spool_hass, source)
    pending = asyncio.get_running_loop().create_future()
    staging_started = asyncio.Event()
    original_executor = spool_hass.async_add_executor_job

    def controlled_executor(func, *args):
        if func is spool_module._stage_for_ai:
            staging_started.set()
            return pending
        return original_executor(func, *args)

    spool_hass.async_add_executor_job = controlled_executor

    async def replay():
        async with async_replay_source(spool_hass, job_for_spool(source, name, digest, size)):
            pytest.fail("the cancelled stage must not reach the provider")

    task = asyncio.create_task(replay())
    await asyncio.wait_for(staging_started.wait(), 5)
    task.cancel()
    await asyncio.sleep(0)
    assert not task.done()
    pending.set_exception(RetryAttachmentError("cannot stage"))
    with pytest.raises(asyncio.CancelledError):
        await task
    assert list((tmp_path / "media").glob("daylight-retry-*")) == []


@pytest.mark.parametrize(("kind", "media_type", "suffix"), [
    (SourceKind.IMAGE, "image/png", ".png"),
    (SourceKind.IMAGE, "image/webp", ".webp"),
    (SourceKind.IMAGE, "image/jpeg", ".jpg"),
    (SourceKind.PDF, "application/pdf", ".pdf"),
])
async def test_replay_media_suffix_is_derived_only_from_authenticated_type(
    spool_hass, tmp_path, kind, media_type, suffix,
):
    path = tmp_path / "daylight-bytes.bin"
    path.write_bytes(b"private document bytes")
    source = source_for_stage(path, kind=kind)
    name, digest, size = await async_spool_upload(spool_hass, source)
    job = replace(job_for_spool(source, name, digest, size), attachment_media_type=media_type)
    async with async_replay_source(spool_hass, job) as replayed:
        assert replayed.attachments[0].content_ref.endswith(suffix)
        staged = list((tmp_path / "media").iterdir())
        assert len(staged) == 1
        assert staged[0].suffix == suffix
        assert staged[0].read_bytes() == b"private document bytes"
    assert list((tmp_path / "media").iterdir()) == []


def test_reconcile_missing_and_symlinked_private_roots(spool_hass, tmp_path):
    root = tmp_path / "daylight_calendar_import_retry"
    spool_module._reconcile(spool_hass, frozenset())
    assert not root.exists()
    outside = tmp_path / "outside"
    outside.mkdir()
    root.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RetryAttachmentError, match="Unsafe private retry directory"):
        spool_module._reconcile(spool_hass, frozenset())
    assert list(outside.iterdir()) == []


async def test_reconcile_skips_unowned_names_and_unlinks_only_spool_symlinks(
    spool_hass, tmp_path,
):
    root = tmp_path / "daylight_calendar_import_retry"
    root.mkdir()
    target = tmp_path / "outside-source.txt"
    target.write_bytes(b"outside untouched")
    link = root / ("b" * 32 + ".bin")
    link.symlink_to(target)
    unrelated = root / "user-notes.txt"
    unrelated.write_bytes(b"unrelated")
    await async_reconcile_spool(spool_hass, ())
    assert not link.exists() and not link.is_symlink()
    assert target.read_bytes() == b"outside untouched"
    assert unrelated.read_bytes() == b"unrelated"


@pytest.mark.parametrize("scenario", ["root_symlink", "unrelated_file", "valid_name_directory"])
async def test_clear_private_spool_never_traverses_other_content(
    spool_hass, tmp_path, scenario,
):
    root = tmp_path / "daylight_calendar_import_retry"
    outside = tmp_path / "outside"
    outside.mkdir()
    sentinel = outside / "keep.txt"
    sentinel.write_bytes(b"preserve")
    if scenario == "root_symlink":
        root.symlink_to(outside, target_is_directory=True)
        with pytest.raises(RetryAttachmentError, match="Unsafe private retry directory"):
            await async_clear_spool(spool_hass)
        assert root.is_symlink()
    else:
        root.mkdir()
        child = root / ("b" * 32 + ".bin" if scenario == "valid_name_directory" else "notes.txt")
        if scenario == "valid_name_directory":
            child.mkdir()
        else:
            child.write_bytes(b"not an opaque spool entry")
        await async_clear_spool(spool_hass)
        assert root.exists()
        assert child.exists()
    assert sentinel.read_bytes() == b"preserve"


@pytest.mark.parametrize("bytes_written", [b"", b"bad", b"x" * (10 * 1024 * 1024 + 1)])
async def test_replay_detects_empty_wrong_size_or_oversized_private_data(
    spool_hass, tmp_path, bytes_written,
):
    path = tmp_path / "daylight-source.png"
    path.write_bytes(b"original bytes")
    source = source_for_stage(path)
    name, digest, size = await async_spool_upload(spool_hass, source)
    stored = tmp_path / "daylight_calendar_import_retry" / name
    stored.write_bytes(bytes_written)
    with pytest.raises(RetryAttachmentError, match="integrity"):
        async with async_replay_source(spool_hass, job_for_spool(source, name, digest, size)):
            pytest.fail("corrupt bytes must not reach the provider")
