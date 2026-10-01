"""Normalize RFC email messages into transport-neutral source documents."""

from __future__ import annotations

from collections.abc import Callable
from email import policy
from email.message import Message
from email.parser import BytesParser
from hashlib import sha256
from html.parser import HTMLParser
import json
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
_SKIPPED_HTML_TAGS = frozenset({"script", "style", "noscript", "template"})
_SAFE_LINK_PREFIXES = ("http://", "https://", "mailto:")


class EmailNormalizationError(ValueError):
    """An RFC email message could not be normalized safely."""


class _HTMLTextExtractor(HTMLParser):
    """Conservatively extract useful visible text without fetching resources."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(
        self, tag: str, attrs: list[tuple[str, str | None]]
    ) -> None:
        tag = tag.casefold()
        if tag in _SKIPPED_HTML_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
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
        if tag in _SKIPPED_HTML_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
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
    message_ids = [
        normalized
        for value in message.get_all("message-id", [])
        if (normalized := _normalize_header_value(str(value)))
    ]
    if len(message_ids) == 1:
        return message_ids[0]
    return _fallback_identity(message)


def _fallback_identity(message: Message) -> str:
    payload = {
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
    for part in message.walk():
        if part.is_multipart():
            continue
        content_type = part.get_content_type().casefold()
        disposition = (part.get_content_disposition() or "").casefold()
        if content_type in _BODY_CONTENT_TYPES and disposition != "attachment":
            continue
        payload = _decoded_part_bytes(part)
        descriptors.append(
            {
                "content_id": _normalize_header_value(part.get("content-id", "")),
                "content_type": content_type,
                "disposition": disposition,
                "filename": _normalize_header_value(part.get_filename() or ""),
                "sha256": sha256(payload).hexdigest(),
                "size": len(payload),
            }
        )
    return descriptors


def _extract_part_text(part: Message) -> str:
    if (part.get_content_disposition() or "").casefold() == "attachment":
        return ""
    if part.is_multipart():
        children = part.get_payload()
        if not isinstance(children, list):
            return ""
        candidates = [
            (child.get_content_type().casefold(), _extract_part_text(child))
            for child in children
        ]
        if part.get_content_subtype().casefold() == "alternative":
            for preferred_type in ("text/plain", "text/html"):
                for content_type, text in candidates:
                    if content_type == preferred_type and text:
                        return text
            return next((text for _, text in candidates if text), "")
        return _join_text(text for _, text in candidates if text)

    content_type = part.get_content_type().casefold()
    if content_type == "text/plain":
        return _normalize_body_text(_decode_text_part(part))
    if content_type == "text/html":
        return html_to_text(_decode_text_part(part))
    return ""


def _decode_text_part(part: Message) -> str:
    try:
        content = part.get_content()
        if isinstance(content, str):
            return content
    except (LookupError, UnicodeError):
        pass

    payload = _decoded_part_bytes(part)
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


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


def _join_text(values: object) -> str:
    return "\n".join(str(value) for value in values if str(value)).strip()
