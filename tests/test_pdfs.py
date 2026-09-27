"""PDF extraction, fallback, validation, and temporary-file cleanup."""

import asyncio
from contextlib import contextmanager
from datetime import UTC
from hashlib import sha256
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID
import zlib

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, EncodedStreamObject, NameObject

from custom_components.daylight_calendar_import import pdfs
from custom_components.daylight_calendar_import.providers import SourceValidationError
from custom_components.daylight_calendar_import.sources import SourceKind


def make_pdf(text=None, *, encrypted=False, page_count=1):
    writer = PdfWriter()
    for _ in range(page_count):
        page = writer.add_blank_page(width=300, height=300)
        if text is not None:
            font = writer._add_object(DictionaryObject({
                NameObject("/Type"): NameObject("/Font"),
                NameObject("/Subtype"): NameObject("/Type1"),
                NameObject("/BaseFont"): NameObject("/Helvetica"),
            }))
            page[NameObject("/Resources")] = DictionaryObject({
                NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            })
            stream = DecodedStreamObject()
            stream.set_data(f"BT /F1 12 Tf 20 200 Td ({text}) Tj ET".encode())
            page[NameObject("/Contents")] = writer._add_object(stream)
    if encrypted:
        writer.encrypt("secret")
    output = BytesIO()
    writer.write(output)
    return output.getvalue()


class UploadHass:
    def __init__(self, media_dirs):
        self.config = SimpleNamespace(media_dirs=media_dirs)

    async def async_add_executor_job(self, func, *args):
        return await asyncio.to_thread(func, *args)


@pytest.fixture
def uploaded_file(monkeypatch, tmp_path):
    original = tmp_path / "schedule.pdf"
    @contextmanager
    def process(_hass, file_id):
        assert file_id == "a" * 32
        try:
            yield original
        finally:
            original.unlink(missing_ok=True)
    monkeypatch.setattr(pdfs, "process_uploaded_file", process)
    return original


async def test_text_layer_uses_local_extraction_without_media(uploaded_file):
    data = make_pdf("Soccer Thursday 5pm")
    uploaded_file.write_bytes(data)
    async with pdfs.async_pdf_source(UploadHass({}), "a" * 32, "  Team schedule  ") as source:
        assert source.kind is SourceKind.PDF
        assert UUID(source.id)
        assert source.received_at.tzinfo is UTC
        assert source.title == "schedule.pdf"
        assert source.text == "Team schedule\n\nSoccer Thursday 5pm"
        assert source.attachments == ()
        assert source.metadata["sha256"] == sha256(data).hexdigest()
        assert not uploaded_file.exists()


async def test_scanned_pdf_fallback_and_cleanup(uploaded_file, tmp_path):
    data = make_pdf()
    uploaded_file.write_bytes(data)
    directory = tmp_path / "media"
    with pytest.raises(RuntimeError, match="provider error"):
        async with pdfs.async_pdf_source(UploadHass({"local": str(directory)}), "a" * 32, "  Context ") as source:
            assert source.text == "Context"
            assert UUID(source.id)
            assert source.kind is SourceKind.PDF
            assert source.received_at.tzinfo is UTC
            assert source.title == "schedule.pdf"
            assert source.attachments[0].media_type == "application/pdf"
            assert UUID(source.attachments[0].id)
            assert source.attachments[0].filename == "schedule.pdf"
            assert source.attachments[0].size_bytes == len(data)
            assert source.attachments[0].sha256 == sha256(data).hexdigest()
            assert source.attachments[0].content_ref.startswith("media-source://media_source/local/daylight-")
            staged = next(directory.iterdir())
            assert staged.suffix == ".pdf"
            assert staged.read_bytes().startswith(b"%PDF-")
            assert not uploaded_file.exists()
            raise RuntimeError("provider error")
    assert not staged.exists()


async def test_mixed_text_and_scanned_pages_retain_pdf_attachment(uploaded_file, tmp_path):
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(make_pdf("Cover page"))))
    writer.add_blank_page(width=300, height=300)
    output = BytesIO()
    writer.write(output)
    uploaded_file.write_bytes(output.getvalue())
    async with pdfs.async_pdf_source(UploadHass({"local": str(tmp_path)}), "a" * 32) as source:
        assert source.text == "Cover page"
        assert source.attachments[0].media_type == "application/pdf"
        assert source.attachments[0].sha256 == sha256(output.getvalue()).hexdigest()
        assert len(list(tmp_path.glob("daylight-*.pdf"))) == 1
    assert not list(tmp_path.glob("daylight-*.pdf"))


def test_large_compressed_page_falls_back_before_text_extraction(monkeypatch):
    raw = zlib.compress(b" " * (pdfs.MAX_PDF_PAGE_CONTENT_BYTES + 1))
    stream = EncodedStreamObject()
    stream._data = raw
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None,
                           extract_text=lambda: pytest.fail("unbounded content reached extraction"))
    monkeypatch.setattr(pdfs, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    assert pdfs._extract_pdf_text(b"%PDF-fake") == ("", True)


def test_page_with_image_retains_attachment_even_with_text(monkeypatch):
    page = SimpleNamespace(get=lambda key: {"/XObject": {"/Im0": object()}} if key == "/Resources" else None,
                           extract_text=lambda: pytest.fail("image content reached text-only path"))
    monkeypatch.setattr(pdfs, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    assert pdfs._extract_pdf_text(b"%PDF-fake") == ("", True)


def test_indirect_page_resources_with_images_retain_attachment():
    resources = SimpleNamespace(get_object=lambda: {"/XObject": {"/Im0": object()}})
    page = SimpleNamespace(get=lambda key: resources if key == "/Resources" else None)
    assert pdfs._bounded_text_page(page) is False


@pytest.mark.parametrize(("raw", "filter_name", "expected"), [
    (b"x" * (pdfs.MAX_PDF_PAGE_CONTENT_BYTES + 1), None, False),
    (b"BT (Meeting) Tj ET", None, True),
    (b"not zlib", "/FlateDecode", False),
    (zlib.compress(b"Meeting")[:-2], "/FlateDecode", False),
    (b"Meeting", "/ASCII85Decode", False),
])
def test_page_stream_decoding_is_bounded(raw, filter_name, expected):
    stream = DecodedStreamObject()
    stream.set_data(raw)
    if filter_name:
        stream[NameObject("/Filter")] = NameObject(filter_name)
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None)
    assert pdfs._bounded_text_page(page) is expected


def test_page_with_too_many_content_streams_falls_back():
    streams = [DecodedStreamObject() for _ in range(101)]
    contents = SimpleNamespace(get_object=lambda: streams)
    page = SimpleNamespace(get=lambda key: contents if key == "/Contents" else None)
    assert pdfs._bounded_text_page(page) is False


@pytest.mark.parametrize(("data", "code", "message"), [
    (b"", "empty_attachment", "Source attachment is empty"),
    (b"not a PDF", "unsupported_media", "Unsupported attachment media type"),
    (b"%PDF-broken", "invalid_pdf", "PDF could not be read"),
    (make_pdf(encrypted=True), "unsupported_pdf", "Encrypted PDF is not supported"),
    (make_pdf(page_count=0), "invalid_pdf", "PDF has no pages"),
    (make_pdf(page_count=pdfs.MAX_PDF_PAGES + 1), "too_many_pages", "PDF exceeds the page limit"),
    (b"%PDF-" + b"x" * pdfs.MAX_UPLOAD_BYTES, "source_too_large", "Source attachments exceed the size limit"),
])
async def test_invalid_pdf_is_explicit_and_consumed(uploaded_file, data, code, message):
    uploaded_file.write_bytes(data)
    with pytest.raises(SourceValidationError) as caught:
        async with pdfs.async_pdf_source(UploadHass({}), "a" * 32):
            pytest.fail("invalid PDF reached parser")
    assert caught.value.code == code
    assert str(caught.value) == message
    assert not uploaded_file.exists()


async def test_scanned_pdf_without_local_media_is_explicit(uploaded_file):
    uploaded_file.write_bytes(make_pdf())
    with pytest.raises(SourceValidationError) as caught:
        async with pdfs.async_pdf_source(UploadHass({}), "a" * 32):
            pytest.fail("no media directory")
    assert caught.value.code == "media_storage_unavailable"
    assert str(caught.value) == "No local media directory configured"
    assert not uploaded_file.exists()


@pytest.mark.parametrize("incomplete_exists", [True, False])
async def test_failed_pdf_staging_cleans_incomplete_copy(uploaded_file, tmp_path, monkeypatch, incomplete_exists):
    uploaded_file.write_bytes(make_pdf())
    staged = tmp_path / "partial.pdf"
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

    monkeypatch.setattr(pdfs.tempfile, "NamedTemporaryFile", lambda **_kwargs: FailingFile())
    with pytest.raises(OSError, match="storage full"):
        async with pdfs.async_pdf_source(UploadHass({"local": str(tmp_path / "media")}), "a" * 32):
            pytest.fail("failed staging reached parser")
    assert not staged.exists()
    assert not uploaded_file.exists()


@pytest.mark.parametrize("has_text", [True, False])
async def test_cancellation_during_pdf_staging_cleans_upload(uploaded_file, tmp_path, has_text):
    uploaded_file.write_bytes(make_pdf("Meeting Friday" if has_text else None))
    stage_started = asyncio.Event()
    release_stage = asyncio.Event()
    media_dir = tmp_path / "media"

    class DelayedHass(UploadHass):
        async def async_add_executor_job(self, func, *args):
            if func is pdfs._stage_pdf:
                stage_started.set()
                await release_stage.wait()
            return await asyncio.to_thread(func, *args)

    async def parse_uploaded():
        async with pdfs.async_pdf_source(DelayedHass({"local": str(media_dir)}), "a" * 32):
            pytest.fail("cancelled PDF reached provider")

    task = asyncio.create_task(parse_uploaded())
    await asyncio.wait_for(stage_started.wait(), 2)
    task.cancel()
    release_stage.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, 2)
    assert not uploaded_file.exists()
    if media_dir.exists():
        assert not list(media_dir.iterdir())


def test_pdf_text_limit_is_enforced(monkeypatch):
    fake_page = SimpleNamespace(get=lambda _key: None, extract_text=lambda: "x" * (pdfs.MAX_PDF_TEXT_CHARS + 1))
    monkeypatch.setattr(pdfs, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[fake_page]))
    with pytest.raises(SourceValidationError) as caught:
        pdfs._extract_pdf_text(b"%PDF-fake")
    assert caught.value.code == "source_too_large"
    assert str(caught.value) == "PDF text exceeds the size limit"


def test_pdf_text_limit_counts_all_pages_and_accepts_exact_limit(monkeypatch):
    text = "x" * ((pdfs.MAX_PDF_TEXT_CHARS - 2) // 2)
    pages = [SimpleNamespace(get=lambda _key: None, extract_text=lambda: text) for _ in range(2)]
    def reader(_stream, *, strict):
        assert strict is False
        return SimpleNamespace(is_encrypted=False, pages=pages)
    monkeypatch.setattr(pdfs, "PdfReader", reader)
    assert pdfs._extract_pdf_text(b"%PDF-fake") == (text + "\n\n" + text, False)
    pages.append(SimpleNamespace(get=lambda _key: None, extract_text=lambda: "one more"))
    with pytest.raises(SourceValidationError) as caught:
        pdfs._extract_pdf_text(b"%PDF-fake")
    assert caught.value.code == "source_too_large"


def test_pdf_page_limit_includes_last_allowed_page():
    assert pdfs._extract_pdf_text(make_pdf(page_count=pdfs.MAX_PDF_PAGES)) == ("", True)


async def test_scanned_pdf_creates_nested_media_directory(uploaded_file, tmp_path):
    directory = tmp_path / "nested" / "media"
    for _ in range(2):
        uploaded_file.write_bytes(make_pdf())
        async with pdfs.async_pdf_source(UploadHass({"local": str(directory)}), "a" * 32) as source:
            assert source.text is None
            assert directory.is_dir()


def test_pdf_read_is_bounded_before_extraction(monkeypatch, tmp_path):
    class BoundedReader(BytesIO):
        def read(self, size=-1):
            assert size == pdfs.MAX_UPLOAD_BYTES + 1
            return super().read(size)

    class Uploaded:
        name = "schedule.pdf"

        def open(self, mode):
            assert mode == "rb"
            return BoundedReader(make_pdf("Meeting Friday"))

    @contextmanager
    def process(hass, file_id):
        assert hass is not None
        assert file_id == "a" * 32
        yield Uploaded()

    monkeypatch.setattr(pdfs, "process_uploaded_file", process)
    source, staged = pdfs._stage_pdf(UploadHass({}), "a" * 32, {}, "")
    assert source.text == "Meeting Friday"
    assert staged is None


async def test_maximum_allowed_pdf_is_accepted(uploaded_file, monkeypatch):
    data = b"%PDF-" + b"x" * (pdfs.MAX_UPLOAD_BYTES - 5)
    uploaded_file.write_bytes(data)
    monkeypatch.setattr(pdfs, "_extract_pdf_text", lambda _data: ("Meeting Friday", False))
    async with pdfs.async_pdf_source(UploadHass({}), "a" * 32) as source:
        assert source.metadata["sha256"] == sha256(data).hexdigest()
