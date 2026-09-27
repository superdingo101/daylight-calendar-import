"""PDF extraction, fallback, validation, and temporary-file cleanup."""

import asyncio
from contextlib import contextmanager
from datetime import UTC
from hashlib import sha256
from io import BytesIO, StringIO
import json
from types import SimpleNamespace
from uuid import UUID
import zlib
import subprocess

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.errors import PdfReadError
from pypdf.generic import DecodedStreamObject, DictionaryObject, EncodedStreamObject, NameObject

from custom_components.daylight_calendar_import import pdfs, pdf_worker as worker
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
        else:
            stream = DecodedStreamObject()
            stream.set_data(b"0 0 100 100 re f")
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
    scanned = writer.add_blank_page(width=300, height=300)
    stream = DecodedStreamObject()
    stream.set_data(b"0 0 100 100 re f")
    scanned[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    uploaded_file.write_bytes(output.getvalue())
    async with pdfs.async_pdf_source(UploadHass({"local": str(tmp_path)}), "a" * 32) as source:
        assert source.text == "Cover page"
        assert source.attachments[0].media_type == "application/pdf"
        assert source.attachments[0].sha256 == sha256(output.getvalue()).hexdigest()
        assert len(list(tmp_path.glob("daylight-*.pdf"))) == 1
    assert not list(tmp_path.glob("daylight-*.pdf"))


async def test_blank_page_in_text_pdf_does_not_require_attachment(uploaded_file):
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(make_pdf("Meeting Friday"))))
    writer.add_blank_page(width=300, height=300)
    output = BytesIO()
    writer.write(output)
    uploaded_file.write_bytes(output.getvalue())
    async with pdfs.async_pdf_source(UploadHass({}), "a" * 32) as source:
        assert source.text == "Meeting Friday"
        assert source.attachments == ()


def test_large_compressed_page_falls_back_before_text_extraction(monkeypatch):
    raw = zlib.compress(b" " * (worker.MAX_PDF_PAGE_CONTENT_BYTES + 1))
    stream = EncodedStreamObject()
    stream._data = raw
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None,
                           extract_text=lambda: pytest.fail("unbounded content reached extraction"))
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    assert worker.extract_text(b"%PDF-fake") == ("", True)


def test_page_with_image_retains_attachment_even_with_text(monkeypatch):
    page = SimpleNamespace(get=lambda key: {"/XObject": {"/Im0": object()}} if key == "/Resources" else None,
                           extract_text=lambda: pytest.fail("image content reached text-only path"))
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    assert worker.extract_text(b"%PDF-fake") == ("", True)


def test_indirect_page_resources_with_images_retain_attachment():
    resources = SimpleNamespace(get_object=lambda: {"/XObject": {"/Im0": object()}})
    page = SimpleNamespace(get=lambda key: resources if key == "/Resources" else None)
    assert worker._bounded_text_page(page) is False


def test_annotated_page_retains_form_values_as_pdf_evidence(monkeypatch):
    page = SimpleNamespace(get=lambda key: [object()] if key == "/Annots" else None,
                           extract_text=lambda: pytest.fail("form values reached text-only path"))
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    assert worker.extract_text(b"%PDF-fake") == ("", True)


@pytest.mark.parametrize("operator", [b"BI /W", b"BI/W"])
def test_inline_image_operator_retains_pdf_attachment(monkeypatch, operator):
    stream = DecodedStreamObject()
    stream.set_data(b"BT (Cover text) Tj ET " + operator + b" 1/H 1 ID x EI")
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None,
                           extract_text=lambda: pytest.fail("inline image reached text-only path"))
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    assert worker.extract_text(b"%PDF-fake") == ("", True)


@pytest.mark.parametrize(("response", "code"), [
    (SimpleNamespace(returncode=1, stdout=b""), "invalid_pdf"),
    (SimpleNamespace(returncode=0, stdout=b"not json"), "invalid_pdf"),
    (SimpleNamespace(returncode=0, stdout=b"{}"), "invalid_pdf"),
    (SimpleNamespace(returncode=0, stdout=b'{"error":"too_many_pages","message":"PDF exceeds the page limit"}'), "too_many_pages"),
])
def test_worker_failures_are_explicit(monkeypatch, response, code):
    monkeypatch.setattr(pdfs.subprocess, "run", lambda *_args, **_kwargs: response)
    with pytest.raises(SourceValidationError) as caught:
        pdfs._extract_pdf_text(b"%PDF-fake")
    assert caught.value.code == code


def test_worker_timeout_is_explicit(monkeypatch):
    def timeout(*_args, **_kwargs):
        raise subprocess.TimeoutExpired("pdf_worker.py", 20)
    monkeypatch.setattr(pdfs.subprocess, "run", timeout)
    with pytest.raises(SourceValidationError) as caught:
        pdfs._extract_pdf_text(b"%PDF-fake")
    assert caught.value.code == "invalid_pdf"
    assert str(caught.value) == "PDF extraction timed out"


@pytest.mark.parametrize(("data", "error", "message"), [
    (make_pdf("Meeting Friday"), None, None),
    (b"not pdf", "unsupported_media", "Unsupported attachment media type"),
    (b"too long", "source_too_large", "Source attachments exceed the size limit"),
])
def test_worker_main_applies_limits_and_serializes_result(monkeypatch, data, error, message):
    import resource
    calls = []
    monkeypatch.setattr(resource, "setrlimit", lambda kind, limits: calls.append((kind, limits)))
    if error is None:
        monkeypatch.setattr(worker, "MAX_INPUT_BYTES", len(data))
    elif error == "source_too_large":
        monkeypatch.setattr(worker, "MAX_INPUT_BYTES", 3)
    class BoundedInput(BytesIO):
        def read(self, size=-1):
            assert size == worker.MAX_INPUT_BYTES + 1
            return super().read(size)
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=BoundedInput(data)))
    output = StringIO()
    monkeypatch.setattr(worker.sys, "stdout", output)
    worker.main()
    payload = json.loads(output.getvalue())
    if error:
        assert payload == {"error": error, "message": message}
    else:
        assert payload == {"text": "Meeting Friday", "needs_attachment": False}
    assert calls == [
        (resource.RLIMIT_AS, (worker.MAX_WORKER_MEMORY_BYTES, worker.MAX_WORKER_MEMORY_BYTES)),
        (resource.RLIMIT_CPU, (worker.MAX_WORKER_CPU_SECONDS, worker.MAX_WORKER_CPU_SECONDS)),
    ]


def test_worker_main_maps_memory_limit(monkeypatch):
    import resource
    monkeypatch.setattr(resource, "setrlimit", lambda *_args: None)
    monkeypatch.setattr(worker.sys, "stdin", SimpleNamespace(buffer=BytesIO(b"%PDF-fake")))
    output = StringIO()
    monkeypatch.setattr(worker.sys, "stdout", output)
    monkeypatch.setattr(worker, "extract_text", lambda _data: (_ for _ in ()).throw(MemoryError()))
    worker.main()
    assert json.loads(output.getvalue()) == {
        "error": "source_too_large", "message": "PDF exceeds the extraction resource limit"
    }


@pytest.mark.parametrize(("encrypted", "page_count", "code", "message"), [
    (True, 1, "unsupported_pdf", "Encrypted PDF is not supported"),
    (False, 0, "invalid_pdf", "PDF has no pages"),
    (False, worker.MAX_PDF_PAGES + 1, "too_many_pages", "PDF exceeds the page limit"),
])
def test_worker_rejects_unsupported_document_shapes(monkeypatch, encrypted, page_count, code, message):
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(
        is_encrypted=encrypted, pages=[object()] * page_count
    ))
    with pytest.raises(worker.PdfExtractionError) as caught:
        worker.extract_text(b"%PDF-fake")
    assert caught.value.code == code
    assert str(caught.value) == message


def test_worker_maps_pdf_parser_error(monkeypatch):
    def fail(*_args, **_kwargs):
        raise PdfReadError("private parser details")
    monkeypatch.setattr(worker, "PdfReader", fail)
    with pytest.raises(worker.PdfExtractionError) as caught:
        worker.extract_text(b"%PDF-fake")
    assert caught.value.code == "invalid_pdf"
    assert str(caught.value) == "PDF could not be read"


@pytest.mark.parametrize(("raw", "filter_name", "expected"), [
    (b"x" * (worker.MAX_PDF_PAGE_CONTENT_BYTES + 1), None, False),
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
    assert worker._bounded_text_page(page) is expected


def test_page_with_too_many_content_streams_falls_back():
    streams = [DecodedStreamObject() for _ in range(101)]
    contents = SimpleNamespace(get_object=lambda: streams)
    page = SimpleNamespace(get=lambda key: contents if key == "/Contents" else None)
    assert worker._bounded_text_page(page) is False


def test_last_allowed_stream_and_exact_page_byte_limit_are_accepted():
    streams = [DecodedStreamObject() for _ in range(100)]
    streams[-1].set_data(b"x" * worker.MAX_PDF_PAGE_CONTENT_BYTES)
    contents = SimpleNamespace(get_object=lambda: streams)
    page = SimpleNamespace(get=lambda key: contents if key == "/Contents" else None)
    assert worker._bounded_text_page(page) is True


def test_page_content_limit_applies_across_multiple_streams():
    streams = [DecodedStreamObject() for _ in range(2)]
    streams[0].set_data(b"x" * (worker.MAX_PDF_PAGE_CONTENT_BYTES // 2 + 1))
    streams[1].set_data(b"y" * (worker.MAX_PDF_PAGE_CONTENT_BYTES // 2))
    contents = SimpleNamespace(get_object=lambda: streams)
    page = SimpleNamespace(get=lambda key: contents if key == "/Contents" else None)
    assert worker._bounded_text_page(page) is False


@pytest.mark.parametrize("filter_name", ["/FlateDecode", "/Fl"])
def test_safe_flate_stream_is_extracted_with_a_strict_decoding_bound(monkeypatch, filter_name):
    compressed = zlib.compress(b"BT (Meeting) Tj ET")
    stream = EncodedStreamObject()
    stream._data = compressed
    stream[NameObject("/Filter")] = NameObject(filter_name)
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None)
    real_decoder = zlib.decompressobj
    def bounded_decoder():
        decoder = real_decoder()
        class Spy:
            @property
            def unconsumed_tail(self):
                return decoder.unconsumed_tail
            @property
            def eof(self):
                return decoder.eof
            def decompress(self, data, limit):
                assert data == compressed
                assert limit == worker.MAX_PDF_PAGE_CONTENT_BYTES + 1
                return decoder.decompress(data, limit)
        return Spy()
    monkeypatch.setattr(worker.zlib, "decompressobj", bounded_decoder)
    assert worker._bounded_text_page(page) is True


def test_unsupported_filter_is_not_decoded_even_if_bytes_are_valid_zlib():
    stream = EncodedStreamObject()
    stream._data = zlib.compress(b"Meeting")
    stream[NameObject("/Filter")] = NameObject("/ASCII85Decode")
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None)
    assert worker._bounded_text_page(page) is False


@pytest.mark.parametrize(("data", "code", "message"), [
    (b"", "empty_attachment", "Source attachment is empty"),
    (b"not a PDF", "unsupported_media", "Unsupported attachment media type"),
    (b"%PDF-broken", "invalid_pdf", "PDF could not be read"),
    (make_pdf(encrypted=True), "unsupported_pdf", "Encrypted PDF is not supported"),
    (make_pdf(page_count=0), "invalid_pdf", "PDF has no pages"),
    (make_pdf(page_count=worker.MAX_PDF_PAGES + 1), "too_many_pages", "PDF exceeds the page limit"),
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
    fake_page = SimpleNamespace(get=lambda _key: None, extract_text=lambda: "x" * (worker.MAX_PDF_TEXT_CHARS + 1))
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[fake_page]))
    with pytest.raises(worker.PdfExtractionError) as caught:
        worker.extract_text(b"%PDF-fake")
    assert caught.value.code == "source_too_large"
    assert str(caught.value) == "PDF text exceeds the size limit"


def test_pdf_text_limit_counts_all_pages_and_accepts_exact_limit(monkeypatch):
    text = "x" * ((worker.MAX_PDF_TEXT_CHARS - 2) // 2)
    pages = [SimpleNamespace(get=lambda _key: None, extract_text=lambda: text) for _ in range(2)]
    def reader(_stream, *, strict):
        assert strict is False
        return SimpleNamespace(is_encrypted=False, pages=pages)
    monkeypatch.setattr(worker, "PdfReader", reader)
    assert worker.extract_text(b"%PDF-fake") == (text + "\n\n" + text, False)
    pages.append(SimpleNamespace(get=lambda _key: None, extract_text=lambda: "x"))
    with pytest.raises(worker.PdfExtractionError) as caught:
        worker.extract_text(b"%PDF-fake")
    assert caught.value.code == "source_too_large"


def test_scanned_page_before_text_still_requires_attachment(monkeypatch):
    pages = [
        SimpleNamespace(get=lambda key: [object()] if key == "/Annots" else None, extract_text=lambda: ""),
        SimpleNamespace(get=lambda _key: None, extract_text=lambda: "Meeting Friday"),
    ]
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=pages))
    assert worker.extract_text(b"%PDF-fake") == ("Meeting Friday", True)


def test_pdf_page_limit_includes_last_allowed_page():
    assert worker.extract_text(make_pdf(page_count=worker.MAX_PDF_PAGES)) == ("", True)


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
