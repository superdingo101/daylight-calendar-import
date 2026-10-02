from __future__ import annotations

from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
from hashlib import sha256
from uuid import UUID

import pytest

from custom_components.daylight_calendar_import import email_normalize
from custom_components.daylight_calendar_import.email_normalize import (
    EmailNormalizationError,
    FALLBACK_IDENTITY_PREFIX,
    html_to_text,
    normalize_email,
    stable_email_identity,
)
from custom_components.daylight_calendar_import.email_source import (
    DirectImapReference,
    EmailEnvelope,
    EmailProvenance,
    EmailSourceType,
)
from custom_components.daylight_calendar_import.sources import SourceKind


def _envelope(
    raw_message: bytes,
    *,
    upstream_source_id: str | None = None,
) -> EmailEnvelope:
    return EmailEnvelope(
        received_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        raw_message=raw_message,
        provenance=EmailProvenance(
            source_id="primary-email",
            source_type=EmailSourceType.DIRECT_IMAP,
            transport_reference=DirectImapReference(
                mailbox="INBOX",
                uid_validity=10,
                uid=20,
            ),
        ),
        upstream_source_id=upstream_source_id,
    )


def _raw_message(
    body: bytes = b"Friday at 5",
    *,
    subject: bytes = b"Party",
    message_id: bytes | None = b"<party@example.test>",
    extra_headers: bytes = b"",
) -> bytes:
    headers = [b"Subject: " + subject]
    if message_id is not None:
        headers.append(b"Message-ID: " + message_id)
    if extra_headers:
        headers.append(extra_headers.rstrip(b"\r\n"))
    return b"\r\n".join(headers) + b"\r\n\r\n" + body


def test_upstream_identity_wins_over_message_id_and_fallback() -> None:
    source = normalize_email(
        _envelope(
            _raw_message(message_id=b"<message@example.test>"),
            upstream_source_id="transport-key-123",
        ),
        document_id_factory=lambda: "doc-1",
    )

    assert source.id == "doc-1"
    assert source.kind is SourceKind.EMAIL
    assert source.upstream_source_id == "transport-key-123"
    assert source.title == "Party"
    assert source.text == "Friday at 5"


@pytest.mark.parametrize("upstream_source_id", (None, ""))
def test_missing_or_empty_upstream_identity_uses_message_id(
    upstream_source_id: str | None,
) -> None:
    source = normalize_email(
        _envelope(
            _raw_message(message_id=b"<message@example.test>"),
            upstream_source_id=upstream_source_id,
        ),
        document_id_factory=lambda: "doc-1",
    )

    assert source.upstream_source_id == "<message@example.test>"


def test_nonempty_whitespace_upstream_identity_is_preserved_verbatim() -> None:
    source = normalize_email(
        _envelope(_raw_message(), upstream_source_id=" "),
        document_id_factory=lambda: "doc-1",
    )

    assert source.upstream_source_id == " "


def test_valid_message_id_uses_semantic_token() -> None:
    raw = _raw_message(message_id=b"(before) <abc@example.test> (after)")

    assert stable_email_identity(raw) == "<abc@example.test>"


@pytest.mark.parametrize(
    "message_id",
    (
        b"bad",
        b"<a@example.test> <b@example.test>",
        b'<"obsolete local"@example.test>',
    ),
)
def test_defective_or_ambiguous_message_id_falls_back_to_raw_wire(
    message_id: bytes,
) -> None:
    raw = _raw_message(message_id=message_id)

    assert stable_email_identity(raw) == (
        FALLBACK_IDENTITY_PREFIX + sha256(raw).hexdigest()
    )


def test_duplicate_message_id_fields_fall_back_to_raw_wire() -> None:
    raw = (
        b"Message-ID: <one@example.test>\r\n"
        b"Message-ID: <two@example.test>\r\n"
        b"Subject: Party\r\n\r\nFriday at 5"
    )

    assert stable_email_identity(raw) == (
        FALLBACK_IDENTITY_PREFIX + sha256(raw).hexdigest()
    )


def test_missing_message_id_fallback_is_exact_raw_sha256() -> None:
    raw = _raw_message(message_id=None)

    assert stable_email_identity(raw) == (
        FALLBACK_IDENTITY_PREFIX + sha256(raw).hexdigest()
    )


def test_raw_fallback_is_deterministic_for_identical_bytes() -> None:
    raw = _raw_message(message_id=None)

    assert stable_email_identity(raw) == stable_email_identity(raw)


def test_different_raw_wire_bytes_intentionally_receive_different_fallback_ids() -> None:
    first = _raw_message(
        body=b"QQ==",
        message_id=None,
        extra_headers=b"Content-Transfer-Encoding: base64",
    )
    second = _raw_message(
        body=b"A",
        message_id=None,
        extra_headers=b"Content-Transfer-Encoding: 7bit",
    )

    assert stable_email_identity(first) != stable_email_identity(second)


def test_default_document_id_is_uuid() -> None:
    source = normalize_email(_envelope(_raw_message()))

    UUID(source.id)


def test_plain_text_body_is_normalized() -> None:
    raw = (
        b"Subject: Plain\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"  First   line \r\n\r\n Second line  "
    )

    source = normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    )

    assert source.text == "First line\nSecond line"


def test_declared_charset_is_used() -> None:
    raw = (
        b"Subject: Latin\r\n"
        b"Content-Type: text/plain; charset=iso-8859-1\r\n\r\n"
        b"caf\xe9"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text == "café"


def test_unknown_charset_degrades_to_utf8_replacement() -> None:
    raw = (
        b"Subject: Unknown charset\r\n"
        b"Content-Type: text/plain; charset=x-not-real\r\n\r\n"
        b"caf\xc3\xa9"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text == "café"


def test_html_visible_text_safe_links_and_alt_text_are_preserved() -> None:
    html = (
        '<p>Hello <a href="https://example.test/event">details</a></p>'
        '<p><a href="mailto:rsvp@example.test">RSVP</a></p>'
        '<img src="https://tracker.invalid/pixel.png" alt="Calendar icon">'
        '<a href="javascript:alert(1)">unsafe link text</a>'
    )

    text = html_to_text(html)

    assert "Hello" in text
    assert "https://example.test/event" in text
    assert "details" in text
    assert "mailto:rsvp@example.test" in text
    assert "Calendar icon" in text
    assert "unsafe link text" in text
    assert "javascript:" not in text
    assert "tracker.invalid" not in text


@pytest.mark.parametrize("tag", ("script", "style", "template"))
def test_html_executable_or_nonvisible_tags_are_excluded(tag: str) -> None:
    assert html_to_text(
        f"<p>Visible</p><{tag}>secret</{tag}><p>After</p>"
    ) == "Visible\nAfter"


def test_html_hidden_attribute_excludes_subtree() -> None:
    assert html_to_text(
        "<div hidden>secret<span>nested</span></div><p>Visible</p>"
    ) == "Visible"


@pytest.mark.parametrize(
    "style",
    (
        "display:none",
        "display: none",
        "DISPLAY: NONE !important",
        "visibility:hidden",
        "visibility: hidden !important",
        "color:red; display:none; font-weight:bold",
    ),
)
def test_common_inline_hidden_styles_are_excluded(style: str) -> None:
    assert html_to_text(
        f'<div style="{style}">secret</div><p>Visible</p>'
    ) == "Visible"


def test_unrelated_or_malformed_inline_style_is_not_treated_as_hidden() -> None:
    assert html_to_text(
        '<p style="color:red; broken">Visible</p>'
    ) == "Visible"


def test_hidden_void_element_does_not_hide_following_content() -> None:
    assert html_to_text(
        '<img hidden src="https://tracker.invalid/x"><p>Visible</p>'
    ) == "Visible"


def test_multipart_alternative_selects_last_supported_representation() -> None:
    raw = (
        b"Subject: Alternative\r\n"
        b"Content-Type: multipart/alternative; boundary=alt\r\n\r\n"
        b"--alt\r\nContent-Type: text/plain\r\n\r\nOlder plain\r\n"
        b"--alt\r\nContent-Type: text/html\r\n\r\n<p>Preferred HTML</p>\r\n"
        b"--alt--\r\n"
    )

    source = normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    )

    assert source.text == "Preferred HTML"


def test_empty_final_multipart_alternative_remains_authoritative() -> None:
    raw = (
        b"Subject: Alternative\r\n"
        b"Content-Type: multipart/alternative; boundary=alt\r\n\r\n"
        b"--alt\r\nContent-Type: text/plain\r\n\r\nOlder text\r\n"
        b"--alt\r\nContent-Type: text/plain\r\n\r\n"
        b"--alt--\r\n"
    )

    source = normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    )

    assert source.text is None


def test_multipart_alternative_without_supported_representation_is_empty() -> None:
    raw = (
        b"Subject: Alternative\r\n"
        b"Content-Type: multipart/alternative; boundary=alt\r\n\r\n"
        b"--alt\r\nContent-Type: application/json\r\n\r\n{}\r\n"
        b"--alt--\r\n"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text is None


def test_multipart_mixed_joins_body_text_and_excludes_attachment() -> None:
    raw = (
        b"Subject: Mixed\r\n"
        b"Content-Type: multipart/mixed; boundary=mix\r\n\r\n"
        b"--mix\r\nContent-Type: text/plain\r\n\r\nFirst\r\n"
        b"--mix\r\nContent-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=file.bin\r\n\r\n"
        b"SECRET_ATTACHMENT\r\n"
        b"--mix\r\nContent-Type: text/plain\r\n\r\nSecond\r\n"
        b"--mix--\r\n"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text == "First\nSecond"


def test_filename_marks_text_part_as_attachment() -> None:
    raw = (
        b"Subject: Mixed\r\n"
        b"Content-Type: multipart/mixed; boundary=mix\r\n\r\n"
        b"--mix\r\nContent-Type: text/plain\r\n\r\nVisible\r\n"
        b"--mix\r\nContent-Type: text/plain; name=attached.txt\r\n"
        b"Content-Disposition: inline; filename=attached.txt\r\n\r\n"
        b"Hidden attachment text\r\n"
        b"--mix--\r\n"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text == "Visible"


def test_encapsulated_message_is_excluded_from_body_text() -> None:
    raw = (
        b"Subject: Outer\r\n"
        b"Content-Type: multipart/mixed; boundary=mix\r\n\r\n"
        b"--mix\r\nContent-Type: text/plain\r\n\r\nOuter text\r\n"
        b"--mix\r\nContent-Type: message/rfc822\r\n\r\n"
        b"Subject: Forwarded\r\n\r\nSECRET_FORWARDED_TEXT\r\n"
        b"--mix--\r\n"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text == "Outer text"


def test_unsupported_leaf_media_type_is_ignored() -> None:
    raw = (
        b"Subject: Binary\r\n"
        b"Content-Type: application/json\r\n\r\n"
        b'{"secret":"not parser text"}'
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text is None


def test_related_defaults_to_first_child_without_start() -> None:
    raw = (
        b"Subject: Related\r\n"
        b"Content-Type: multipart/related; boundary=rel\r\n\r\n"
        b"--rel\r\nContent-Type: text/html\r\nContent-ID: <root@id>\r\n\r\n"
        b"<p>Root text</p>\r\n"
        b"--rel\r\nContent-Type: image/png\r\nContent-ID: <image@id>\r\n\r\nPNG\r\n"
        b"--rel--\r\n"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text == "Root text"


def test_related_uses_designated_start_child() -> None:
    raw = (
        b"Subject: Related\r\n"
        b"Content-Type: multipart/related; boundary=rel; start=\"<root@id>\"\r\n\r\n"
        b"--rel\r\nContent-Type: text/plain\r\nContent-ID: <resource@id>\r\n\r\n"
        b"Wrong resource\r\n"
        b"--rel\r\nContent-Type: text/html\r\nContent-ID: <root@id>\r\n\r\n"
        b"<p>Designated root</p>\r\n"
        b"--rel--\r\n"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text == "Designated root"


def test_related_uses_stdlib_decoded_rfc2231_start_parameter() -> None:
    raw = (
        b"Subject: Related\r\n"
        b"Content-Type: multipart/related; boundary=rel; "
        b"start*=us-ascii''%3Croot%40id%3E\r\n\r\n"
        b"--rel\r\nContent-Type: text/plain\r\nContent-ID: <resource@id>\r\n\r\n"
        b"Wrong resource\r\n"
        b"--rel\r\nContent-Type: text/html\r\nContent-ID: <root@id>\r\n\r\n"
        b"<p>RFC2231 root</p>\r\n"
        b"--rel--\r\n"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text == "RFC2231 root"


def test_related_missing_designated_root_falls_back_to_first_child() -> None:
    raw = (
        b"Subject: Related\r\n"
        b"Content-Type: multipart/related; boundary=rel; start=\"<missing@id>\"\r\n\r\n"
        b"--rel\r\nContent-Type: text/plain\r\nContent-ID: <first@id>\r\n\r\n"
        b"First child\r\n"
        b"--rel--\r\n"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text == "First child"


def test_empty_multipart_related_degrades_to_no_text() -> None:
    raw = (
        b"Subject: Related\r\n"
        b"Content-Type: multipart/related; boundary=rel\r\n\r\n"
        b"--rel--\r\n"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text is None


def test_malformed_transfer_encoding_degrades_without_crashing() -> None:
    raw = (
        b"Subject: Broken transfer\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"not valid base64!!!"
    )

    source = normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    )

    assert source.kind is SourceKind.EMAIL


def test_title_uses_first_subject_and_normalizes_whitespace() -> None:
    raw = (
        b"Subject:   A   spaced   subject\r\n"
        b"Subject: Ignored duplicate\r\n\r\n"
        b"Body"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).title == "A spaced subject"


def test_missing_subject_becomes_none() -> None:
    raw = b"Content-Type: text/plain\r\n\r\nBody"

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).title is None


def test_inline_style_helper_is_explicitly_bounded() -> None:
    assert not email_normalize._inline_style_hides("display:block")
    assert not email_normalize._inline_style_hides("visibility:visible")
    assert not email_normalize._inline_style_hides("broken declaration")


def test_decode_text_part_handles_nonbytes_string_payload() -> None:
    class FakeTextPart:
        def get_payload(self, decode: bool = False) -> object:
            return None if decode else "Already decoded"

        def get_content_charset(self) -> None:
            return None

    assert email_normalize._decode_text_part(
        FakeTextPart()  # type: ignore[arg-type]
    ) == "Already decoded"


def test_decode_text_part_handles_nonbytes_nonstr_payload() -> None:
    class FakeTextPart:
        def get_payload(self, decode: bool = False) -> object:
            return None if decode else []

        def get_content_charset(self) -> None:
            return None

    assert email_normalize._decode_text_part(
        FakeTextPart()  # type: ignore[arg-type]
    ) == ""


def test_parse_error_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    class BrokenParser:
        def __init__(self, *, policy: object) -> None:
            pass

        def parsebytes(self, raw_message: bytes) -> None:
            raise RuntimeError("boom")

    monkeypatch.setattr(email_normalize, "BytesParser", BrokenParser)

    with pytest.raises(EmailNormalizationError, match="could not be parsed"):
        stable_email_identity(b"anything")


def test_message_id_parser_failure_falls_back_to_raw_wire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = _raw_message(message_id=b"<message@example.test>")

    monkeypatch.setattr(
        email_normalize,
        "get_msg_id",
        lambda _value: (_ for _ in ()).throw(ValueError("boom")),
    )

    assert stable_email_identity(raw) == (
        FALLBACK_IDENTITY_PREFIX + sha256(raw).hexdigest()
    )


def test_hidden_subtree_keeps_void_elements_hidden() -> None:
    assert html_to_text(
        '<div hidden>secret<img alt="also secret">still secret</div>'
        '<p>Visible</p>'
    ) == "Visible"


def test_html_image_without_alt_adds_no_text() -> None:
    assert html_to_text("<img><p>Visible</p>") == "Visible"


def test_malformed_hidden_markup_does_not_pop_wrong_skip_frame() -> None:
    parser = email_normalize._HTMLTextExtractor()
    parser.handle_starttag("div", [("hidden", None)])
    parser.handle_starttag("span", [])
    parser.handle_endtag("div")
    parser.handle_data("still hidden")
    parser.handle_endtag("span")
    parser.handle_endtag("div")
    parser.handle_data("Visible")

    assert parser.text() == "Visible"


@pytest.mark.parametrize(
    ("remainder", "defects"),
    (
        (" trailing", ()),
        ("", (ValueError("defect"),)),
    ),
)
def test_noncanonical_message_id_parse_result_uses_raw_fallback(
    monkeypatch: pytest.MonkeyPatch,
    remainder: str,
    defects: tuple[Exception, ...],
) -> None:
    class FakeToken:
        all_defects = defects
        value = "<message@example.test>"

    raw = _raw_message(message_id=b"<message@example.test>")
    monkeypatch.setattr(
        email_normalize,
        "get_msg_id",
        lambda _value: (FakeToken(), remainder),
    )

    assert stable_email_identity(raw) == (
        FALLBACK_IDENTITY_PREFIX + sha256(raw).hexdigest()
    )


def test_related_uses_stdlib_decoded_rfc2231_continuation_start() -> None:
    raw = (
        b"Subject: Related\r\n"
        b"Content-Type: multipart/related; boundary=rel; "
        b"start*0*=us-ascii''%3Croot; start*1*=%40id%3E\r\n\r\n"
        b"--rel\r\nContent-Type: text/plain\r\nContent-ID: <resource@id>\r\n\r\n"
        b"Wrong resource\r\n"
        b"--rel\r\nContent-Type: text/html\r\nContent-ID: <root@id>\r\n\r\n"
        b"<p>RFC2231 continuation root</p>\r\n"
        b"--rel--\r\n"
    )

    assert normalize_email(
        _envelope(raw),
        document_id_factory=lambda: "doc",
    ).text == "RFC2231 continuation root"
