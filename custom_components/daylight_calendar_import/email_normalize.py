"""Normalize RFC email messages into transport-neutral source documents.

Identity is intentionally conservative: a supplied upstream identity wins, then one
valid RFC Message-ID, then a SHA-256 of the exact RFC wire bytes. The raw fallback
is not a semantic MIME fingerprint; differently encoded or malformed wire messages
are intentionally allowed to receive different fallback identities.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from email import policy
from email._header_value_parser import get_msg_id
from email.message import Message
from email.parser import BytesParser
from hashlib import sha256
from html.parser import HTMLParser
from typing import cast
from uuid import uuid4

from .email_source import EmailEnvelope
from .sources import SourceDocument, SourceKind

FALLBACK_IDENTITY_VERSION = "v1"
FALLBACK_IDENTITY_PREFIX = f"email-fallback:{FALLBACK_IDENTITY_VERSION}:"

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
    {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "source",
        "track",
        "wbr",
    }
)
_SAFE_LINK_PREFIXES = ("http://", "https://", "mailto:")


class EmailNormalizationError(ValueError):
    """An RFC email message could not be normalized safely."""


class _HTMLTextExtractor(HTMLParser):
    """Extract bounded visible email text without executing or fetching content."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_stack: list[str] = []

    def handle_starttag(
        self,
        tag: str,
        attrs: list[tuple[str, str | None]],
    ) -> None:
        tag = tag.casefold()
        attributes = {name.casefold(): value for name, value in attrs}

        if self._skip_stack:
            if tag not in _VOID_HTML_TAGS:
                self._skip_stack.append(tag)
            return

        style = attributes.get("style") or ""
        if (
            tag in _SKIPPED_HTML_TAGS
            or "hidden" in attributes
            or _inline_style_hides(style)
        ):
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
        if tag in _BLOCK_TAGS:
            self._parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip_stack:
            self._parts.append(data)

    def text(self) -> str:
        """Return normalized visible text."""
        return _normalize_body_text("".join(self._parts))


def normalize_email(
    envelope: EmailEnvelope,
    *,
    document_id_factory: Callable[[], str] | None = None,
) -> SourceDocument:
    """Normalize one email envelope without exposing transport provenance downstream."""
    message = _parse_message(envelope.raw_message)
    identity = (
        envelope.upstream_source_id
        if envelope.upstream_source_id not in (None, "")
        else _message_identity(message, envelope.raw_message)
    )
    _, text = _extract_body(message)
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
    """Return Message-ID identity or a conservative raw-wire fallback identity."""
    message = _parse_message(raw_message)
    return _message_identity(message, raw_message)


def html_to_text(value: str) -> str:
    """Convert email HTML to bounded visible text without remote access or execution."""
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
    message_id = _valid_message_id(message)
    if message_id is not None:
        return message_id
    return f"{FALLBACK_IDENTITY_PREFIX}{sha256(raw_message).hexdigest()}"


def _valid_message_id(message: Message) -> str | None:
    headers = list(message.get_all("message-id", []))
    if len(headers) != 1:
        return None

    header = headers[0]
    if getattr(header, "defects", ()):
        return None

    try:
        token, remainder = get_msg_id(str(header))
    except Exception:
        return None

    if remainder.strip() or getattr(token, "all_defects", ()):
        return None

    value = str(getattr(token, "value", "")).strip()
    return value or None


def _extract_body(part: Message) -> tuple[bool, str]:
    """Return (supported representation exists, rendered text)."""
    if _is_attachment_like(part) or _is_encapsulated_message(part):
        return False, ""

    if not part.is_multipart():
        if part.get_content_type().casefold() not in _BODY_CONTENT_TYPES:
            return False, ""
        return True, _render_text_part(part)

    children = cast(list[Message], part.get_payload())
    subtype = part.get_content_subtype().casefold()

    if subtype == "related":
        return _extract_body(_related_root(part, children))

    if subtype == "alternative":
        for child in reversed(children):
            supported, text = _extract_body(child)
            if supported:
                return True, text
        return False, ""

    supported = False
    texts: list[str] = []
    for child in children:
        child_supported, text = _extract_body(child)
        supported = supported or child_supported
        if child_supported and text:
            texts.append(text)
    return supported, _join_text(texts)


def _related_root(part: Message, children: list[Message]) -> Message:
    start = part.get_param("start", header="content-type")
    if start is not None:
        target = str(start).strip()
        for child in children:
            content_id = child.get("content-id")
            if content_id is not None and str(content_id).strip() == target:
                return child

    return children[0]


def _render_text_part(part: Message) -> str:
    text = _decode_text_part(part)
    if part.get_content_type().casefold() == "text/html":
        return html_to_text(text)
    return _normalize_body_text(text)


def _decode_text_part(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if not isinstance(payload, bytes):
        raw_payload = part.get_payload(decode=False)
        return raw_payload if isinstance(raw_payload, str) else ""

    charset = (part.get_content_charset() or "us-ascii").casefold()
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("utf-8", errors="replace")


def _is_attachment_like(part: Message) -> bool:
    return (
        (part.get_content_disposition() or "").casefold() == "attachment"
        or part.get_filename() is not None
    )


def _is_encapsulated_message(part: Message) -> bool:
    return part.get_content_maintype().casefold() == "message"


def _inline_style_hides(style: str) -> bool:
    """Recognize only common inline email hiding declarations.

    This is intentionally not a CSS parser or cascade implementation.
    """
    for declaration in style.split(";"):
        name, separator, value = declaration.partition(":")
        if not separator:
            continue
        property_name = name.strip().casefold()
        property_value = value.strip().casefold()
        if property_value.endswith("!important"):
            property_value = property_value[: -len("!important")].strip()
        if (
            property_name == "display"
            and property_value == "none"
            or property_name == "visibility"
            and property_value == "hidden"
        ):
            return True
    return False


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
