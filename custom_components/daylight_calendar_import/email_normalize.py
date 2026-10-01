"""Normalize RFC email messages into transport-neutral source documents."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from email import policy
from email.errors import ObsoleteHeaderDefect
from email.message import Message
from email.parser import BytesParser
from hashlib import sha256
from html.parser import HTMLParser
import json
import re
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
_SAFE_LINK_PREFIXES = ("http://", "https://", "mailto:")
_MESSAGE_ID_TOKEN_RE = re.compile(
    r'<(?:[^<>"\r\n]|"(?:\\.|[^"\\\r\n])*")+>'
)


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
        if self._skip_stack:
            if tag in _SKIPPED_HTML_TAGS:
                self._skip_stack.append(tag)
            return
        if tag in _SKIPPED_HTML_TAGS:
            self._skip_stack.append(tag)
            return
        if tag == "br" or tag in _BLOCK_TAGS:
            self._parts.append("\n")
        attributes = {name.casefold(): value for name, value in attrs}
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
    message = _parse_message(envelope.raw_message)
    identity = _message_identity(message)
    text = _extract_part_text(message)
    title = _first_header(message, "subject")
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
    return _message_identity(_parse_message(raw_message))


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


def _message_identity(message: Message) -> str:
    headers = message.get_all("message-id", [])
    if len(headers) == 1 and (canonical := _canonical_message_id(headers[0])):
        return canonical
    return _fallback_identity(message)


def _canonical_message_id(header: object) -> str | None:
    """Return only the semantic msg-id token from a usable Message-ID field."""
    defects = getattr(header, "defects", ())
    if any(not isinstance(defect, ObsoleteHeaderDefect) for defect in defects):
        return None
    matches = _MESSAGE_ID_TOKEN_RE.findall(str(header))
    if len(matches) != 1:
        return None
    return _normalize_header_value(matches[0])


def _fallback_identity(message: Message) -> str:
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

    rejected_message_ids = [
        normalized
        for value in message.get_all("message-id", [])
        if (normalized := _normalize_header_value(str(value)))
    ]
    if rejected_message_ids:
        payload["message_id_headers"] = rejected_message_ids

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
    if force_descriptor or _is_attachment_like(part):
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
    return {
        "content_id": _normalize_header_value(part.get("content-id", "")),
        "content_type": part.get_content_type().casefold(),
        "disposition": (part.get_content_disposition() or "").casefold(),
        "filename": _normalize_header_value(part.get_filename() or ""),
        "sha256": sha256(payload).hexdigest(),
        "size": len(payload),
    }


def _descriptor_payload_bytes(part: Message) -> bytes:
    if part.is_multipart():
        return part.as_bytes(policy=policy.default)
    return _decoded_part_bytes(part)


def _extract_part_text(part: Message) -> str:
    text, _ = _extract_part_result(part)
    return text


def _extract_part_result(part: Message) -> tuple[str, tuple[Message, ...]]:
    if _is_attachment_like(part):
        return "", ()
    if not part.is_multipart():
        if not _is_body_text_part(part):
            return "", ()
        return _render_text_part(part), (part,)

    children = cast(list[Message], part.get_payload())
    subtype = part.get_content_subtype().casefold()
    if subtype == "related":
        root = _related_root(part, children)
        return _extract_part_result(root) if root is not None else ("", ())

    candidates = [
        (child.get_content_type().casefold(), *_extract_part_result(child))
        for child in children
    ]
    if subtype == "alternative":
        for preferred_type in ("text/plain", "text/html"):
            for content_type, text, leaves in candidates:
                if content_type == preferred_type and text:
                    return text, leaves
        for _, text, leaves in candidates:
            if text:
                return text, leaves
        return "", ()

    texts = [text for _, text, _ in candidates if text]
    leaves = tuple(
        leaf
        for _, text, candidate_leaves in candidates
        if text
        for leaf in candidate_leaves
    )
    return _join_text(texts), leaves


def _related_root(part: Message, children: list[Message]) -> Message | None:
    if not children:
        return None
    start = _normalize_header_value(
        part.get_param("start", header="content-type") or ""
    )
    if start:
        for child in children:
            if _normalize_header_value(child.get("content-id", "")) == start:
                return child
    return children[0]


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


def _lossy_body_part_descriptors(message: Message) -> list[dict[str, object]]:
    _, leaves = _extract_part_result(message)
    descriptors: list[dict[str, object]] = []
    for part in leaves:
        _, lossy, charset, payload = _decode_text_payload(part)
        if lossy:
            descriptors.append(
                {
                    "charset": charset,
                    "content_type": part.get_content_type().casefold(),
                    "sha256": sha256(payload).hexdigest(),
                    "size": len(payload),
                }
            )
    return descriptors


def _decode_format_flowed(value: str, *, delsp: bool) -> str:
    """Reconstruct RFC 3676 format=flowed logical lines before normalization."""
    lines = value.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    logical: list[str] = []
    pending_depth: int | None = None
    pending_text = ""
    pending_flowed = False

    for raw_line in lines:
        if raw_line.startswith(" "):
            raw_line = raw_line[1:]
        quote_depth = len(raw_line) - len(raw_line.lstrip(">"))
        content = raw_line[quote_depth:]
        flowed = content.endswith(" ") and content != "-- "
        piece = content[:-1] if flowed and delsp else content

        if pending_depth is None:
            pending_depth = quote_depth
            pending_text = piece
            pending_flowed = flowed
            continue

        if pending_flowed and pending_depth == quote_depth:
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
