"""Tests for temporary MIME attachment staging from email sources."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from email.message import EmailMessage
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from custom_components.daylight_calendar_import import email_attachments
from custom_components.daylight_calendar_import.email_source import (
    DirectImapReference,
    EmailEnvelope,
    EmailProvenance,
    EmailSourceType,
)
from custom_components.daylight_calendar_import.providers import SourceValidationError
from custom_components.daylight_calendar_import.sources import (
    SourceAttachment,
    SourceDocument,
    SourceKind,
)


JPEG = b"\xff\xd8\xffemail jpeg"
PNG = b"\x89PNG\r\n\x1a\nemail png"
PDF = b"%PDF-1.7\nemail pdf"


class AttachmentHass:
    def __init__(self, media_dir: Path) -> None:
        self.config = SimpleNamespace(media_dirs={"local": str(media_dir)})

    async def async_add_executor_job(self, func, *args):
        return await asyncio.to_thread(func, *args)


def _raw_email(*, include_supported: bool = True) -> bytes:
    message = EmailMessage()
    message["Subject"] = "School schedule"
    message["Message-ID"] = "<attachments@example.test>"
    message.set_content("See the attached schedule.")
    message.add_alternative("<p>See the attached schedule.</p>", subtype="html")
    if include_supported:
        message.add_attachment(
            JPEG,
            maintype="image",
            subtype="jpeg",
            filename="flyer.jpg",
        )
        message.add_attachment(
            PDF,
            maintype="application",
            subtype="pdf",
            filename="schedule.pdf",
        )
        message.add_attachment(
            PNG,
            maintype="image",
            subtype="png",
            disposition="inline",
            cid="<inline@example.test>",
        )
    message.add_attachment(
        b"notes",
        maintype="text",
        subtype="plain",
        filename="notes.txt",
    )
    return message.as_bytes()


def _envelope(raw_message: bytes) -> EmailEnvelope:
    return EmailEnvelope(
        received_at=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
        raw_message=raw_message,
        provenance=EmailProvenance(
            source_id="primary-email",
            source_type=EmailSourceType.DIRECT_IMAP,
            transport_reference=DirectImapReference(
                mailbox="INBOX",
                uid_validity=10,
                uid=7,
            ),
        ),
    )


def _document() -> SourceDocument:
    return SourceDocument(
        id="document-1",
        kind=SourceKind.EMAIL,
        received_at=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
        text="See the attached schedule.",
        title="School schedule",
        upstream_source_id="<attachments@example.test>",
    )


async def test_supported_mime_attachments_stage_and_cleanup(tmp_path) -> None:
    media_dir = tmp_path / "media"
    envelope = _envelope(_raw_email())
    original = _document()

    async with email_attachments.async_email_attachments(
        AttachmentHass(media_dir),
        envelope,
        original,
    ) as staged:
        assert staged is not original
        assert staged.text == original.text
        assert staged.title == original.title
        assert staged.kind is SourceKind.EMAIL
        assert len(staged.attachments) == 3
        by_type = {item.media_type: item for item in staged.attachments}
        assert set(by_type) == {"image/jpeg", "image/png", "application/pdf"}
        assert by_type["image/jpeg"].filename == "flyer.jpg"
        assert by_type["application/pdf"].filename == "schedule.pdf"
        assert by_type["image/png"].filename is None
        assert by_type["image/jpeg"].size_bytes == len(JPEG)
        assert by_type["application/pdf"].sha256 == sha256(PDF).hexdigest()
        assert by_type["image/png"].sha256 == sha256(PNG).hexdigest()
        for attachment in staged.attachments:
            assert UUID(attachment.id)
            assert attachment.content_ref.startswith(
                "media-source://media_source/local/daylight-email-"
            )
        staged_paths = tuple(media_dir.iterdir())
        assert len(staged_paths) == 3
        assert {path.suffix for path in staged_paths} == {".jpg", ".png", ".pdf"}

    assert list(media_dir.iterdir()) == []


async def test_existing_document_attachments_are_preserved(tmp_path) -> None:
    existing = SourceAttachment(
        id="existing",
        media_type="image/png",
        size_bytes=3,
        content_ref="media-source://existing",
    )
    document = SourceDocument(
        id="document-1",
        kind=SourceKind.EMAIL,
        received_at=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
        text="body",
        attachments=(existing,),
    )
    envelope = _envelope(_raw_email())

    async with email_attachments.async_email_attachments(
        AttachmentHass(tmp_path),
        envelope,
        document,
    ) as staged:
        assert staged.attachments[0] is existing
        assert len(staged.attachments) == 4


async def test_email_without_supported_attachments_needs_no_media_directory(tmp_path) -> None:
    hass = AttachmentHass(tmp_path / "unused")
    hass.config.media_dirs = {}
    document = _document()

    async with email_attachments.async_email_attachments(
        hass,
        _envelope(_raw_email(include_supported=False)),
        document,
    ) as staged:
        assert staged is document
        assert staged.attachments == ()

    assert not (tmp_path / "unused").exists()


async def test_supported_attachment_requires_local_media_directory(tmp_path) -> None:
    hass = AttachmentHass(tmp_path / "unused")
    hass.config.media_dirs = {}

    with pytest.raises(SourceValidationError) as caught:
        async with email_attachments.async_email_attachments(
            hass,
            _envelope(_raw_email()),
            _document(),
        ):
            pytest.fail("missing media storage reached processor")

    assert caught.value.code == "media_storage_unavailable"
    assert str(caught.value) == "No local media directory configured"


async def test_processor_error_still_cleans_staged_attachments(tmp_path) -> None:
    media_dir = tmp_path / "media"
    with pytest.raises(RuntimeError, match="processor failed"):
        async with email_attachments.async_email_attachments(
            AttachmentHass(media_dir),
            _envelope(_raw_email()),
            _document(),
        ):
            assert len(list(media_dir.iterdir())) == 3
            raise RuntimeError("processor failed")

    assert list(media_dir.iterdir()) == []


async def test_cancellation_during_staging_cleans_late_result(tmp_path) -> None:
    media_dir = tmp_path / "media"
    stage_started = asyncio.Event()
    release_stage = asyncio.Event()
    cleanup_done = asyncio.Event()

    class DelayedHass(AttachmentHass):
        async def async_add_executor_job(self, func, *args):
            if func is email_attachments._stage_email_attachments:
                stage_started.set()
                await release_stage.wait()
            result = await asyncio.to_thread(func, *args)
            if func is not email_attachments._stage_email_attachments:
                cleanup_done.set()
            return result

    async def consume() -> None:
        async with email_attachments.async_email_attachments(
            DelayedHass(media_dir),
            _envelope(_raw_email()),
            _document(),
        ):
            pytest.fail("cancelled staging reached processor")

    task = asyncio.create_task(consume())
    await stage_started.wait()
    task.cancel()
    release_stage.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    await cleanup_done.wait()
    assert list(media_dir.iterdir()) == []


async def test_late_cleanup_consumes_staging_failure(tmp_path) -> None:
    staging = asyncio.get_running_loop().create_future()
    staging.set_exception(RuntimeError("staging failed"))

    await email_attachments._async_cleanup_late_staging(
        AttachmentHass(tmp_path),
        staging,
    )


def test_attachment_payload_rejects_non_bytes() -> None:
    part = EmailMessage()
    part.set_type("image/png")
    part.set_payload([EmailMessage()])

    with pytest.raises(
        email_attachments.EmailAttachmentError,
        match="payload could not be decoded",
    ):
        email_attachments._attachment_payload(part)


def test_staging_parse_failure_is_wrapped(monkeypatch, tmp_path) -> None:
    class BrokenParser:
        def parsebytes(self, _raw):
            raise ValueError("broken MIME")

    monkeypatch.setattr(
        email_attachments,
        "BytesParser",
        lambda **_kwargs: BrokenParser(),
    )

    with pytest.raises(
        email_attachments.EmailAttachmentError,
        match="attachments could not be parsed",
    ):
        email_attachments._stage_email_attachments(
            b"broken",
            {"local": str(tmp_path)},
        )


def test_partial_staging_failure_removes_earlier_files(monkeypatch, tmp_path) -> None:
    raw = _raw_email()
    real_named_temporary_file = email_attachments.tempfile.NamedTemporaryFile
    calls = 0

    class FailingFile:
        name = str(tmp_path / "failed.pdf")

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write(self, _data):
            raise OSError("storage full")

    def named_temporary_file(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return FailingFile()
        return real_named_temporary_file(**kwargs)

    monkeypatch.setattr(
        email_attachments.tempfile,
        "NamedTemporaryFile",
        named_temporary_file,
    )

    with pytest.raises(OSError, match="storage full"):
        email_attachments._stage_email_attachments(
            raw,
            {"local": str(tmp_path)},
        )

    assert list(tmp_path.iterdir()) == []


def test_attached_message_content_is_not_treated_as_parent_attachment(tmp_path) -> None:
    nested = EmailMessage()
    nested["Subject"] = "Forwarded"
    nested.set_content("Forwarded body")
    nested.add_attachment(
        PNG,
        maintype="image",
        subtype="png",
        filename="nested.png",
    )

    outer = EmailMessage()
    outer["Subject"] = "Outer"
    outer.set_content("Outer body")
    outer.add_attachment(nested)

    attachments, paths = email_attachments._stage_email_attachments(
        outer.as_bytes(),
        {"local": str(tmp_path)},
    )

    assert attachments == ()
    assert paths == ()
    assert list(tmp_path.iterdir()) == []


def test_close_failure_removes_current_and_earlier_staged_files(monkeypatch, tmp_path) -> None:
    raw = _raw_email()
    real_named_temporary_file = email_attachments.tempfile.NamedTemporaryFile
    calls = 0

    class CloseFailingFile:
        name = str(tmp_path / "close-failed.pdf")

        def __enter__(self):
            Path(self.name).write_bytes(b"partial")
            return self

        def __exit__(self, *_args):
            raise OSError("close failed")

        def write(self, data):
            Path(self.name).write_bytes(data)

    def named_temporary_file(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return CloseFailingFile()
        return real_named_temporary_file(**kwargs)

    monkeypatch.setattr(
        email_attachments.tempfile,
        "NamedTemporaryFile",
        named_temporary_file,
    )

    with pytest.raises(OSError, match="close failed"):
        email_attachments._stage_email_attachments(
            raw,
            {"local": str(tmp_path)},
        )

    assert list(tmp_path.iterdir()) == []


def test_cleanup_paths_attempts_all_files_before_raising(monkeypatch, tmp_path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    third = tmp_path / "third.png"
    for path in (first, second, third):
        path.write_bytes(b"data")

    original_unlink = Path.unlink
    attempted: list[Path] = []

    def unlink(path, missing_ok=False):
        attempted.append(path)
        if path == second:
            raise OSError("cleanup failed")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(OSError, match="cleanup failed"):
        email_attachments._cleanup_paths((first, second, third))

    assert attempted == [first, second, third]
    assert not first.exists()
    assert second.exists()
    assert not third.exists()


async def test_cancellation_during_cleanup_finishes_entire_batch(tmp_path) -> None:
    paths = tuple(tmp_path / f"{index}.png" for index in range(3))
    for path in paths:
        path.write_bytes(b"data")

    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    attempted: list[Path] = []

    class DelayedCleanupHass(AttachmentHass):
        async def async_add_executor_job(self, func, *args):
            if func is email_attachments._cleanup_paths:
                cleanup_started.set()
                await release_cleanup.wait()
            return await asyncio.to_thread(func, *args)

    task = asyncio.create_task(
        email_attachments._async_cleanup_paths(
            DelayedCleanupHass(tmp_path),
            paths,
        )
    )
    await cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert all(not path.exists() for path in paths)


async def test_cancellation_during_cleanup_preserves_cancel_over_cleanup_error(
    monkeypatch,
    tmp_path,
) -> None:
    paths = (tmp_path / "first.png", tmp_path / "second.png")
    for path in paths:
        path.write_bytes(b"data")

    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()
    original_cleanup = email_attachments._cleanup_paths

    def failing_cleanup(batch):
        original_cleanup((batch[0],))
        raise OSError("cleanup failed")

    class DelayedCleanupHass(AttachmentHass):
        async def async_add_executor_job(self, func, *args):
            if func is email_attachments._cleanup_paths:
                cleanup_started.set()
                await release_cleanup.wait()
                return await asyncio.to_thread(failing_cleanup, *args)
            return await asyncio.to_thread(func, *args)

    task = asyncio.create_task(
        email_attachments._async_cleanup_paths(
            DelayedCleanupHass(tmp_path),
            paths,
        )
    )
    await cleanup_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_late_cleanup_failure_does_not_replace_staging_cancellation(
    monkeypatch,
    tmp_path,
) -> None:
    media_dir = tmp_path / "media"
    stage_started = asyncio.Event()
    release_stage = asyncio.Event()

    class DelayedHass(AttachmentHass):
        async def async_add_executor_job(self, func, *args):
            if func is email_attachments._stage_email_attachments:
                stage_started.set()
                await release_stage.wait()
            return await asyncio.to_thread(func, *args)

    real_cleanup = email_attachments._cleanup_paths

    def failing_cleanup(paths):
        real_cleanup(paths)
        raise OSError("cleanup failed")

    monkeypatch.setattr(email_attachments, "_cleanup_paths", failing_cleanup)

    async def consume() -> None:
        async with email_attachments.async_email_attachments(
            DelayedHass(media_dir),
            _envelope(_raw_email()),
            _document(),
        ):
            pytest.fail("cancelled staging reached processor")

    task = asyncio.create_task(consume())
    await stage_started.wait()
    task.cancel()
    release_stage.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert list(media_dir.iterdir()) == []


def test_cleanup_paths_preserves_first_error_when_multiple_deletions_fail(
    monkeypatch,
    tmp_path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    third = tmp_path / "third.png"
    for path in (first, second, third):
        path.write_bytes(b"data")

    original_unlink = Path.unlink
    attempted: list[Path] = []

    def unlink(path, missing_ok=False):
        attempted.append(path)
        if path == first:
            raise OSError("first cleanup failure")
        if path == second:
            raise OSError("second cleanup failure")
        return original_unlink(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(OSError, match="first cleanup failure"):
        email_attachments._cleanup_paths((first, second, third))

    assert attempted == [first, second, third]
    assert first.exists()
    assert second.exists()
    assert not third.exists()


def test_partial_staging_cleanup_error_preserves_original_failure(
    monkeypatch,
    tmp_path,
) -> None:
    raw = _raw_email()
    real_named_temporary_file = email_attachments.tempfile.NamedTemporaryFile
    real_cleanup = email_attachments._cleanup_paths
    cleanup_batches: list[tuple[Path, ...]] = []
    calls = 0

    class FailingFile:
        name = str(tmp_path / "failed.pdf")

        def __enter__(self):
            Path(self.name).write_bytes(b"partial")
            return self

        def __exit__(self, *_args):
            return False

        def write(self, _data):
            raise OSError("storage full")

    def named_temporary_file(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 2:
            return FailingFile()
        return real_named_temporary_file(**kwargs)

    def failing_cleanup(paths):
        cleanup_batches.append(paths)
        real_cleanup(paths)
        raise OSError("cleanup also failed")

    monkeypatch.setattr(
        email_attachments.tempfile,
        "NamedTemporaryFile",
        named_temporary_file,
    )
    monkeypatch.setattr(email_attachments, "_cleanup_paths", failing_cleanup)

    with pytest.raises(OSError, match="storage full"):
        email_attachments._stage_email_attachments(
            raw,
            {"local": str(tmp_path)},
        )

    assert len(cleanup_batches) == 1
    assert len(cleanup_batches[0]) == 2
    assert list(tmp_path.iterdir()) == []


async def test_repeated_cancellation_while_waiting_for_late_staging_still_cleans(
    tmp_path,
) -> None:
    media_dir = tmp_path / "media"
    stage_started = asyncio.Event()
    release_stage = asyncio.Event()
    cleanup_started = asyncio.Event()
    release_cleanup = asyncio.Event()

    class DelayedHass(AttachmentHass):
        async def async_add_executor_job(self, func, *args):
            if func is email_attachments._stage_email_attachments:
                stage_started.set()
                await release_stage.wait()
            if func is email_attachments._cleanup_paths:
                cleanup_started.set()
                await release_cleanup.wait()
            return await asyncio.to_thread(func, *args)

    async def consume() -> None:
        async with email_attachments.async_email_attachments(
            DelayedHass(media_dir),
            _envelope(_raw_email()),
            _document(),
        ):
            pytest.fail("cancelled staging reached processor")

    task = asyncio.create_task(consume())
    await stage_started.wait()
    task.cancel()
    await asyncio.sleep(0)
    task.cancel()
    await asyncio.sleep(0)
    release_stage.set()
    await cleanup_started.wait()
    release_cleanup.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert list(media_dir.iterdir()) == []


async def test_body_failure_wins_over_cleanup_failure(monkeypatch, tmp_path) -> None:
    async def failing_cleanup(_hass, _paths):
        raise OSError("cleanup failed")

    monkeypatch.setattr(
        email_attachments,
        "_async_cleanup_paths",
        failing_cleanup,
    )

    with pytest.raises(RuntimeError, match="processor failed"):
        async with email_attachments.async_email_attachments(
            AttachmentHass(tmp_path),
            _envelope(_raw_email()),
            _document(),
        ):
            raise RuntimeError("processor failed")


async def test_body_cancellation_wins_over_cleanup_failure(
    monkeypatch,
    tmp_path,
) -> None:
    async def failing_cleanup(_hass, _paths):
        raise OSError("cleanup failed")

    monkeypatch.setattr(
        email_attachments,
        "_async_cleanup_paths",
        failing_cleanup,
    )

    with pytest.raises(asyncio.CancelledError):
        async with email_attachments.async_email_attachments(
            AttachmentHass(tmp_path),
            _envelope(_raw_email()),
            _document(),
        ):
            raise asyncio.CancelledError


def test_attachment_count_limit_is_enforced_before_decoding(monkeypatch, tmp_path) -> None:
    def fail_decode(_part):
        raise AssertionError("attachment payload should not be decoded")

    monkeypatch.setattr(email_attachments, "_attachment_payload", fail_decode)

    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            _raw_email(),
            {"local": str(tmp_path / "media")},
            (),
            2,
            10 * 1024 * 1024,
        )

    assert caught.value.code == "too_many_attachments"
    assert not (tmp_path / "media").exists()


def test_attachment_size_limit_is_enforced_before_decoding(monkeypatch, tmp_path) -> None:
    def fail_decode(_part):
        raise AssertionError("attachment payload should not be decoded")

    monkeypatch.setattr(email_attachments, "_attachment_payload", fail_decode)

    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            _raw_email(),
            {"local": str(tmp_path / "media")},
            (),
            4,
            1,
        )

    assert caught.value.code == "source_too_large"
    assert not (tmp_path / "media").exists()


def test_attachment_size_limit_counts_existing_attachments(tmp_path) -> None:
    existing = SourceAttachment(
        id="existing",
        media_type="image/png",
        size_bytes=9,
        content_ref="media-source://existing",
    )

    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            _raw_email(),
            {"local": str(tmp_path / "media")},
            (existing,),
            4,
            10,
        )

    assert caught.value.code == "source_too_large"
    assert not (tmp_path / "media").exists()


def test_decoded_payload_upper_bound_handles_non_base64_and_invalid_payload() -> None:
    plain = EmailMessage()
    plain.set_type("image/png")
    plain.set_payload("abcd")
    assert email_attachments._decoded_payload_upper_bound(plain) == 4

    invalid = EmailMessage()
    invalid.set_type("image/png")
    invalid.set_payload([EmailMessage()])
    with pytest.raises(
        email_attachments.EmailAttachmentError,
        match="could not be bounded",
    ):
        email_attachments._decoded_payload_upper_bound(invalid)


def test_cleanup_failure_reporting_includes_failed_paths(caplog, tmp_path) -> None:
    failed = tmp_path / "staged.png"
    error = email_attachments.EmailAttachmentCleanupError(
        (failed,),
        OSError("permission denied"),
    )

    email_attachments._report_cleanup_failure("processor failure", error)

    assert "processor failure" in caplog.text
    assert str(failed) in caplog.text


def test_generic_cleanup_failure_reporting_is_actionable(caplog) -> None:
    email_attachments._report_cleanup_failure(
        "staging failure",
        OSError("cleanup exploded"),
    )

    assert "staging failure" in caplog.text
    assert "cleanup exploded" in caplog.text


async def test_late_cleanup_cancellation_is_suppressed(tmp_path) -> None:
    staging = asyncio.get_running_loop().create_future()
    staging.set_result(((), (tmp_path / "staged.png",)))

    async def cancelled_cleanup(_hass, _paths):
        raise asyncio.CancelledError

    original = email_attachments._async_cleanup_paths
    email_attachments._async_cleanup_paths = cancelled_cleanup
    try:
        await email_attachments._async_cleanup_late_staging(
            AttachmentHass(tmp_path),
            staging,
        )
    finally:
        email_attachments._async_cleanup_paths = original


async def test_body_failure_wins_over_cleanup_cancellation(monkeypatch, tmp_path) -> None:
    async def cancelled_cleanup(_hass, _paths):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        email_attachments,
        "_async_cleanup_paths",
        cancelled_cleanup,
    )

    with pytest.raises(RuntimeError, match="processor failed"):
        async with email_attachments.async_email_attachments(
            AttachmentHass(tmp_path),
            _envelope(_raw_email()),
            _document(),
        ):
            raise RuntimeError("processor failed")


async def test_successful_body_propagates_cleanup_cancellation(monkeypatch, tmp_path) -> None:
    async def cancelled_cleanup(_hass, _paths):
        raise asyncio.CancelledError

    monkeypatch.setattr(
        email_attachments,
        "_async_cleanup_paths",
        cancelled_cleanup,
    )

    with pytest.raises(asyncio.CancelledError):
        async with email_attachments.async_email_attachments(
            AttachmentHass(tmp_path),
            _envelope(_raw_email()),
            _document(),
        ):
            pass


async def test_successful_body_reports_cleanup_failure(monkeypatch, caplog, tmp_path) -> None:
    async def failing_cleanup(_hass, _paths):
        raise OSError("cleanup failed")

    monkeypatch.setattr(
        email_attachments,
        "_async_cleanup_paths",
        failing_cleanup,
    )

    async with email_attachments.async_email_attachments(
        AttachmentHass(tmp_path),
        _envelope(_raw_email()),
        _document(),
    ):
        pass

    assert "successful processing" in caplog.text
    assert "cleanup failed" in caplog.text
