"""Normalize RFC email messages into transport-neutral source documents."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from email import policy
from email._header_value_parser import get_msg_id
from email.errors import ObsoleteHeaderDefect
from email.message import Message
from email.parser import BytesParser
from hashlib import sha256
from html.parser import HTMLParser
import base64
import json
from typing import cast
from uuid import uuid4

from .email_source import EmailEnvelope
from .sources import SourceDocument, SourceKind

FALLBACK_IDENTITY_VERSION = "v1"
FALLBACK_IDENTITY_PREFIX = f"email-fallback:{FALLBACK_IDENTITY_VERSION}:"

_IDENTITY_HEADERS = (
    "date",
    "from",
    "sender",
    "reply-to",
    "to",
    "cc",
    "subject",
)
_BODY_CONTENT_TYPES = frozenset({"text/plain", "text/html"})
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "aside",
        "blockquote",
        "div",
        "footer",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "header",
        "hr",
        "li",
        "main",
        "nav",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_SKIPPED_HTML_TAGS = frozenset({"script", "style", "template"})
_VOID_HTML_TAGS = frozenset(
    {"area", "base", "br", "col", "embed", "hr", "img", "input", "link", "meta", "source", "track", "wbr"}
)
_SAFE_LINK_PREFIXES = ("http://", "https://", "mailto:")


class EmailNormalizationError(ValueError):
    """An RFC email message could not be normalized safely."""


class _HTMLTextExtractor(HTMLParser):
    """Conservatively extract useful visible text without fetching resources."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_stack: list[str] = []

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.casefold()
        attributes = {name.casefold(): value for name, value in attrs}
        if self._skip_stack:
            if tag not in _VOID_HTML_TAGS:
                self._skip_stack.append(tag)
            return
        if tag in _SKIPPED_HTML_TAGS or "hidden" in attributes:
            if tag not in _VOID_HTML_TAGS:
                self._skip_stack.append(tag)
            return
        if tag == "br" or tag in _BLOCK_TAGS:
            self._parts.append("\n")
        if tag == "a":
            href = (attributes.get("href") or "").strip()
            if href.casefold().startswith(_SAFE_LINK_PREFIXES):
                self._parts.extend((" ", href, " "))
        elif tag == "img":
            alt = (attributes.get("alt") or "").strip()
            if alt:
                self._parts.extend((" ", alt, " "))

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if self._skip_stack:
            if tag == self._skip_stack[-1]:
                self._skip_stack.pop()
            return
        if tag in _SKIPPED_HTML_TAGS:
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_stack:
            self._parts.append(data)

    def text(self) -> str:
        """Return whitespace-normalized extracted text."""
        return _normalize_body_text("".join(self._parts))


def normalize_email(
    envelope: EmailEnvelope,
    *,
    document_id_factory: Callable[[], str] | None = None,
) -> SourceDocument:
    """Normalize one raw email envelope without exposing transport state downstream."""
    try:
        message = _parse_message(envelope.raw_message)
        identity = _message_identity(message, envelope.raw_message)
        text = _extract_part_text(message)
        title = _first_header(message, "subject")
    except EmailNormalizationError:
        raise
    except Exception as exc:
        raise EmailNormalizationError("Email message could not be normalized") from exc

    make_id = document_id_factory or (lambda: str(uuid4()))
    return SourceDocument(
        id=make_id(),
        kind=SourceKind.EMAIL,
        received_at=envelope.received_at,
        text=text or None,
        title=title or None,
        upstream_source_id=identity,
    )


def stable_email_identity(raw_message: bytes) -> str:
    """Return the semantic source identity used for pre-AI deduplication."""
    try:
        return _message_identity(_parse_message(raw_message), raw_message)
    except EmailNormalizationError:
        raise
    except Exception as exc:
        raise EmailNormalizationError("Email message could not be normalized") from exc


def html_to_text(value: str) -> str:
    """Convert email HTML to useful text without executing or fetching content."""
    parser = _HTMLTextExtractor()
    parser.feed(value)
    parser.close()
    return parser.text()


def _parse_message(raw_message: bytes) -> Message:
    try:
        return BytesParser(policy=policy.default).parsebytes(raw_message)
    except Exception as exc:
        raise EmailNormalizationError("Email message could not be parsed") from exc


def _message_identity(message: Message, raw_message: bytes) -> str:
    raw_message_ids = _raw_header_values(raw_message, b"message-id")
    if len(raw_message_ids) == 1:
        canonical = _canonical_msg_id_bytes(raw_message_ids[0])
        if canonical is not None:
            return canonical
    return _fallback_identity(
        message,
        raw_message=raw_message,
        raw_message_ids=raw_message_ids,
    )


def _canonical_msg_id_bytes(value: bytes) -> str | None:
    """Parse one raw msg-id field value and return its semantic token."""
    try:
        text = _unfold_header_value(value).decode("ascii", errors="surrogateescape")
        token, remainder = get_msg_id(text)
    except Exception:
        return None
    if remainder.strip() or not _msg_id_defects_are_acceptable(token):
        return None
    return _semantic_msg_id_token(token)


def _canonical_msg_id_text(value: object) -> str | None:
    try:
        token, remainder = get_msg_id(str(value))
    except Exception:
        return None
    if remainder.strip() or not _msg_id_defects_are_acceptable(token):
        return None
    return _semantic_msg_id_token(token)


def _msg_id_defects_are_acceptable(token: object) -> bool:
    return all(
        isinstance(defect, ObsoleteHeaderDefect)
        for defect in getattr(token, "all_defects", ())
    )


def _semantic_msg_id_token(token: object) -> str | None:
    """Render a parsed msg-id and require nonempty semantic id-left/id-right."""
    def render(node: object) -> str:
        token_type = getattr(node, "token_type", "")
        if token_type in {"cfws", "comment", "msg-id-start", "msg-id-end"}:
            return ""
        if token_type == "bare-quoted-string":
            return str(node)
        if isinstance(node, list):
            return "".join(render(child) for child in node)
        return str(node)

    children = list(token) if isinstance(token, list) else []
    at_indexes = [
        index
        for index, child in enumerate(children)
        if getattr(child, "token_type", "") == "address-at-symbol"
    ]
    if len(at_indexes) != 1:
        return None
    at_index = at_indexes[0]
    left = "".join(render(child) for child in children[:at_index])
    right = "".join(render(child) for child in children[at_index + 1 :])
    if not left or not right:
        return None
    return f"<{left}@{right}>"


def _raw_header_values(raw_message: bytes, name: bytes) -> tuple[bytes, ...]:
    """Return raw field values while scanning only the RFC header block."""
    values: list[bytes] = []
    current_name: bytes | None = None
    current_value = bytearray()

    def flush() -> None:
        nonlocal current_name, current_value
        if current_name == name:
            values.append(bytes(current_value))
        current_name = None
        current_value = bytearray()

    for line in _iter_header_lines(raw_message):
        if line == b"":
            flush()
            break
        if line[:1] in (b" ", b"\t"):
            if current_name is None:
                flush()
                break
            current_value.extend(b"\n")
            current_value.extend(line)
            continue
        flush()
        field_name, separator, field_value = line.partition(b":")
        if not separator or not _is_valid_field_name(field_name):
            break
        current_name = field_name.lower()
        current_value.extend(field_value.lstrip(b" \t"))
    else:
        flush()

    return tuple(values)


def _is_valid_field_name(value: bytes) -> bool:
    return bool(value) and all(
        33 <= byte <= 126 and byte != ord(":")
        for byte in value
    )


def _iter_header_lines(raw_message: bytes) -> Iterable[bytes]:
    """Yield header lines in one linear pass and stop at the blank separator."""
    line_start = 0
    position = 0
    length = len(raw_message)
    while position < length:
        byte = raw_message[position]
        if byte not in (10, 13):
            position += 1
            continue

        line = raw_message[line_start:position]
        yield line
        if not line:
            return

        if byte == 13 and position + 1 < length and raw_message[position + 1] == 10:
            position += 2
        else:
            position += 1
        line_start = position

    if line_start < length:
        yield raw_message[line_start:]


def _unfold_header_value(value: bytes) -> bytes:
    lines = value.split(b"\n")
    return b" ".join(line.lstrip(b" \t") for line in lines)


def _fallback_identity(
    message: Message,
    *,
    raw_message: bytes,
    raw_message_ids: tuple[bytes, ...] = (),
) -> str:
    payload: dict[str, object] = {
        "headers": {
            name: [
                normalized
                for value in message.get_all(name, [])
                if (normalized := _normalize_header_value(str(value)))
            ]
            for name in _IDENTITY_HEADERS
        },
        "body": _extract_part_text(message),
        "non_body_parts": _non_body_part_descriptors(message),
    }

    if raw_message_ids:
        payload["message_id_headers"] = [
            base64.b64encode(_unfold_header_value(value)).decode("ascii")
            for value in raw_message_ids
        ]

    lossy_identity_headers = _lossy_identity_header_descriptors(
        message,
        raw_message,
    )
    if lossy_identity_headers:
        payload["lossy_identity_headers"] = lossy_identity_headers

    lossy_body_parts = _lossy_body_part_descriptors(message)
    if lossy_body_parts:
        payload["lossy_body_parts"] = lossy_body_parts

    serialized = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    digest = sha256(serialized.encode("utf-8")).hexdigest()
    return f"{FALLBACK_IDENTITY_PREFIX}{digest}"


def _lossy_identity_header_descriptors(
    message: Message,
    raw_message: bytes,
) -> dict[str, list[str]]:
    descriptors: dict[str, list[str]] = {}
    for name in _IDENTITY_HEADERS:
        header_values = list(message.get_all(name, []))
        normalized_values = [
            _normalize_header_value(str(value))
            for value in header_values
        ]
        defective = any(
            getattr(value, "defects", ()) or getattr(value, "all_defects", ())
            for value in header_values
        )
        if not defective and not any("\ufffd" in value for value in normalized_values):
            continue
        raw_values = _raw_header_values(raw_message, name.encode("ascii"))
        if raw_values:
            descriptors[name] = [
                base64.b64encode(_unfold_header_value(value)).decode("ascii")
                for value in raw_values
            ]
    return descriptors


def _non_body_part_descriptors(message: Message) -> list[dict[str, object]]:
    descriptors: list[dict[str, object]] = []
    _collect_non_body_part_descriptors(message, descriptors)
    return descriptors


def _collect_non_body_part_descriptors(
    part: Message,
    descriptors: list[dict[str, object]],
    *,
    force_descriptor: bool = False,
) -> None:
    if force_descriptor or _is_attachment_like(part) or _is_encapsulated_message(part):
        descriptors.append(_part_descriptor(part))
        return

    if part.is_multipart():
        children = cast(list[Message], part.get_payload())
        if part.get_content_subtype().casefold() == "related":
            root = _related_root(part, children)
            for child in children:
                _collect_non_body_part_descriptors(
                    child,
                    descriptors,
                    force_descriptor=root is not None and child is not root,
                )
            return
        for child in children:
            _collect_non_body_part_descriptors(child, descriptors)
        return

    if not _is_body_text_part(part):
        descriptors.append(_part_descriptor(part))


def _part_descriptor(part: Message) -> dict[str, object]:
    payload = _descriptor_payload_bytes(part)
    filename = part.get_filename() or ""
    descriptor: dict[str, object] = {
        "content_id": _canonical_or_raw_msg_id_header(part, "content-id"),
        "content_type": part.get_content_type().casefold(),
        "disposition": (part.get_content_disposition() or "").casefold(),
        "filename": filename,
        "sha256": sha256(payload).hexdigest(),
        "size": len(payload),
    }
    if _filename_decoding_is_lossy(part, filename):
        raw_filename_parameters = _raw_filename_parameter_descriptors(part)
        if raw_filename_parameters:
            descriptor["filename_raw_parameters"] = raw_filename_parameters
    if (transfer := _transfer_decode_descriptor(part)) is not None:
        descriptor["transfer_wire"] = transfer
    return descriptor


def _filename_decoding_is_lossy(part: Message, filename: str) -> bool:
    if "\ufffd" in filename:
        return True
    for name in ("content-disposition", "content-type"):
        for header in part.get_all(name, []):
            if getattr(header, "defects", ()) or getattr(header, "all_defects", ()):
                return True
    return False


def _raw_filename_parameter_descriptors(part: Message) -> list[str]:
    """Return folding/spacing-insensitive raw filename parameter values."""
    descriptors: list[str] = []
    for header_name, value in part.raw_items():
        lower_name = header_name.casefold()
        parameter_prefix = (
            "filename" if lower_name == "content-disposition"
            else "name" if lower_name == "content-type"
            else None
        )
        if parameter_prefix is None:
            continue
        raw = _unfold_header_value(
            value.encode("utf-8", errors="surrogateescape")
        )
        for parameter in _split_mime_parameters(raw)[1:]:
            key, separator, raw_value = parameter.partition(b"=")
            canonical_key = key.strip().lower()
            if not separator or not (
                canonical_key == parameter_prefix.encode("ascii")
                or canonical_key.startswith(
                    parameter_prefix.encode("ascii") + b"*"
                )
            ):
                continue
            descriptors.append(
                canonical_key.decode("ascii", errors="replace")
                + ":"
                + base64.b64encode(raw_value.strip(b" \t")).decode("ascii")
            )
    return descriptors


def _split_mime_parameters(value: bytes) -> list[bytes]:
    parts: list[bytes] = []
    start = 0
    quoted = False
    escaped = False
    for index, byte in enumerate(value):
        if escaped:
            escaped = False
            continue
        if quoted and byte == 92:
            escaped = True
            continue
        if byte == 34:
            quoted = not quoted
            continue
        if byte == 59 and not quoted:
            parts.append(value[start:index])
            start = index + 1
    parts.append(value[start:])
    return parts


def _canonical_or_raw_msg_id_header(part: Message, name: str) -> str:
    raw_values = [
        value
        for header_name, value in part.raw_items()
        if header_name.casefold() == name.casefold()
    ]
    if len(raw_values) == 1:
        canonical = _canonical_msg_id_text(raw_values[0])
        if canonical is not None:
            return canonical
    if not raw_values:
        return ""
    encoded = [
        base64.b64encode(
            value.encode("utf-8", errors="surrogateescape")
        ).decode("ascii")
        for value in raw_values
    ]
    return "raw:" + ",".join(encoded)


def _descriptor_payload_bytes(part: Message) -> bytes:
    if part.is_multipart():
        semantic = _semantic_part_fingerprint(part)
        return json.dumps(
            semantic,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    return _decoded_part_bytes(part)


def _semantic_part_fingerprint(part: Message) -> dict[str, object]:
    headers = {
        name.casefold(): [
            _normalize_header_value(str(value))
            for value in part.get_all(name, [])
        ]
        for name in ("date", "from", "sender", "reply-to", "to", "cc", "subject")
        if part.get_all(name, [])
    }
    fingerprint: dict[str, object] = {
        "content_id": _canonical_or_raw_msg_id_header(part, "content-id"),
        "content_type": part.get_content_type().casefold(),
        "disposition": (part.get_content_disposition() or "").casefold(),
        "filename": part.get_filename() or "",
        "headers": headers,
    }
    if part.is_multipart():
        children = cast(list[Message], part.get_payload())
        fingerprint["children"] = [
            _semantic_part_fingerprint(child) for child in children
        ]
        return fingerprint

    payload = _decoded_part_bytes(part)
    fingerprint["sha256"] = sha256(payload).hexdigest()
    fingerprint["size"] = len(payload)
    if (transfer := _transfer_decode_descriptor(part)) is not None:
        fingerprint["transfer_wire"] = transfer
    return fingerprint


def _extract_part_text(part: Message) -> str:
    text, _, _ = _extract_part_result(part)
    return text


def _extract_part_result(
    part: Message,
) -> tuple[str, tuple[Message, ...], str]:
    if _is_attachment_like(part) or _is_encapsulated_message(part):
        return "", (), ""
    if not part.is_multipart():
        if not _is_body_text_part(part):
            return "", (), ""
        content_type = part.get_content_type().casefold()
        return _render_text_part(part), (part,), content_type

    children = cast(list[Message], part.get_payload())
    subtype = part.get_content_subtype().casefold()
    if subtype == "related":
        root = _related_root(part, children)
        return _extract_part_result(root) if root is not None else ("", (), "")

    candidates = [_extract_part_result(child) for child in children]
    if subtype == "alternative":
        for preferred_type in ("text/plain", "text/html"):
            for text, leaves, effective_type in candidates:
                if effective_type == preferred_type and text:
                    return text, leaves, effective_type
        for text, leaves, effective_type in candidates:
            if text:
                return text, leaves, effective_type
        return "", (), ""

    texts = [text for text, _, _ in candidates if text]
    leaves = tuple(
        leaf
        for text, candidate_leaves, _ in candidates
        if text
        for leaf in candidate_leaves
    )
    effective_types = {
        effective_type
        for text, _, effective_type in candidates
        if text and effective_type
    }
    effective_type = next(iter(effective_types)) if len(effective_types) == 1 else ""
    return _join_text(texts), leaves, effective_type


def _related_root(part: Message, children: list[Message]) -> Message | None:
    if not children:
        return None
    start = _canonical_msg_id_text(
        part.get_param("start", header="content-type") or ""
    )
    if start:
        for child in children:
            content_id = _unique_canonical_msg_id_header(child, "content-id")
            if content_id == start:
                return child
    return children[0]


def _unique_canonical_msg_id_header(part: Message, name: str) -> str | None:
    raw_values = [
        value
        for header_name, value in part.raw_items()
        if header_name.casefold() == name.casefold()
    ]
    if len(raw_values) != 1:
        return None
    return _canonical_msg_id_text(raw_values[0])


def _render_text_part(part: Message) -> str:
    content_type = part.get_content_type().casefold()
    text = _decode_text_part(part)
    if content_type == "text/html":
        return html_to_text(text)
    if (part.get_param("format", header="content-type") or "").casefold() == "flowed":
        text = _decode_format_flowed(
            text,
            delsp=(part.get_param("delsp", header="content-type") or "").casefold()
            == "yes",
        )
    return _normalize_body_text(text)


def _is_attachment_like(part: Message) -> bool:
    return (
        (part.get_content_disposition() or "").casefold() == "attachment"
        or part.get_filename() is not None
    )


def _is_encapsulated_message(part: Message) -> bool:
    return part.get_content_maintype().casefold() == "message"


def _is_body_text_part(part: Message) -> bool:
    return (
        part.get_content_type().casefold() in _BODY_CONTENT_TYPES
        and not _is_attachment_like(part)
    )


def _decode_text_part(part: Message) -> str:
    text, _, _, _ = _decode_text_payload(part)
    return text


def _decode_text_payload(part: Message) -> tuple[str, bool, str, bytes]:
    payload = _decoded_part_bytes(part)
    charset = (part.get_content_charset() or "us-ascii").casefold()
    try:
        return payload.decode(charset), False, charset, payload
    except LookupError:
        return payload.decode("utf-8", errors="replace"), True, charset, payload
    except UnicodeDecodeError:
        return payload.decode(charset, errors="replace"), True, charset, payload


def _transfer_decode_descriptor(part: Message) -> dict[str, object] | None:
    defects = getattr(part, "defects", ())
    if not any(
        "base64" in type(defect).__name__.casefold()
        or "quotedprintable" in type(defect).__name__.casefold()
        for defect in defects
    ):
        return None
    raw_payload = part.get_payload(decode=False)
    if not isinstance(raw_payload, str):
        return None
    wire = raw_payload.encode("utf-8", errors="surrogateescape")
    cte = _normalize_header_value(
        part.get("content-transfer-encoding", "")
    ).casefold()
    if cte == "base64":
        wire = bytes(byte for byte in wire if byte not in b" \t\r\n")
    return {
        "encoding": cte,
        "sha256": sha256(wire).hexdigest(),
        "size": len(wire),
    }


def _lossy_body_part_descriptors(message: Message) -> list[dict[str, object]]:
    _, leaves, _ = _extract_part_result(message)
    descriptors: list[dict[str, object]] = []
    for part in leaves:
        _, lossy, charset, payload = _decode_text_payload(part)
        transfer = _transfer_decode_descriptor(part)
        if lossy or transfer is not None:
            descriptor: dict[str, object] = {
                "charset": charset,
                "content_type": part.get_content_type().casefold(),
                "sha256": sha256(payload).hexdigest(),
                "size": len(payload),
            }
            if transfer is not None:
                descriptor["transfer_wire"] = transfer
            descriptors.append(descriptor)
    return descriptors


def _decode_format_flowed(value: str, *, delsp: bool) -> str:
    """Reconstruct RFC 3676 format=flowed logical lines before normalization."""
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    logical: list[str] = []
    pending_depth: int | None = None
    pending_text = ""
    pending_flowed = False

    for raw_line in lines:
        quote_depth = len(raw_line) - len(raw_line.lstrip(">"))
        content = raw_line[quote_depth:]
        if content.startswith(" "):
            content = content[1:]
        signature_separator = content == "-- "
        flowed = content.endswith(" ") and not signature_separator
        piece = content[:-1] if flowed and delsp else content

        if pending_depth is None:
            pending_depth = quote_depth
            pending_text = piece
            pending_flowed = flowed
            continue

        if (
            not signature_separator
            and pending_flowed
            and pending_depth == quote_depth
        ):
            pending_text += piece
            pending_flowed = flowed
            continue

        logical.append(">" * pending_depth + pending_text)
        pending_depth = quote_depth
        pending_text = piece
        pending_flowed = flowed

    logical.append(">" * (pending_depth or 0) + pending_text)
    return "\n".join(logical)


def _decoded_part_bytes(part: Message) -> bytes:
    payload = part.get_payload(decode=True)
    if isinstance(payload, bytes):
        return payload
    raw_payload = part.get_payload(decode=False)
    if isinstance(raw_payload, str):
        return raw_payload.encode("utf-8", errors="replace")
    return part.as_bytes(policy=policy.default)


def _first_header(message: Message, name: str) -> str:
    values = message.get_all(name, [])
    if not values:
        return ""
    return _normalize_header_value(str(values[0]))


def _normalize_header_value(value: object) -> str:
    return " ".join(str(value).split())


def _normalize_body_text(value: str) -> str:
    value = value.replace("\r\n", "\n").replace("\r", "\n")
    return "\n".join(
        line
        for raw_line in value.split("\n")
        if (line := " ".join(raw_line.split()))
    ).strip()


def _join_text(values: Iterable[str]) -> str:
    return "\n".join(value for value in values if value).strip()
