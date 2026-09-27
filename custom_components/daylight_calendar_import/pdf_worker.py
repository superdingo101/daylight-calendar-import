"""Isolated PDF text extraction; executable without importing Home Assistant."""

from __future__ import annotations

from io import BytesIO
import json
import sys
import zlib

from pypdf import PdfReader
from pypdf.errors import LimitReachedError, PdfReadError
from pypdf.generic import ContentStream, DecodedStreamObject

MAX_PDF_PAGES = 30
MAX_PDF_TEXT_CHARS = 100_000
MAX_PDF_PAGE_CONTENT_BYTES = 1_000_000
MAX_INPUT_BYTES = 10 * 1024 * 1024
MAX_WORKER_MEMORY_BYTES = 256 * 1024 * 1024
MAX_WORKER_CPU_SECONDS = 10
VISUAL_OPERATORS = frozenset({
    b"INLINE IMAGE", b"Do", b"sh", b"S", b"s", b"f", b"F", b"f*",
    b"B", b"B*", b"b", b"b*",
})


class PdfExtractionError(ValueError):
    """A stable validation category returned by the child process."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _bounded_text_page(page: object) -> tuple[bool, bool]:
    """Return whether text extraction is safe and whether the page has content."""
    if page.get("/Annots"):
        # AcroForm values and appearances do not appear in extracted page text.
        return False, True
    resources = page.get("/Resources") or {}
    if hasattr(resources, "get_object"):
        resources = resources.get_object()
    if resources.get("/XObject"):
        return False, True
    contents = page.get("/Contents")
    if contents is None:
        return True, False
    resolved = contents.get_object()
    streams = resolved if isinstance(resolved, list) else [resolved]
    if len(streams) > 100:
        return False, True
    total = 0
    decoded_streams = []
    for item in streams:
        stream = item.get_object()
        raw = stream._data
        if len(raw) > MAX_PDF_PAGE_CONTENT_BYTES:
            return False, True
        encoding = stream.get("/Filter")
        if encoding is None:
            decoded = raw
        elif encoding in ("/FlateDecode", "/Fl"):
            try:
                decoder = zlib.decompressobj()
                decoded = decoder.decompress(raw, MAX_PDF_PAGE_CONTENT_BYTES + 1)
                if decoder.unconsumed_tail or not decoder.eof:
                    return False, True
            except zlib.error:
                return False, True
        else:
            return False, True
        total += len(decoded)
        if total > MAX_PDF_PAGE_CONTENT_BYTES:
            return False, True
        decoded_streams.append(decoded)
    # ContentStream tokenizes PDF strings/comments separately from operators.
    bounded = DecodedStreamObject()
    bounded.set_data(b"\n".join(decoded_streams))
    operations = ContentStream(bounded, getattr(page, "pdf", None), "bytes").operations
    return not any(operator in VISUAL_OPERATORS for _, operator in operations), bool(operations)


def extract_text(data: bytes) -> tuple[str, bool]:
    """Extract text and report whether any page still needs PDF attachment review."""
    if not data.startswith(b"%PDF-"):
        raise PdfExtractionError("unsupported_media", "Unsupported attachment media type")
    try:
        reader = PdfReader(BytesIO(data), strict=False)
        if reader.is_encrypted:
            raise PdfExtractionError("unsupported_pdf", "Encrypted PDF is not supported")
        if not reader.pages:
            raise PdfExtractionError("invalid_pdf", "PDF has no pages")
        if len(reader.pages) > MAX_PDF_PAGES:
            raise PdfExtractionError("too_many_pages", "PDF exceeds the page limit")
        pages: list[str] = []
        length = 0
        needs_attachment = False
        for page in reader.pages:
            safe, has_content = _bounded_text_page(page)
            text = (page.extract_text() or "") if safe else ""
            needs_attachment |= not text.strip() and (not safe or has_content)
            length += len(text) + (2 if pages else 0)
            if length > MAX_PDF_TEXT_CHARS:
                raise PdfExtractionError("source_too_large", "PDF text exceeds the size limit")
            pages.append(text)
    except (PdfReadError, LimitReachedError) as err:
        raise PdfExtractionError("invalid_pdf", "PDF could not be read") from err
    return "\n\n".join(pages).strip(), needs_attachment


def main() -> None:
    """Apply limits before reading or constructing any pypdf objects."""
    import resource

    resource.setrlimit(resource.RLIMIT_AS, (MAX_WORKER_MEMORY_BYTES, MAX_WORKER_MEMORY_BYTES))
    resource.setrlimit(resource.RLIMIT_CPU, (MAX_WORKER_CPU_SECONDS, MAX_WORKER_CPU_SECONDS))
    try:
        data = sys.stdin.buffer.read(MAX_INPUT_BYTES + 1)
        if len(data) > MAX_INPUT_BYTES:
            raise PdfExtractionError("source_too_large", "Source attachments exceed the size limit")
        text, needs_attachment = extract_text(data)
        result = {"text": text, "needs_attachment": needs_attachment}
    except PdfExtractionError as err:
        result = {"error": err.code, "message": str(err)}
    except MemoryError:
        result = {"error": "source_too_large", "message": "PDF exceeds the extraction resource limit"}
    sys.stdout.write(json.dumps(result))


if __name__ == "__main__":  # pragma: no cover - exercised by the subprocess integration tests
    main()
