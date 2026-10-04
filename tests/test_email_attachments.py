"""Tests for bounded temporary MIME attachment staging."""

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


def _raw_email(*, supported: bool = True) -> bytes:
    message = EmailMessage()
    message["Subject"] = "School schedule"
    message["Message-ID"] = "<attachments@example.test>"
    message.set_content("See the attached schedule.")
    message.add_alternative("<p>See the attached schedule.</p>", subtype="html")
    if supported:
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


def _document(
    attachments: tuple[SourceAttachment, ...] = (),
) -> SourceDocument:
    return SourceDocument(
        id="document-1",
        kind=SourceKind.EMAIL,
        received_at=datetime(2026, 10, 2, 12, 0, tzinfo=UTC),
        text="See the attached schedule.",
        title="School schedule",
        attachments=attachments,
        upstream_source_id="<attachments@example.test>",
    )


def test_stage_supported_parts_and_cleanup(tmp_path) -> None:
    attachments, paths = email_attachments._stage_email_attachments(
        _raw_email(),
        {"local": str(tmp_path)},
    )

    assert len(attachments) == 3
    by_type = {item.media_type: item for item in attachments}
    assert set(by_type) == {"image/jpeg", "image/png", "application/pdf"}
    assert by_type["image/jpeg"].filename == "flyer.jpg"
    assert by_type["application/pdf"].filename == "schedule.pdf"
    assert by_type["image/png"].filename is None
    assert by_type["image/jpeg"].size_bytes == len(JPEG)
    assert by_type["application/pdf"].sha256 == sha256(PDF).hexdigest()
    assert by_type["image/png"].sha256 == sha256(PNG).hexdigest()
    assert len(paths) == 3
    for attachment, path in zip(attachments, paths, strict=True):
        assert UUID(attachment.id)
        assert attachment.content_ref == (
            f"media-source://media_source/local/{path.name}"
        )
        assert path.exists()

    email_attachments._cleanup_paths(paths)
    assert list(tmp_path.iterdir()) == []


def test_unsupported_and_attached_message_parts_are_ignored(tmp_path) -> None:
    nested = EmailMessage()
    nested.set_content("Forwarded")
    nested.add_attachment(
        PNG,
        maintype="image",
        subtype="png",
        filename="nested.png",
    )
    outer = EmailMessage()
    outer.set_content("Outer")
    outer.add_attachment(nested)
    outer.add_attachment(
        b"notes",
        maintype="text",
        subtype="plain",
        filename="notes.txt",
    )

    attachments, paths = email_attachments._stage_email_attachments(
        outer.as_bytes(),
        {"local": str(tmp_path)},
    )

    assert attachments == ()
    assert paths == ()
    assert not tmp_path.exists() or list(tmp_path.iterdir()) == []


def test_attachment_payload_requires_bytes() -> None:
    part = EmailMessage()
    part.set_type("image/png")
    part.set_payload([EmailMessage()])

    with pytest.raises(
        email_attachments.EmailAttachmentError,
        match="could not be decoded",
    ):
        email_attachments._attachment_payload(part)


@pytest.mark.parametrize(
    ("kwargs", "code"),
    [
        ({"max_raw_bytes": 1}, "source_too_large"),
        ({"existing_attachments": tuple(
            SourceAttachment(
                id=str(index),
                media_type="image/png",
                size_bytes=1,
                content_ref=f"media-source://{index}",
            )
            for index in range(5)
        )}, "too_many_attachments"),
        ({"existing_attachments": (
            SourceAttachment(
                id="existing",
                media_type="image/png",
                size_bytes=11,
                content_ref="media-source://existing",
            ),
        ), "max_total_bytes": 10}, "source_too_large"),
        ({"max_attachments": 2}, "too_many_attachments"),
    ],
)
def test_staging_rejects_bounds_before_writing(tmp_path, kwargs, code) -> None:
    media_dir = tmp_path / "media"
    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            _raw_email(),
            {"local": str(media_dir)},
            **kwargs,
        )

    assert caught.value.code == code
    assert not media_dir.exists()


def test_decoded_total_limit_is_checked_before_current_file_write(
    monkeypatch,
    tmp_path,
) -> None:
    created = 0

    def fail_tempfile(**_kwargs):
        nonlocal created
        created += 1
        raise AssertionError("oversized decoded payload reached tempfile creation")

    monkeypatch.setattr(
        email_attachments.tempfile,
        "NamedTemporaryFile",
        fail_tempfile,
    )

    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            _raw_email(),
            {"local": str(tmp_path)},
            max_total_bytes=1,
        )

    assert caught.value.code == "source_too_large"
    assert created == 0


def test_existing_attachment_bytes_are_counted(tmp_path) -> None:
    existing = SourceAttachment(
        id="existing",
        media_type="image/png",
        size_bytes=9,
        content_ref="media-source://existing",
    )
    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            _raw_email(),
            {"local": str(tmp_path)},
            (existing,),
            max_total_bytes=10,
        )
    assert caught.value.code == "source_too_large"


def test_no_supported_parts_need_no_media_directory() -> None:
    assert email_attachments._stage_email_attachments(
        _raw_email(supported=False),
        {},
    ) == ((), ())


def test_supported_parts_require_media_directory() -> None:
    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(_raw_email(), {})
    assert caught.value.code == "media_storage_unavailable"


def test_parse_failure_is_wrapped(monkeypatch, tmp_path) -> None:
    class BrokenParser:
        def parsebytes(self, _raw):
            raise ValueError("broken")

    monkeypatch.setattr(
        email_attachments,
        "BytesParser",
        lambda **_kwargs: BrokenParser(),
    )
    with pytest.raises(
        email_attachments.EmailAttachmentError,
        match="could not be parsed",
    ):
        email_attachments._stage_email_attachments(
            b"mail",
            {"local": str(tmp_path)},
        )


def test_partial_staging_failure_cleans_all_owned_paths(monkeypatch, tmp_path) -> None:
    raw = _raw_email()
    real = email_attachments.tempfile.NamedTemporaryFile
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
        return real(**kwargs)

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


def test_staging_failure_reports_cleanup_error(
    monkeypatch,
    caplog,
    tmp_path,
) -> None:
    def fail_payload(_part):
        raise RuntimeError("decode failed")

    def fail_cleanup(_paths):
        raise OSError("cleanup failed")

    monkeypatch.setattr(
        email_attachments,
        "_attachment_payload",
        fail_payload,
    )
    monkeypatch.setattr(
        email_attachments,
        "_cleanup_paths",
        fail_cleanup,
    )

    with pytest.raises(RuntimeError, match="decode failed"):
        email_attachments._stage_email_attachments(
            _raw_email(),
            {"local": str(tmp_path)},
        )

    assert "staging failure" in caplog.text
    assert "cleanup failed" in caplog.text


def test_cleanup_attempts_all_paths_and_reports_failures(
    monkeypatch,
    tmp_path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    third = tmp_path / "third.png"
    for path in (first, second, third):
        path.write_bytes(b"data")
    original = Path.unlink
    attempted: list[Path] = []

    def unlink(path, missing_ok=False):
        attempted.append(path)
        if path in (first, second):
            raise OSError(f"cannot remove {path.name}")
        return original(path, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(email_attachments.EmailAttachmentCleanupError) as caught:
        email_attachments._cleanup_paths((first, second, third))

    assert caught.value.failed_paths == (first, second)
    assert str(first) in str(caught.value)
    assert attempted == [first, second, third]
    assert not third.exists()


async def test_async_cleanup_succeeds(tmp_path) -> None:
    path = tmp_path / "staged.png"
    path.write_bytes(b"data")

    await email_attachments._async_cleanup_paths(
        AttachmentHass(tmp_path),
        (path,),
        context="success",
    )

    assert not path.exists()


async def test_async_cleanup_finishes_before_propagating_cancellation(
    tmp_path,
) -> None:
    path = tmp_path / "staged.png"
    path.write_bytes(b"data")
    started = asyncio.Event()
    release = asyncio.Event()

    class DelayedHass(AttachmentHass):
        async def async_add_executor_job(self, func, *args):
            started.set()
            await release.wait()
            return await asyncio.to_thread(func, *args)

    task = asyncio.create_task(
        email_attachments._async_cleanup_paths(
            DelayedHass(tmp_path),
            (path,),
            context="cancelled",
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert not path.exists()


async def test_async_cleanup_logs_failure_before_propagating_cancellation(
    monkeypatch,
    caplog,
    tmp_path,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    def fail_cleanup(_paths):
        raise OSError("cleanup failed")

    monkeypatch.setattr(email_attachments, "_cleanup_paths", fail_cleanup)

    class DelayedHass(AttachmentHass):
        async def async_add_executor_job(self, func, *args):
            started.set()
            await release.wait()
            return await asyncio.to_thread(func, *args)

    task = asyncio.create_task(
        email_attachments._async_cleanup_paths(
            DelayedHass(tmp_path),
            (tmp_path / "staged.png",),
            context="cancelled",
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert "cancelled" in caplog.text


async def test_stage_cleanup_logs_ordinary_failure(monkeypatch, caplog, tmp_path) -> None:
    async def fail_cleanup(_hass, _paths, *, context):
        raise OSError(f"{context} failed")

    monkeypatch.setattr(
        email_attachments,
        "_async_cleanup_paths",
        fail_cleanup,
    )
    stage = email_attachments.EmailAttachmentStage(
        _document(),
        AttachmentHass(tmp_path),
        (tmp_path / "staged.png",),
    )

    await stage.async_cleanup("processor failure")

    assert "processor failure" in caplog.text


async def test_stage_cleanup_propagates_cancellation(monkeypatch, tmp_path) -> None:
    async def cancel_cleanup(_hass, _paths, *, context):
        del context
        raise asyncio.CancelledError

    monkeypatch.setattr(
        email_attachments,
        "_async_cleanup_paths",
        cancel_cleanup,
    )
    stage = email_attachments.EmailAttachmentStage(
        _document(),
        AttachmentHass(tmp_path),
        (),
    )

    with pytest.raises(asyncio.CancelledError):
        await stage.async_cleanup("success")


async def test_wait_for_staging_normal_result() -> None:
    future = asyncio.get_running_loop().create_future()
    future.set_result(((), ()))

    result, cancelled = await email_attachments._async_wait_for_staging(future)

    assert result == ((), ())
    assert cancelled is False


async def test_wait_for_staging_defers_outer_cancellation() -> None:
    future = asyncio.get_running_loop().create_future()
    started = asyncio.Event()

    async def wait():
        started.set()
        return await email_attachments._async_wait_for_staging(future)

    task = asyncio.create_task(wait())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    future.set_result(((), ()))

    result, cancelled = await task
    assert result == ((), ())
    assert cancelled is True


async def test_wait_for_staging_propagates_inner_cancellation() -> None:
    future = asyncio.get_running_loop().create_future()
    future.cancel()

    with pytest.raises(asyncio.CancelledError):
        await email_attachments._async_wait_for_staging(future)


async def test_wait_for_staging_propagates_error_without_outer_cancel() -> None:
    future = asyncio.get_running_loop().create_future()
    future.set_exception(RuntimeError("staging failed"))

    with pytest.raises(RuntimeError, match="staging failed"):
        await email_attachments._async_wait_for_staging(future)


async def test_wait_for_staging_preserves_outer_cancel_over_later_error() -> None:
    future = asyncio.get_running_loop().create_future()
    started = asyncio.Event()

    async def wait():
        started.set()
        return await email_attachments._async_wait_for_staging(future)

    task = asyncio.create_task(wait())
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    future.set_exception(RuntimeError("staging failed"))

    with pytest.raises(asyncio.CancelledError):
        await task


async def test_async_stage_returns_explicit_resource_and_cleanup(tmp_path) -> None:
    hass = AttachmentHass(tmp_path)
    original = _document()

    stage = await email_attachments.async_stage_email_attachments(
        hass,
        _envelope(_raw_email()),
        original,
    )

    assert stage.document is not original
    assert len(stage.document.attachments) == 3
    assert len(list(tmp_path.iterdir())) == 3

    await stage.async_cleanup("successful processing")
    assert list(tmp_path.iterdir()) == []


async def test_async_stage_without_attachments_preserves_document(tmp_path) -> None:
    document = _document()
    stage = await email_attachments.async_stage_email_attachments(
        AttachmentHass(tmp_path),
        _envelope(_raw_email(supported=False)),
        document,
    )

    assert stage.document is document
    assert stage._paths == ()


async def test_async_stage_cancellation_cleans_finished_result(tmp_path) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class DelayedHass(AttachmentHass):
        async def async_add_executor_job(self, func, *args):
            if func is email_attachments._stage_email_attachments:
                started.set()
                await release.wait()
            return await asyncio.to_thread(func, *args)

    hass = DelayedHass(tmp_path)
    task = asyncio.create_task(
        email_attachments.async_stage_email_attachments(
            hass,
            _envelope(_raw_email()),
            _document(),
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task
    assert list(tmp_path.iterdir()) == []


async def test_async_stage_cancellation_ignores_cleanup_cancellation(
    monkeypatch,
    tmp_path,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    class DelayedHass(AttachmentHass):
        async def async_add_executor_job(self, func, *args):
            if func is email_attachments._stage_email_attachments:
                started.set()
                await release.wait()
            return await asyncio.to_thread(func, *args)

    async def cancel_cleanup(self, context):
        del self, context
        raise asyncio.CancelledError

    monkeypatch.setattr(
        email_attachments.EmailAttachmentStage,
        "async_cleanup",
        cancel_cleanup,
    )

    task = asyncio.create_task(
        email_attachments.async_stage_email_attachments(
            DelayedHass(tmp_path),
            _envelope(_raw_email()),
            _document(),
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task


def _single_image_email(payload: bytes = JPEG) -> bytes:
    message = EmailMessage()
    message["Subject"] = "Single image"
    message.set_content("See image")
    message.add_attachment(
        payload,
        maintype="image",
        subtype="jpeg",
        filename="single.jpg",
    )
    return message.as_bytes()


def test_cleanup_error_message_preserves_first_error_and_separator(tmp_path) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    error = email_attachments.EmailAttachmentCleanupError(
        (first, second),
        OSError("first failure"),
    )

    assert error.failed_paths == (first, second)
    assert str(error) == (
        "first failure; failed staged email attachment path(s): "
        f"{first}, {second}"
    )


def test_cleanup_reporting_message_is_exact(caplog) -> None:
    email_attachments._report_cleanup_failure(
        "processor failure",
        OSError("cleanup exploded"),
    )

    assert caplog.records[-1].getMessage() == (
        "Failed to clean staged email attachments after processor failure: "
        "cleanup exploded"
    )


def test_attachment_payload_error_message_is_exact() -> None:
    part = EmailMessage()
    part.set_type("image/png")
    part.set_payload([EmailMessage()])

    with pytest.raises(email_attachments.EmailAttachmentError) as caught:
        email_attachments._attachment_payload(part)

    assert caught.value.code == "invalid_attachment"
    assert str(caught.value) == "Email attachment payload could not be decoded"


def test_cleanup_paths_ignores_already_missing_file(tmp_path) -> None:
    email_attachments._cleanup_paths((tmp_path / "already-gone.png",))


def test_cleanup_paths_preserves_first_error_and_all_failed_paths(
    monkeypatch,
    tmp_path,
) -> None:
    first = tmp_path / "first.png"
    second = tmp_path / "second.png"
    for path in (first, second):
        path.write_bytes(b"data")

    def unlink(path, missing_ok=False):
        del missing_ok
        if path == first:
            raise OSError("first failure")
        raise OSError("second failure")

    monkeypatch.setattr(Path, "unlink", unlink)

    with pytest.raises(email_attachments.EmailAttachmentCleanupError) as caught:
        email_attachments._cleanup_paths((first, second))

    assert caught.value.failed_paths == (first, second)
    assert str(caught.value) == (
        "first failure; failed staged email attachment path(s): "
        f"{first}, {second}"
    )


async def test_async_cleanup_cancellation_reports_exact_underlying_error(
    monkeypatch,
    caplog,
    tmp_path,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    def fail_cleanup(_paths):
        raise OSError("cleanup exploded")

    monkeypatch.setattr(email_attachments, "_cleanup_paths", fail_cleanup)

    class DelayedHass(AttachmentHass):
        async def async_add_executor_job(self, func, *args):
            started.set()
            await release.wait()
            return await asyncio.to_thread(func, *args)

    task = asyncio.create_task(
        email_attachments._async_cleanup_paths(
            DelayedHass(tmp_path),
            (tmp_path / "staged.png",),
            context="shutdown",
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert caplog.records[-1].getMessage() == (
        "Failed to clean staged email attachments after shutdown: "
        "cleanup exploded"
    )


async def test_stage_cleanup_passes_and_reports_exact_context(
    monkeypatch,
    caplog,
    tmp_path,
) -> None:
    received_contexts: list[str] = []

    async def fail_cleanup(_hass, _paths, *, context):
        received_contexts.append(context)
        raise OSError("cleanup exploded")

    monkeypatch.setattr(
        email_attachments,
        "_async_cleanup_paths",
        fail_cleanup,
    )
    stage = email_attachments.EmailAttachmentStage(
        _document(),
        AttachmentHass(tmp_path),
        (),
    )

    await stage.async_cleanup("processor failure")

    assert received_contexts == ["processor failure"]
    assert caplog.records[-1].getMessage() == (
        "Failed to clean staged email attachments after processor failure: "
        "cleanup exploded"
    )


def test_raw_message_limit_allows_exact_boundary() -> None:
    raw = _raw_email(supported=False)

    assert email_attachments._stage_email_attachments(
        raw,
        {},
        max_raw_bytes=len(raw),
    ) == ((), ())


def test_raw_message_limit_error_message_is_exact() -> None:
    raw = _raw_email(supported=False)

    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            raw,
            {},
            max_raw_bytes=len(raw) - 1,
        )

    assert caught.value.code == "source_too_large"
    assert str(caught.value) == "Source attachments exceed the size limit"


def test_existing_attachment_count_allows_exact_boundary() -> None:
    existing = tuple(
        SourceAttachment(
            id=str(index),
            media_type="image/png",
            size_bytes=1,
            content_ref=f"media-source://existing/{index}",
        )
        for index in range(4)
    )

    assert email_attachments._stage_email_attachments(
        _raw_email(supported=False),
        {},
        existing,
        max_attachments=4,
    ) == ((), ())


def test_existing_attachment_count_error_message_is_exact() -> None:
    existing = tuple(
        SourceAttachment(
            id=str(index),
            media_type="image/png",
            size_bytes=1,
            content_ref=f"media-source://existing/{index}",
        )
        for index in range(5)
    )

    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            _raw_email(supported=False),
            {},
            existing,
            max_attachments=4,
        )

    assert caught.value.code == "too_many_attachments"
    assert str(caught.value) == "Too many source attachments"


def test_existing_attachment_bytes_allow_exact_boundary() -> None:
    existing = (
        SourceAttachment(
            id="existing",
            media_type="image/png",
            size_bytes=10,
            content_ref="media-source://existing",
        ),
    )

    assert email_attachments._stage_email_attachments(
        _raw_email(supported=False),
        {},
        existing,
        max_total_bytes=10,
    ) == ((), ())


def test_existing_attachment_bytes_error_message_is_exact() -> None:
    existing = (
        SourceAttachment(
            id="existing",
            media_type="image/png",
            size_bytes=11,
            content_ref="media-source://existing",
        ),
    )

    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            _raw_email(supported=False),
            {},
            existing,
            max_total_bytes=10,
        )

    assert caught.value.code == "source_too_large"
    assert str(caught.value) == "Source attachments exceed the size limit"


def test_incoming_attachment_count_error_message_is_exact(tmp_path) -> None:
    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            _raw_email(),
            {"local": str(tmp_path)},
            max_attachments=2,
        )

    assert caught.value.code == "too_many_attachments"
    assert str(caught.value) == "Too many source attachments"


def test_parse_error_message_is_exact(monkeypatch, tmp_path) -> None:
    class BrokenParser:
        def parsebytes(self, _raw):
            raise ValueError("broken")

    monkeypatch.setattr(
        email_attachments,
        "BytesParser",
        lambda **_kwargs: BrokenParser(),
    )

    with pytest.raises(email_attachments.EmailAttachmentError) as caught:
        email_attachments._stage_email_attachments(
            b"mail",
            {"local": str(tmp_path)},
        )

    assert caught.value.code == "invalid_attachment"
    assert str(caught.value) == "Email attachments could not be parsed"


def test_missing_media_directory_error_message_is_exact() -> None:
    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            _single_image_email(),
            {},
        )

    assert caught.value.code == "media_storage_unavailable"
    assert str(caught.value) == "No local media directory configured"


def test_decoded_bytes_allow_exact_boundary(tmp_path) -> None:
    attachments, paths = email_attachments._stage_email_attachments(
        _single_image_email(),
        {"local": str(tmp_path)},
        max_total_bytes=len(JPEG),
    )
    try:
        assert len(attachments) == 1
        assert attachments[0].size_bytes == len(JPEG)
    finally:
        email_attachments._cleanup_paths(paths)


def test_decoded_bytes_error_message_is_exact(tmp_path) -> None:
    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            _single_image_email(),
            {"local": str(tmp_path)},
            max_total_bytes=len(JPEG) - 1,
        )

    assert caught.value.code == "source_too_large"
    assert str(caught.value) == "Source attachments exceed the size limit"
    assert list(tmp_path.iterdir()) == []


def test_decoded_bytes_are_cumulative_across_attachments(tmp_path) -> None:
    message = EmailMessage()
    message.set_content("attachments")
    message.add_attachment(
        b"aaaa",
        maintype="image",
        subtype="jpeg",
        filename="one.jpg",
    )
    message.add_attachment(
        b"bbbb",
        maintype="image",
        subtype="jpeg",
        filename="two.jpg",
    )

    with pytest.raises(SourceValidationError) as caught:
        email_attachments._stage_email_attachments(
            message.as_bytes(),
            {"local": str(tmp_path)},
            max_total_bytes=6,
        )

    assert caught.value.code == "source_too_large"
    assert str(caught.value) == "Source attachments exceed the size limit"
    assert list(tmp_path.iterdir()) == []


def test_nested_media_directory_is_created_with_parents(tmp_path) -> None:
    media_dir = tmp_path / "nested" / "media" / "email"

    attachments, paths = email_attachments._stage_email_attachments(
        _single_image_email(),
        {"local": str(media_dir)},
    )
    try:
        assert media_dir.is_dir()
        assert len(attachments) == 1
    finally:
        email_attachments._cleanup_paths(paths)


def test_named_temporary_file_contract_is_exact(monkeypatch, tmp_path) -> None:
    real = email_attachments.tempfile.NamedTemporaryFile
    calls: list[dict[str, object]] = []

    def capture(**kwargs):
        calls.append(dict(kwargs))
        return real(**kwargs)

    monkeypatch.setattr(
        email_attachments.tempfile,
        "NamedTemporaryFile",
        capture,
    )

    attachments, paths = email_attachments._stage_email_attachments(
        _single_image_email(),
        {"local": str(tmp_path)},
    )
    try:
        assert len(attachments) == 1
        assert calls == [{
            "prefix": "daylight-email-",
            "suffix": ".jpg",
            "dir": tmp_path,
            "delete": False,
        }]
    finally:
        email_attachments._cleanup_paths(paths)


def test_staging_failure_log_context_is_exact(
    monkeypatch,
    caplog,
    tmp_path,
) -> None:
    def fail_payload(_part):
        raise RuntimeError("decode failed")

    def fail_cleanup(_paths):
        raise OSError("cleanup exploded")

    monkeypatch.setattr(
        email_attachments,
        "_attachment_payload",
        fail_payload,
    )
    monkeypatch.setattr(
        email_attachments,
        "_cleanup_paths",
        fail_cleanup,
    )

    with pytest.raises(RuntimeError, match="decode failed"):
        email_attachments._stage_email_attachments(
            _single_image_email(),
            {"local": str(tmp_path)},
        )

    assert caplog.records[-1].getMessage() == (
        "Failed to clean staged email attachments after staging failure: "
        "cleanup exploded"
    )


async def test_async_stage_accounts_for_existing_attachments(tmp_path) -> None:
    existing = tuple(
        SourceAttachment(
            id=str(index),
            media_type="image/png",
            size_bytes=1,
            content_ref=f"media-source://existing/{index}",
        )
        for index in range(4)
    )

    with pytest.raises(SourceValidationError) as caught:
        await email_attachments.async_stage_email_attachments(
            AttachmentHass(tmp_path),
            _envelope(_single_image_email()),
            _document(existing),
        )

    assert caught.value.code == "too_many_attachments"
    assert str(caught.value) == "Too many source attachments"
    assert list(tmp_path.iterdir()) == []


async def test_async_stage_cancellation_uses_exact_cleanup_context(
    monkeypatch,
    tmp_path,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    contexts: list[str] = []

    class DelayedHass(AttachmentHass):
        async def async_add_executor_job(self, func, *args):
            if func is email_attachments._stage_email_attachments:
                started.set()
                await release.wait()
            return await asyncio.to_thread(func, *args)

    async def capture_cleanup(self, context):
        contexts.append(context)
        email_attachments._cleanup_paths(self._paths)

    monkeypatch.setattr(
        email_attachments.EmailAttachmentStage,
        "async_cleanup",
        capture_cleanup,
    )

    task = asyncio.create_task(
        email_attachments.async_stage_email_attachments(
            DelayedHass(tmp_path),
            _envelope(_single_image_email()),
            _document(),
        )
    )
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert contexts == ["staging cancellation"]
    assert list(tmp_path.iterdir()) == []
