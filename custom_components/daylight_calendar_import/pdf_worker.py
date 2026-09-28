"""Isolated PDF text extraction; executable without importing Home Assistant."""

from __future__ import annotations

from io import BytesIO
import json
import sys
import zlib

from pypdf import PdfReader
from pypdf.errors import LimitReachedError, PdfReadError
from pypdf.generic import ContentStream, DecodedStreamObject, NullObject

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
NON_RENDERING_OPERATORS = frozenset({
    b"q", b"Q", b"cm", b"BT", b"ET", b"Tf", b"Td", b"TD", b"Tm", b"T*",
    b"Tc", b"Tw", b"Tz", b"TL", b"Ts", b"Tr", b"w", b"J", b"j", b"M",
    b"d", b"ri", b"i", b"gs", b"CS", b"cs", b"SC", b"sc", b"SCN",
    b"scn", b"g", b"G", b"rg", b"RG", b"k", b"K", b"re", b"m", b"l",
    b"c", b"v", b"y", b"h", b"W", b"W*",
    b"n", b"BX", b"EX", b"MP", b"DP", b"BMC", b"BDC", b"EMC",
})


class PdfExtractionError(ValueError):
    """A stable validation category returned by the child process."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _bounded_text_page(page: object) -> tuple[bool, bool]:
    """Return whether text extraction is safe and whether the page has content."""
    annotations = page.get("/Annots") or []
    if hasattr(annotations, "get_object"):
        annotations = annotations.get_object()
    if isinstance(annotations, NullObject):
        annotations = []
    for annotation in annotations:
        item = annotation.get_object()
        if isinstance(item, NullObject):
            continue
        subtype = item.get("/Subtype")
        appearance = item.get("/AP")
        contents = item.get("/Contents")
        if hasattr(subtype, "get_object"):
            subtype = subtype.get_object()
        if hasattr(appearance, "get_object"):
            appearance = appearance.get_object()
        if hasattr(contents, "get_object"):
            contents = contents.get_object()
        # Plain hyperlinks add no content; form values and annotation appearances
        # are unavailable to page.extract_text().
        if (subtype != "/Link"
                or (appearance and not isinstance(appearance, NullObject))
                or (contents and not isinstance(contents, NullObject))):
            return False, True
    contents = page.get("/Contents")
    if contents is None:
        return True, False
    resolved = contents.get_object()
    if isinstance(resolved, NullObject):
        return True, False
    streams = resolved if isinstance(resolved, list) else [resolved]
    if len(streams) > 100:
        return False, True
    total = 0
    decoded_streams = []
    for item in streams:
        stream = item.get_object()
        if isinstance(stream, NullObject):
            continue
        raw = stream._data
        if len(raw) > MAX_PDF_PAGE_CONTENT_BYTES:
            return False, True
        encoding = stream.get("/Filter")
        if hasattr(encoding, "get_object"):
            encoding = encoding.get_object()
        if isinstance(encoding, list):
            if not encoding:
                encoding = None
            elif len(encoding) == 1:
                encoding = encoding[0]
        if hasattr(encoding, "get_object"):
            encoding = encoding.get_object()
        if isinstance(encoding, NullObject):
            encoding = None
        if encoding is None:
            decoded = raw
        elif encoding in ("/FlateDecode", "/Fl"):
            params = stream.get("/DecodeParms")
            if hasattr(params, "get_object"):
                params = params.get_object()
            if isinstance(params, list):
                if not params:
                    params = None
                elif len(params) == 1:
                    params = params[0]
            if hasattr(params, "get_object"):
                params = params.get_object()
            if params is not None and not isinstance(params, NullObject):
                predictor = params.get("/Predictor", 1) if isinstance(params, dict) else None
                if hasattr(predictor, "get_object"):
                    predictor = predictor.get_object()
                if isinstance(predictor, NullObject):
                    predictor = 1
                if predictor != 1:
                    return False, True
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
    def renders(operands: object, operator: bytes) -> bool:
        if operator in NON_RENDERING_OPERATORS:
            return False
        if operator in (b"Tj", b"'", b'"') and operands:
            value = operands[-1]
            return bool(value.strip()) if isinstance(value, (str, bytes)) else bool(value)
        if operator == b"TJ" and operands:
            return any(isinstance(part, (str, bytes)) and bool(part.strip()) for part in operands[0])
        return True
    return (not any(operator in VISUAL_OPERATORS for _, operator in operations),
            any(renders(operands, operator) for operands, operator in operations))


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
