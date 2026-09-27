"""Tests for bounded image upload ingestion and cleanup."""

import asyncio
from contextlib import contextmanager
from datetime import UTC
from hashlib import sha256
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from uuid import UUID

import pytest

from custom_components.daylight_calendar_import import uploads
from custom_components.daylight_calendar_import.providers import SourceValidationError
from custom_components.daylight_calendar_import.sources import SourceKind
from homeassistant.components.file_upload import FileUploadData


PNG = b"\x89PNG\r\n\x1a\n" + b"valid image payload"
JPEG = b"\xff\xd8\xff" + b"valid image payload"
WEBP = b"RIFF1234WEBP" + b"valid image payload"


class UploadHass:
    def __init__(self, media_dir):
        self.config = SimpleNamespace(media_dirs={"local": str(media_dir)})
        self.data = {}

    async def async_add_executor_job(self, func, *args):
        return await asyncio.to_thread(func, *args)


@pytest.fixture
def uploaded_file(monkeypatch, tmp_path):
    original = tmp_path / "upload.bin"
    file_id = "a" * 32

    @contextmanager
    def process(_hass, received_id):
        assert received_id == file_id
        try:
            yield original
        finally:
            original.unlink(missing_ok=True)

    monkeypatch.setattr(uploads, "process_uploaded_file", process)
    return original, file_id


@pytest.mark.parametrize(("data", "media_type", "suffix"), [
    (PNG, "image/png", ".png"),
    (JPEG, "image/jpeg", ".jpg"),
    (WEBP, "image/webp", ".webp"),
])
async def test_upload_stage_then_cleanup(uploaded_file, tmp_path, data, media_type, suffix):
    original, file_id = uploaded_file
    original.write_bytes(data)
    hass = UploadHass(tmp_path / "media")
    async with uploads.async_image_source(hass, file_id) as source:
        attachment = source.attachments[0]
        assert attachment.media_type == media_type
        assert attachment.size_bytes == len(data)
        assert attachment.content_ref.startswith("media-source://media_source/local/daylight-")
        staged = next((tmp_path / "media").iterdir())
        assert staged.suffix == suffix
        assert staged.read_bytes() == data
        assert source.text is None
        assert attachment.sha256
        assert attachment.sha256 == sha256(data).hexdigest()
        assert UUID(source.id)
        assert UUID(attachment.id)
        assert source.kind is SourceKind.IMAGE
        assert source.received_at.tzinfo is UTC
        assert not original.exists()
    assert not staged.exists()


async def test_upload_cleanup_on_parser_error(uploaded_file, tmp_path):
    original, file_id = uploaded_file
    original.write_bytes(PNG)
    hass = UploadHass(tmp_path / "media")
    with pytest.raises(RuntimeError, match="parser failure"):
        async with uploads.async_image_source(hass, file_id):
            staged = next((tmp_path / "media").iterdir())
            raise RuntimeError("parser failure")
    assert not original.exists()
    assert not staged.exists()


async def test_cancellation_during_staging_cleans_late_image(uploaded_file, tmp_path):
    original, file_id = uploaded_file
    original.write_bytes(PNG)
    stage_started = asyncio.Event()
    release_stage = asyncio.Event()
    cleanup_done = asyncio.Event()

    class DelayedHass(UploadHass):
        async def async_add_executor_job(self, func, *args):
            if func is uploads._stage_image:
                stage_started.set()
                await release_stage.wait()
            result = await asyncio.to_thread(func, *args)
            if func is not uploads._stage_image:
                cleanup_done.set()
            return result

    async def parse_uploaded():
        async with uploads.async_image_source(DelayedHass(tmp_path / "media"), file_id):
            pytest.fail("cancelled call reached provider")

    task = asyncio.create_task(parse_uploaded())
    await asyncio.wait_for(stage_started.wait(), 2)
    task.cancel()
    release_stage.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    await asyncio.wait_for(cleanup_done.wait(), 2)
    assert not original.exists()
    assert not list((tmp_path / "media").iterdir())


async def test_cleanup_consumes_failed_staging_result(tmp_path):
    staging = asyncio.get_running_loop().create_future()
    staging.set_exception(OSError("upload unavailable"))
    await uploads._cleanup_late_staging(UploadHass(tmp_path / "media"), staging)
    assert not (tmp_path / "media").exists()


async def test_late_cleanup_tolerates_already_removed_media(tmp_path):
    staging = asyncio.get_running_loop().create_future()
    staging.set_result((None, tmp_path / "already-removed.png"))
    await uploads._cleanup_late_staging(UploadHass(tmp_path / "media"), staging)


@pytest.mark.parametrize(("data", "code"), [
    (b"", "empty_attachment"),
    (b"not an image", "unsupported_media"),
    (b"RIFF1234NOPE", "unsupported_media"),
    (b"NOPE1234WEBP", "unsupported_media"),
    (PNG + b"x" * uploads.MAX_UPLOAD_BYTES, "source_too_large"),
])
async def test_reject_invalid_upload_and_consume_original(uploaded_file, tmp_path, data, code):
    original, file_id = uploaded_file
    original.write_bytes(data)
    hass = UploadHass(tmp_path / "media")
    with pytest.raises(SourceValidationError) as caught:
        async with uploads.async_image_source(hass, file_id):
            pytest.fail("invalid upload reached provider")
    assert caught.value.code == code
    assert str(caught.value) == {
        "empty_attachment": "Source attachment is empty",
        "unsupported_media": "Unsupported attachment media type",
        "source_too_large": "Source attachments exceed the size limit",
    }[code]
    assert not original.exists()
    assert not (tmp_path / "media").exists()


async def test_no_media_directory_consumes_upload(uploaded_file, tmp_path):
    original, file_id = uploaded_file
    original.write_bytes(PNG)
    hass = UploadHass(tmp_path / "media")
    hass.config.media_dirs = {}
    with pytest.raises(SourceValidationError) as caught:
        async with uploads.async_image_source(hass, file_id):
            pytest.fail("upload cannot be staged")
    assert caught.value.code == "media_storage_unavailable"
    assert str(caught.value) == "No local media directory configured"
    assert not original.exists()


@pytest.mark.parametrize("incomplete_exists", [True, False])
async def test_failed_media_write_removes_incomplete_file(uploaded_file, tmp_path, monkeypatch, incomplete_exists):
    original, file_id = uploaded_file
    original.write_bytes(PNG)
    staged = tmp_path / "incomplete.png"
    if incomplete_exists:
        staged.write_bytes(b"partial")

    class FailingFile:
        name = str(staged)

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def write(self, _data):
            raise OSError("storage full")

    monkeypatch.setattr(uploads.tempfile, "NamedTemporaryFile", lambda **_kwargs: FailingFile())
    with pytest.raises(OSError, match="storage full"):
        async with uploads.async_image_source(UploadHass(tmp_path / "media"), file_id):
            pytest.fail("failed write reached parser")
    assert not staged.exists()
    assert not original.exists()


async def test_real_home_assistant_upload_is_consumed(tmp_path):
    file_id = "a" * 32
    temp_dir = tmp_path / "uploads"
    original_dir = temp_dir / file_id
    original_dir.mkdir(parents=True)
    (original_dir / "flyer.png").write_bytes(PNG)
    hass = UploadHass(tmp_path / "media")
    hass.data["file_upload"] = FileUploadData(temp_dir, {file_id: "flyer.png"})

    async with uploads.async_image_source(hass, file_id) as source:
        assert source.attachments[0].filename == "flyer.png"
        assert not original_dir.exists()
    assert not list((tmp_path / "media").iterdir())


async def test_maximum_allowed_image_is_accepted(uploaded_file, tmp_path):
    original, file_id = uploaded_file
    original.write_bytes(PNG + b"x" * (uploads.MAX_UPLOAD_BYTES - len(PNG)))
    async with uploads.async_image_source(UploadHass(tmp_path / "media"), file_id) as source:
        assert source.attachments[0].size_bytes == uploads.MAX_UPLOAD_BYTES


async def test_staging_creates_nested_media_directory(uploaded_file, tmp_path):
    original, file_id = uploaded_file
    original.write_bytes(JPEG)
    directory = tmp_path / "nested" / "media"
    async with uploads.async_image_source(UploadHass(directory), file_id):
        assert directory.is_dir()
    original.write_bytes(JPEG)
    async with uploads.async_image_source(UploadHass(directory), file_id):
        assert directory.is_dir()


def test_upload_read_is_bounded_before_parsing(monkeypatch, tmp_path):
    class BoundedReader(BytesIO):
        def read(self, size=-1):
            assert size == uploads.MAX_UPLOAD_BYTES + 1
            return super().read(size)

    class Uploaded:
        name = "flyer.png"

        def open(self, mode):
            assert mode == "rb"
            return BoundedReader(PNG)

    @contextmanager
    def process(hass, file_id):
        assert hass is not None
        assert file_id == "a" * 32
        yield Uploaded()

    monkeypatch.setattr(uploads, "process_uploaded_file", process)
    _, staged = uploads._stage_image(UploadHass(tmp_path), "a" * 32, {"local": str(tmp_path / "media")})
    staged.unlink()
