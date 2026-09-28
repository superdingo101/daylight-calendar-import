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
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, EncodedStreamObject, NameObject, NullObject, RectangleObject, TextStringObject
from homeassistant.exceptions import ServiceValidationError

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
        self.data = {}

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


@pytest.mark.parametrize("content", [b"", b"% blank page\n", b"q Q", b"BT ET",
                                     b"0 g", b"1 G", b"1 0 0 rg", b"1 0 0 RG",
                                     b"0 0 0 1 k", b"0 0 0 1 K", b"BT () Tj ET",
                                     b"BT [] TJ ET", b"BT [() 120 ()] TJ ET",
                                     b"BT () ' ET", b'BT 0 0 () " ET'])
def test_empty_content_stream_in_text_pdf_needs_no_attachment(content):
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(make_pdf("Meeting Friday"))))
    blank = writer.add_blank_page(width=300, height=300)
    stream = DecodedStreamObject()
    stream.set_data(content)
    blank[NameObject("/Contents")] = writer._add_object(stream)
    output = BytesIO()
    writer.write(output)
    assert worker.extract_text(output.getvalue()) == ("Meeting Friday", False)


@pytest.mark.parametrize("content", [b"BT [(Meeting)] TJ ET", b"BT (Meeting) ' ET",
                                     b'BT 0 0 (Meeting) " ET', b"BT Tj ET", b"BT TJ ET"])
def test_text_show_content_is_not_mistaken_for_blank(content):
    stream = DecodedStreamObject()
    stream.set_data(content)
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None)
    assert worker._bounded_text_page(page) == (True, True)


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
    stream = DecodedStreamObject()
    stream.set_data(b"BT (Cover) Tj ET /Im0 Do")
    page = SimpleNamespace(get=lambda key: {"/XObject": {"/Im0": object()}} if key == "/Resources" else
                           stream if key == "/Contents" else None,
                           extract_text=lambda: pytest.fail("image content reached text-only path"))
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    assert worker.extract_text(b"%PDF-fake") == ("", True)


def test_unused_indirect_xobject_resource_does_not_force_attachment():
    resources = SimpleNamespace(get_object=lambda: {"/XObject": {"/Im0": object()}})
    page = SimpleNamespace(get=lambda key: resources if key == "/Resources" else None)
    assert worker._bounded_text_page(page) == (True, False)


def test_unused_xobject_resource_allows_text_layer(monkeypatch):
    stream = DecodedStreamObject()
    stream.set_data(b"BT (Meeting) Tj ET")
    page = SimpleNamespace(get=lambda key: {"/XObject": {"/Im0": object()}} if key == "/Resources" else
                           stream if key == "/Contents" else None,
                           extract_text=lambda: "Meeting")
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    assert worker.extract_text(b"%PDF-fake") == ("Meeting", False)


def test_annotated_page_retains_form_values_as_pdf_evidence(monkeypatch):
    widget = DictionaryObject({NameObject("/Subtype"): NameObject("/Widget")})
    page = SimpleNamespace(get=lambda key: [widget] if key == "/Annots" else None,
                           extract_text=lambda: pytest.fail("form values reached text-only path"))
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    assert worker.extract_text(b"%PDF-fake") == ("", True)


@pytest.mark.parametrize("extra", [{}, {"/AP": "appearance"}, {"/Contents": "Schedule note"},
                                   {"/AP": NullObject()}, {"/Contents": NullObject()}])
def test_link_annotations_only_fallback_when_they_contain_content(monkeypatch, extra):
    annotation = DictionaryObject({NameObject("/Subtype"): NameObject("/Link")})
    for key, value in extra.items():
        annotation[NameObject(key)] = value if isinstance(value, NullObject) else TextStringObject(value)
    stream = DecodedStreamObject()
    stream.set_data(b"BT (Meeting Friday) Tj ET")
    page = SimpleNamespace(get=lambda key: [annotation] if key == "/Annots" else
                           stream if key == "/Contents" else None,
                           extract_text=lambda: "Meeting Friday")
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    requires_attachment = any(not isinstance(value, NullObject) for value in extra.values())
    assert worker.extract_text(b"%PDF-fake") == (("", True) if requires_attachment else ("Meeting Friday", False))


def test_indirect_link_annotations_allow_text_extraction():
    annotation = DictionaryObject({NameObject("/Subtype"): NameObject("/Link")})
    annotations = SimpleNamespace(get_object=lambda: [annotation])
    page = SimpleNamespace(get=lambda key: annotations if key == "/Annots" else None)
    assert worker._bounded_text_page(page) == (True, False)


def test_text_pdf_with_ordinary_hyperlink_needs_no_attachment():
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(make_pdf("Meeting Friday"))))
    writer.add_uri(0, "https://example.com/calendar", RectangleObject((0, 0, 100, 20)))
    output = BytesIO()
    writer.write(output)
    assert worker.extract_text(output.getvalue()) == ("Meeting Friday", False)


@pytest.mark.parametrize("contents", [NullObject(), ArrayObject([NullObject()])])
def test_null_contents_in_text_pdf_is_blank(contents):
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(make_pdf("Meeting Friday"))))
    blank = writer.add_blank_page(width=300, height=300)
    blank[NameObject("/Contents")] = contents
    output = BytesIO()
    writer.write(output)
    assert worker.extract_text(output.getvalue()) == ("Meeting Friday", False)


def test_null_entry_in_content_array_is_ignored():
    stream = DecodedStreamObject()
    stream.set_data(b"BT (Meeting) Tj ET")
    page = SimpleNamespace(get=lambda key: ArrayObject([NullObject(), stream]) if key == "/Contents" else None)
    assert worker._bounded_text_page(page) == (True, True)


@pytest.mark.parametrize("optional_key", ["/Annots", "/Resources"])
def test_null_optional_page_dictionary_is_ignored(optional_key):
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(make_pdf("Meeting Friday"))))
    blank = writer.add_blank_page(width=300, height=300)
    blank[NameObject(optional_key)] = NullObject()
    output = BytesIO()
    writer.write(output)
    assert worker.extract_text(output.getvalue()) == ("Meeting Friday", False)


def test_null_member_in_annotation_array_is_ignored():
    writer = PdfWriter()
    writer.append(PdfReader(BytesIO(make_pdf("Meeting Friday"))))
    blank = writer.add_blank_page(width=300, height=300)
    blank[NameObject("/Annots")] = ArrayObject([NullObject()])
    output = BytesIO()
    writer.write(output)
    assert worker.extract_text(output.getvalue()) == ("Meeting Friday", False)


@pytest.mark.parametrize("operator", [b"BI /W", b"BI/W"])
def test_inline_image_operator_retains_pdf_attachment(monkeypatch, operator):
    stream = DecodedStreamObject()
    stream.set_data(b"BT (Cover text) Tj ET " + operator + b" 1/H 1 ID x EI")
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None,
                           extract_text=lambda: pytest.fail("inline image reached text-only path"))
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    assert worker.extract_text(b"%PDF-fake") == ("", True)


def test_bi_inside_text_string_does_not_require_pdf_attachment(monkeypatch):
    stream = DecodedStreamObject()
    stream.set_data(b"BT (BI meeting on Friday) Tj ET % BI is only a comment\n")
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None,
                           extract_text=lambda: "BI meeting on Friday")
    monkeypatch.setattr(worker, "PdfReader", lambda *_args, **_kwargs: SimpleNamespace(is_encrypted=False, pages=[page]))
    assert worker.extract_text(b"%PDF-fake") == ("BI meeting on Friday", False)


def test_vector_painting_with_text_retains_pdf_attachment(monkeypatch):
    stream = DecodedStreamObject()
    stream.set_data(b"BT (Cover text) Tj ET 0 0 100 100 re f")
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None,
                           extract_text=lambda: pytest.fail("visual content reached text-only path"))
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


async def test_pdf_workers_are_serialized_before_executor_scheduling(monkeypatch):
    first_entered, second_started, release = (asyncio.Event() for _ in range(3))
    scheduled = 0
    monkeypatch.setattr(pdfs, "_stage_pdf", lambda *_args: (object(), None))
    async def executor(func, *args):
        nonlocal scheduled
        assert func is pdfs._stage_pdf
        scheduled += 1
        if scheduled == 1:
            first_entered.set()
            await release.wait()
        return func(*args)
    hass = SimpleNamespace(config=SimpleNamespace(media_dirs={}), data={}, async_add_executor_job=executor)
    async def consume(second=False):
        if second:
            second_started.set()
        async with pdfs.async_pdf_source(hass, "a" * 32):
            pass
    one = asyncio.create_task(consume())
    await asyncio.wait_for(first_entered.wait(), 2)
    two = asyncio.create_task(consume(second=True))
    try:
        await asyncio.wait_for(second_started.wait(), 2)
        await asyncio.sleep(0)
        assert scheduled == 1
    finally:
        release.set()
    await asyncio.wait_for(asyncio.gather(one, two), 2)
    assert scheduled == 2


async def test_cancelled_pdf_waiter_consumes_upload_before_staging(monkeypatch, uploaded_file):
    uploaded_file.write_bytes(make_pdf("Meeting"))
    first_entered, release = asyncio.Event(), asyncio.Event()
    staged = 0
    monkeypatch.setattr(pdfs, "_stage_pdf", lambda *_args: (object(), None))
    async def executor(func, *args):
        nonlocal staged
        if func is pdfs._stage_pdf:
            staged += 1
            first_entered.set()
            await release.wait()
            return func(*args)
        return await asyncio.to_thread(func, *args)
    hass = SimpleNamespace(config=SimpleNamespace(media_dirs={}), data={}, async_add_executor_job=executor)
    async def consume():
        async with pdfs.async_pdf_source(hass, "a" * 32):
            pass
    first = asyncio.create_task(consume())
    await asyncio.wait_for(first_entered.wait(), 2)
    waiting = asyncio.create_task(consume())
    try:
        await asyncio.sleep(0)
        waiting.cancel()
        with pytest.raises(asyncio.CancelledError):
            await waiting
        assert not uploaded_file.exists()
        assert staged == 1
    finally:
        release.set()
    await asyncio.wait_for(first, 2)


def test_pdf_worker_discards_stderr(monkeypatch):
    def run(*_args, **kwargs):
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.DEVNULL
        return SimpleNamespace(returncode=0, stdout=b'{"text":"Meeting","needs_attachment":false}')
    monkeypatch.setattr(pdfs.subprocess, "run", run)
    assert pdfs._extract_pdf_text(b"%PDF-fake") == ("Meeting", False)


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
    assert worker._bounded_text_page(page)[0] is expected


def test_page_with_too_many_content_streams_falls_back():
    streams = [DecodedStreamObject() for _ in range(101)]
    contents = SimpleNamespace(get_object=lambda: streams)
    page = SimpleNamespace(get=lambda key: contents if key == "/Contents" else None)
    assert worker._bounded_text_page(page) == (False, True)


def test_last_allowed_stream_and_exact_page_byte_limit_are_accepted():
    streams = [DecodedStreamObject() for _ in range(100)]
    streams[-1].set_data(b" " * worker.MAX_PDF_PAGE_CONTENT_BYTES)
    contents = SimpleNamespace(get_object=lambda: streams)
    page = SimpleNamespace(get=lambda key: contents if key == "/Contents" else None)
    assert worker._bounded_text_page(page) == (True, False)


def test_page_content_limit_applies_across_multiple_streams():
    streams = [DecodedStreamObject() for _ in range(2)]
    streams[0].set_data(b"x" * (worker.MAX_PDF_PAGE_CONTENT_BYTES // 2 + 1))
    streams[1].set_data(b"y" * (worker.MAX_PDF_PAGE_CONTENT_BYTES // 2))
    contents = SimpleNamespace(get_object=lambda: streams)
    page = SimpleNamespace(get=lambda key: contents if key == "/Contents" else None)
    assert worker._bounded_text_page(page) == (False, True)


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
    assert worker._bounded_text_page(page) == (True, True)


def test_singleton_flate_filter_array_extracts_text_without_attachment():
    stream = EncodedStreamObject()
    stream._data = zlib.compress(b"BT (Meeting) Tj ET")
    stream[NameObject("/Filter")] = ArrayObject([NameObject("/FlateDecode")])
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None,
                           extract_text=lambda: "Meeting")
    assert worker._bounded_text_page(page) == (True, True)


@pytest.mark.parametrize("filter_value", [NullObject(), ArrayObject([NullObject()])])
def test_null_filter_is_treated_as_unfiltered_content(filter_value):
    stream = DecodedStreamObject()
    stream.set_data(b"BT (Meeting) Tj ET")
    stream[NameObject("/Filter")] = filter_value
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None)
    assert worker._bounded_text_page(page) == (True, True)


@pytest.mark.parametrize(("params", "safe"), [
    (DictionaryObject({NameObject("/Predictor"): 12}), False),
    (DictionaryObject({NameObject("/Predictor"): 1}), True),
    (DictionaryObject({NameObject("/Predictor"): NullObject()}), True),
    (ArrayObject([DictionaryObject({NameObject("/Predictor"): 12})]), False),
    (NullObject(), True),
    (ArrayObject([]), False),
])
def test_flate_predictor_does_not_bypass_visual_preflight(params, safe):
    stream = EncodedStreamObject()
    stream._data = zlib.compress(b"BT (Meeting) Tj ET")
    stream[NameObject("/Filter")] = NameObject("/FlateDecode")
    stream[NameObject("/DecodeParms")] = params
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None)
    assert worker._bounded_text_page(page) == ((True, True) if safe else (False, True))


def test_unsupported_filter_is_not_decoded_even_if_bytes_are_valid_zlib():
    stream = EncodedStreamObject()
    stream._data = zlib.compress(b"Meeting")
    stream[NameObject("/Filter")] = NameObject("/ASCII85Decode")
    page = SimpleNamespace(get=lambda key: stream if key == "/Contents" else None)
    assert worker._bounded_text_page(page) == (False, True)


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
    assert isinstance(caught.value, ServiceValidationError)
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
        SimpleNamespace(get=lambda key: [DictionaryObject({NameObject("/Subtype"): NameObject("/Widget")})] if key == "/Annots" else None, extract_text=lambda: ""),
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
