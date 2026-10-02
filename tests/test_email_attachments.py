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
        message.add_related(
            PNG,
            maintype="image",
            subtype="png",
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
