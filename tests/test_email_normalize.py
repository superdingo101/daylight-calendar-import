"""Tests for MIME normalization and stable semantic email identity."""

from __future__ import annotations

from datetime import UTC, datetime
from email import policy
from email.message import EmailMessage
import re
from uuid import UUID

import pytest

from custom_components.daylight_calendar_import import email_normalize
from custom_components.daylight_calendar_import.email_normalize import (
    FALLBACK_IDENTITY_PREFIX,
    EmailNormalizationError,
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


def _envelope(raw_message: bytes) -> EmailEnvelope:
    return EmailEnvelope(
        received_at=datetime(2026, 10, 1, 12, 0, tzinfo=UTC),
        raw_message=raw_message,
        provenance=EmailProvenance(
            source_id="mailbox-1",
            source_type=EmailSourceType.DIRECT_IMAP,
            transport_reference=DirectImapReference(
                mailbox="INBOX",
                uid_validity=1234,
                uid=42,
            ),
        ),
    )


def _plain_message(
    body: str = "Practice Thursday at 5 PM",
    *,
    message_id: str | None = "<event-1@example.test>",
) -> bytes:
    message = EmailMessage()
    message["From"] = "coach@example.test"
    message["To"] = "family@example.test"
    message["Date"] = "Thu, 01 Oct 2026 12:00:00 -0700"
    message["Subject"] = "Soccer Practice"
    if message_id is not None:
        message["Message-ID"] = message_id
    message.set_content(body)
    return message.as_bytes(policy=policy.default)


def test_normalize_plain_text_message_uses_rfc_message_id() -> None:
    source = normalize_email(_envelope(_plain_message()))

    UUID(source.id)
    assert source.kind is SourceKind.EMAIL
    assert source.received_at == datetime(2026, 10, 1, 12, 0, tzinfo=UTC)
    assert source.text == "Practice Thursday at 5 PM"
    assert source.title == "Soccer Practice"
    assert source.attachments == ()
    assert source.metadata == {}
    assert source.upstream_source_id == "<event-1@example.test>"


def test_normalize_supports_injected_document_id_factory() -> None:
    source = normalize_email(
        _envelope(_plain_message()),
        document_id_factory=lambda: "email-document-1",
    )
    assert source.id == "email-document-1"


def test_html_to_text_preserves_useful_links_and_skips_active_content() -> None:
    value = """
    <html><body>
      <h1>Team &amp; Family</h1>
      <p>Join <a href="https://zoom.example.test/j/123">Zoom</a><br>at 6 PM</p>
      <script>steal()</script>
      <style>.hidden { display:none }</style>
      <img src="https://tracker.example.test/pixel" alt="Team logo">
      <a href="javascript:alert(1)">Unsafe</a>
    </body></html>
    """

    text = html_to_text(value)

    assert text == (
        "Team & Family\n"
        "Join https://zoom.example.test/j/123 Zoom\n"
        "at 6 PM\n"
        "Team logo\n"
        "Unsafe"
    )
    assert "steal" not in text
    assert "display:none" not in text
    assert "tracker.example.test" not in text
    assert "javascript:" not in text


def test_normalize_html_message_to_text() -> None:
    message = EmailMessage()
    message["Subject"] = "Birthday"
    message["Message-ID"] = "<html@example.test>"
    message.set_content(
        "<p>Party <b>Saturday</b></p><p>Bring cake</p>",
        subtype="html",
    )

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text == "Party Saturday\nBring cake"
    assert source.upstream_source_id == "<html@example.test>"


def test_multipart_alternative_prefers_plain_text() -> None:
    message = EmailMessage()
    message["Subject"] = "Practice"
    message["Message-ID"] = "<alternative@example.test>"
    message.set_content("Plain version at 5 PM")
    message.add_alternative(
        "<p>HTML version at <b>6 PM</b></p>",
        subtype="html",
    )

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text == "Plain version at 5 PM"


def test_multipart_alternative_falls_back_to_html() -> None:
    message = EmailMessage()
    message["Message-ID"] = "<html-only@example.test>"
    message.make_alternative()
    html_part = EmailMessage()
    html_part.set_content("<p>Doors open <b>7 PM</b></p>", subtype="html")
    message.attach(html_part)

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text == "Doors open 7 PM"


def test_multipart_alternative_without_text_normalizes_empty() -> None:
    message = EmailMessage()
    message["Message-ID"] = "<no-text-alternative@example.test>"
    message.make_alternative()
    binary_part = EmailMessage()
    binary_part.set_content(b"{}", maintype="application", subtype="json")
    message.attach(binary_part)

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text is None


def test_multipart_mixed_combines_inline_text_and_skips_text_attachment() -> None:
    message = EmailMessage()
    message["Message-ID"] = "<mixed@example.test>"
    message.set_content("Main invitation")
    message.add_attachment(
        "Internal notes that must not become event text",
        subtype="plain",
        filename="notes.txt",
    )

    followup = EmailMessage()
    followup.set_content("Parking is behind the gym")
    message.attach(followup)

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text == "Main invitation\nParking is behind the gym"
    assert "Internal notes" not in source.text


def test_inline_text_part_with_filename_is_not_body_text() -> None:
    message = EmailMessage()
    message["Message-ID"] = "<inline-file@example.test>"
    message.set_content("Main invitation")
    attachment = EmailMessage()
    attachment.set_content("Inline file contents")
    attachment.add_header(
        "Content-Disposition",
        "inline",
        filename="details.txt",
    )
    message.make_mixed()
    message.attach(attachment)

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text == "Main invitation"
    assert "Inline file contents" not in source.text


def test_empty_text_email_normalizes_without_inventing_content() -> None:
    message = EmailMessage()
    message["Subject"] = "Attachment only"
    message["Message-ID"] = "<attachment-only@example.test>"
    message.set_content("")
    message.add_attachment(
        b"%PDF-fake",
        maintype="application",
        subtype="pdf",
        filename="invite.pdf",
    )

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text is None
    assert source.title == "Attachment only"
    assert source.attachments == ()


def test_unknown_charset_falls_back_without_crashing() -> None:
    raw = (
        b"Message-ID: <charset@example.test>\r\n"
        b"Content-Type: text/plain; charset=x-unknown-charset\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"Caf\xe9 Friday"
    )

    source = normalize_email(_envelope(raw))

    assert source.text == "Caf� Friday"


def test_fallback_identity_is_stable_across_transfer_and_transport_headers() -> None:
    first = EmailMessage()
    first["From"] = "coach@example.test"
    first["To"] = "family@example.test"
    first["Date"] = "Thu, 01 Oct 2026 12:00:00 -0700"
    first["Subject"] = "No Message ID"
    first["Received"] = "from first-hop.example.test"
    first.set_content("Café Friday", cte="base64")

    second = EmailMessage()
    second["From"] = "coach@example.test"
    second["To"] = "family@example.test"
    second["Date"] = "Thu, 01 Oct 2026 12:00:00 -0700"
    second["Subject"] = "No Message ID"
    second["Received"] = "from second-hop.example.test"
    second["X-Transport-Trace"] = "different"
    second.set_content("Café Friday", cte="quoted-printable")

    first_id = stable_email_identity(first.as_bytes(policy=policy.default))
    second_id = stable_email_identity(second.as_bytes(policy=policy.default))

    assert first_id == second_id
    assert first_id.startswith(FALLBACK_IDENTITY_PREFIX)
    assert re.fullmatch(r"email-fallback:v1:[0-9a-f]{64}", first_id)


def test_fallback_identity_changes_with_semantic_body_or_attachment() -> None:
    base = EmailMessage()
    base["Subject"] = "No Message ID"
    base.set_content("Friday at 5")
    changed_body = EmailMessage()
    changed_body["Subject"] = "No Message ID"
    changed_body.set_content("Friday at 6")

    with_attachment = EmailMessage()
    with_attachment["Subject"] = "No Message ID"
    with_attachment.set_content("Friday at 5")
    with_attachment.add_attachment(
        b"one",
        maintype="application",
        subtype="pdf",
        filename="invite.pdf",
    )
    changed_attachment = EmailMessage()
    changed_attachment["Subject"] = "No Message ID"
    changed_attachment.set_content("Friday at 5")
    changed_attachment.add_attachment(
        b"two",
        maintype="application",
        subtype="pdf",
        filename="invite.pdf",
    )

    assert stable_email_identity(base.as_bytes()) != stable_email_identity(
        changed_body.as_bytes()
    )
    assert stable_email_identity(with_attachment.as_bytes()) != stable_email_identity(
        changed_attachment.as_bytes()
    )


def test_duplicate_message_id_headers_use_fallback_identity() -> None:
    raw = (
        b"Message-ID: <one@example.test>\r\n"
        b"Message-ID: <two@example.test>\r\n"
        b"Subject: Duplicate IDs\r\n\r\n"
        b"Friday at 5"
    )

    identity = stable_email_identity(raw)

    assert identity.startswith(FALLBACK_IDENTITY_PREFIX)


def test_defective_single_message_id_uses_fallback_identity() -> None:
    identity = stable_email_identity(
        b"Message-ID: not-a-valid-message-id\r\n"
        b"Subject: Defective ID\r\n\r\n"
        b"Friday at 5"
    )

    assert identity.startswith(FALLBACK_IDENTITY_PREFIX)


def test_fallback_identity_wire_format_is_stable() -> None:
    identity = stable_email_identity(
        b"From: coach@example.test\r\n"
        b"To: family@example.test\r\n"
        b"Date: Thu, 01 Oct 2026 12:00:00 -0700\r\n"
        b"Subject: Practice\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
        b"Friday at 5"
    )

    assert identity == (
        "email-fallback:v1:"
        "8676e3b760c47ef42d2e3c2460d6e8ea2f2afd82ec66b7075d4bc5287e9c42f6"
    )


def test_decoded_part_bytes_handles_string_payload_fallback() -> None:
    class StringPayloadPart:
        def get_payload(self, decode: bool) -> object:
            return None if decode else "raw payload"

        def as_bytes(self, *, policy: object) -> bytes:
            raise AssertionError("as_bytes should not be needed")

    assert email_normalize._decoded_part_bytes(StringPayloadPart()) == b"raw payload"


def test_decoded_part_bytes_handles_serialized_payload_fallback() -> None:
    class SerializedPayloadPart:
        def get_payload(self, decode: bool) -> object:
            return None if decode else object()

        def as_bytes(self, *, policy: object) -> bytes:
            return b"serialized payload"

    assert email_normalize._decoded_part_bytes(SerializedPayloadPart()) == (
        b"serialized payload"
    )


def test_invalid_parser_failure_is_wrapped(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_parse(self, raw_message: bytes) -> object:
        raise RuntimeError("parser failed")

    monkeypatch.setattr(email_normalize.BytesParser, "parsebytes", fail_parse)

    with pytest.raises(
        EmailNormalizationError,
        match="^Email message could not be parsed$",
    ):
        stable_email_identity(_plain_message())


def test_html_to_text_handles_unmatched_skipped_end_tag() -> None:
    assert html_to_text("<p>Visible</p></script><p>Still visible</p>") == (
        "Visible\nStill visible"
    )


def test_html_to_text_skips_nested_template_content_and_img_without_alt() -> None:
    assert html_to_text(
        "<template><div>Hidden</div></template>"
        "<img src=\"https://tracker.example.test/pixel\">"
        "<p>Visible</p>"
    ) == "Visible"


def test_malformed_message_id_values_disambiguate_fallback_identity() -> None:
    first = (
        b"Message-ID: id-1@example.test\r\n"
        b"Subject: Same\r\n\r\n"
        b"Friday at 5"
    )
    second = (
        b"Message-ID: id-2@example.test\r\n"
        b"Subject: Same\r\n\r\n"
        b"Friday at 5"
    )

    first_id = stable_email_identity(first)
    second_id = stable_email_identity(second)

    assert first_id.startswith(FALLBACK_IDENTITY_PREFIX)
    assert second_id.startswith(FALLBACK_IDENTITY_PREFIX)
    assert first_id != second_id


def test_message_id_comments_are_canonicalized_to_msg_id_token() -> None:
    plain = stable_email_identity(
        b"Message-ID: <same@example.test>\r\n\r\nBody"
    )
    commented = stable_email_identity(
        b"Message-ID: (delivery comment) <same@example.test>\r\n\r\nChanged body"
    )

    assert plain == "<same@example.test>"
    assert commented == plain


def test_obsolete_but_valid_message_id_remains_authoritative() -> None:
    first = stable_email_identity(
        b'Message-ID: <"local"@example.test>\r\n\r\nBody one'
    )
    second = stable_email_identity(
        b'Message-ID: <"local"@example.test>\r\n\r\nBody two'
    )

    assert first == '<"local"@example.test>'
    assert second == first


def test_canonical_message_id_rejects_unparsed_plain_value() -> None:
    assert email_normalize._canonical_msg_id_text("not-an-id") is None


def test_message_rfc822_attachment_changes_fallback_identity() -> None:
    def build(attached_body: str) -> bytes:
        attached = EmailMessage()
        attached["Subject"] = "Forwarded details"
        attached.set_content(attached_body)

        outer = EmailMessage()
        outer["Subject"] = "Invitation"
        outer.set_content("Main invitation")
        outer.add_attachment(attached, filename="details.eml")
        return outer.as_bytes(policy=policy.default)

    first_raw = build("Room 101")
    second_raw = build("Room 202")

    assert stable_email_identity(first_raw) != stable_email_identity(second_raw)
    assert normalize_email(_envelope(first_raw)).text == "Main invitation"


def test_lossy_body_bytes_disambiguate_fallback_identity() -> None:
    def raw(payload: bytes) -> bytes:
        return (
            b"Subject: Broken charset\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"Content-Transfer-Encoding: 8bit\r\n\r\n"
            + payload
        )

    first = raw(b"\x80")
    second = raw(b"\x81")

    assert normalize_email(_envelope(first)).text == "�"
    assert normalize_email(_envelope(second)).text == "�"
    assert stable_email_identity(first) != stable_email_identity(second)


def _related_message(resource_text: str, *, start: str = "<root@example.test>") -> bytes:
    return (
        b"Subject: Related\r\n"
        b"Content-Type: multipart/related; boundary=rel; start=\""
        + start.encode()
        + b"\"\r\n\r\n"
        b"--rel\r\n"
        b"Content-Type: text/plain; charset=utf-8\r\n"
        b"Content-ID: <resource@example.test>\r\n\r\n"
        + resource_text.encode()
        + b"\r\n--rel\r\n"
        b"Content-Type: text/html; charset=utf-8\r\n"
        b"Content-ID: <root@example.test>\r\n\r\n"
        b"<p>Meeting Friday</p>\r\n"
        b"--rel--\r\n"
    )


def test_multipart_related_uses_designated_root_only() -> None:
    first = _related_message("resource one")
    second = _related_message("resource two")

    source = normalize_email(_envelope(first))

    assert source.text == "Meeting Friday"
    assert "resource one" not in source.text
    assert stable_email_identity(first) != stable_email_identity(second)


def test_related_root_defaults_to_first_child_and_handles_empty_container() -> None:
    empty = EmailMessage()
    empty.make_related()
    assert email_normalize._related_root(empty, []) is None

    child = EmailMessage()
    child.set_content("Root text")
    related = EmailMessage()
    related.make_related()
    related.attach(child)
    assert email_normalize._related_root(related, [child]) is child

    related.set_param("start", "<missing@example.test>", header="Content-Type")
    assert email_normalize._related_root(related, [child]) is child


def test_format_flowed_reconstructs_soft_wrapped_text() -> None:
    delsp_yes = (
        b"Message-ID: <flowed-yes@example.test>\r\n"
        b"Content-Type: text/plain; charset=utf-8; format=flowed; delsp=yes\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"Meet Fri \r\nday at 5"
    )
    delsp_no = (
        b"Message-ID: <flowed-no@example.test>\r\n"
        b"Content-Type: text/plain; charset=utf-8; format=flowed\r\n"
        b"Content-Transfer-Encoding: 8bit\r\n\r\n"
        b"Visit https://example.test/very \r\n long Friday"
    )

    assert normalize_email(_envelope(delsp_yes)).text == "Meet Friday at 5"
    assert normalize_email(_envelope(delsp_no)).text == (
        "Visit https://example.test/very long Friday"
    )


def test_format_flowed_preserves_fixed_signature_and_quote_boundary() -> None:
    value = ">Quoted line \r\nplain\r\n-- \r\n"
    decoded = email_normalize._decode_format_flowed(value, delsp=False)

    assert decoded == ">Quoted line \nplain\n-- \n"


def test_noscript_fallback_text_is_preserved() -> None:
    assert html_to_text(
        "<noscript><p>Meeting Friday</p></noscript>"
    ) == "Meeting Friday"


def test_mismatched_skipped_html_closing_tag_cannot_expose_hidden_text() -> None:
    assert html_to_text(
        "<template></script>hidden payload</template><p>Visible</p>"
    ) == "Visible"


def test_nested_skipped_html_tags_require_matching_closures() -> None:
    assert html_to_text(
        "<template><script>hidden</script></template><p>Visible</p>"
    ) == "Visible"


def test_fallback_identity_describes_non_body_leaf_part() -> None:
    message = EmailMessage()
    message["Subject"] = "Binary only"
    message.set_content(b"{\"event\":true}", maintype="application", subtype="json")

    identity = stable_email_identity(message.as_bytes(policy=policy.default))

    assert identity.startswith(FALLBACK_IDENTITY_PREFIX)


def test_alternative_can_fall_back_to_nested_related_representation() -> None:
    message = EmailMessage()
    message["Message-ID"] = "<nested-alternative@example.test>"
    message.make_alternative()

    related = EmailMessage()
    related.make_related()
    html = EmailMessage()
    html.set_content("<p>Nested meeting Friday</p>", subtype="html")
    related.attach(html)
    message.attach(related)

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text == "Nested meeting Friday"


def test_rejected_message_id_preserves_raw_disambiguating_suffix() -> None:
    first = (
        b"Message-ID: <same@id><unique1@id>\r\n"
        b"Subject: Same\r\n\r\n"
        b"Friday at 5"
    )
    second = (
        b"Message-ID: <same@id><unique2@id>\r\n"
        b"Subject: Same\r\n\r\n"
        b"Friday at 5"
    )

    first_id = stable_email_identity(first)
    second_id = stable_email_identity(second)

    assert first_id.startswith(FALLBACK_IDENTITY_PREFIX)
    assert second_id.startswith(FALLBACK_IDENTITY_PREFIX)
    assert first_id != second_id


def test_raw_message_id_extraction_preserves_non_ascii_octets() -> None:
    raw = (
        b"Message-ID: broken-\xff-id\r\n"
        b"Subject: Same\r\n\r\n"
        b"Friday at 5"
    )

    [value] = email_normalize._raw_header_values(raw, b"message-id")

    assert value == b"broken-\xff-id"
    assert stable_email_identity(raw).startswith(FALLBACK_IDENTITY_PREFIX)


def test_raw_header_extraction_unfolds_only_for_authoritative_parsing() -> None:
    raw = (
        b"Message-ID: <folded@\r\n"
        b" example.test>\r\n\r\n"
        b"Body"
    )

    [raw_value] = email_normalize._raw_header_values(raw, b"message-id")

    assert raw_value == b"<folded@\n example.test>"
    assert email_normalize._unfold_header_value(raw_value) == (
        b"<folded@ example.test>"
    )


def test_pathological_message_id_lazy_parse_does_not_escape_boundary() -> None:
    raw = (
        b"Message-ID: <foo@[ test ]>\r\n"
        b"Subject: Broken structured header\r\n\r\n"
        b"Friday at 5"
    )

    identity = stable_email_identity(raw)
    source = normalize_email(_envelope(raw))

    assert identity.startswith(FALLBACK_IDENTITY_PREFIX)
    assert source.upstream_source_id == identity


def test_public_normalization_wraps_lazy_header_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = email_normalize._first_header

    def fail_header(message: object, name: str) -> str:
        if name == "subject":
            raise RuntimeError("lazy header exploded")
        return original(message, name)  # pragma: no cover

    monkeypatch.setattr(email_normalize, "_first_header", fail_header)

    with pytest.raises(
        EmailNormalizationError,
        match="^Email message could not be normalized$",
    ):
        normalize_email(_envelope(_plain_message()))


def test_message_id_cfws_comment_with_angle_brackets_is_not_an_identifier() -> None:
    first = stable_email_identity(
        b"Message-ID: (previous <x@y>) <real@id>\r\n\r\nBody one"
    )
    second = stable_email_identity(
        b"Message-ID: <real@id>\r\n\r\nBody two"
    )

    assert first == "<real@id>"
    assert second == first


def test_obsolete_internal_cfws_is_removed_from_message_id_token() -> None:
    first = stable_email_identity(
        b"Message-ID: <foo. (comment) bar@example>\r\n\r\nBody one"
    )
    second = stable_email_identity(
        b"Message-ID: <foo.bar@example>\r\n\r\nBody two"
    )

    assert first == "<foo.bar@example>"
    assert second == first


def test_unmarked_message_rfc822_is_excluded_and_hashed_atomically() -> None:
    def build(forwarded_body: str) -> bytes:
        forwarded = EmailMessage()
        forwarded["Subject"] = "Forwarded"
        forwarded.set_content(forwarded_body)

        wrapper = EmailMessage()
        wrapper.set_type("message/rfc822")
        wrapper.set_payload([forwarded])

        outer = EmailMessage()
        outer["Subject"] = "Outer"
        outer.set_content("Outer invitation")
        outer.make_mixed()
        outer.attach(wrapper)
        return outer.as_bytes(policy=policy.default)

    first = build("Forwarded room 101")
    second = build("Forwarded room 202")

    assert normalize_email(_envelope(first)).text == "Outer invitation"
    assert stable_email_identity(first) != stable_email_identity(second)


def test_alternative_prefers_plain_root_inside_related_over_direct_html() -> None:
    message = EmailMessage()
    message["Message-ID"] = "<alternative-related@example.test>"
    message.make_alternative()

    html = EmailMessage()
    html.set_content("<p>HTML at 6 PM</p>", subtype="html")
    message.attach(html)

    related = EmailMessage()
    related.make_related()
    plain_root = EmailMessage()
    plain_root["Content-ID"] = "<plain-root@example.test>"
    plain_root.set_content("Plain at 5 PM")
    related.attach(plain_root)
    related.set_param(
        "start",
        "<plain-root@example.test>",
        header="Content-Type",
    )
    message.attach(related)

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text == "Plain at 5 PM"


def test_alternative_ranks_two_related_roots_by_effective_type() -> None:
    message = EmailMessage()
    message["Message-ID"] = "<two-related@example.test>"
    message.make_alternative()

    html_related = EmailMessage()
    html_related.make_related()
    html_root = EmailMessage()
    html_root.set_content("<p>HTML representation</p>", subtype="html")
    html_related.attach(html_root)

    plain_related = EmailMessage()
    plain_related.make_related()
    plain_root = EmailMessage()
    plain_root.set_content("Plain representation")
    plain_related.attach(plain_root)

    message.attach(html_related)
    message.attach(plain_related)

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text == "Plain representation"


def test_related_start_matches_content_id_with_cfws_comment() -> None:
    raw = (
        b"Message-ID: <related-cid@example.test>\r\n"
        b"Content-Type: multipart/related; boundary=rel; start=\"<root@id>\"\r\n\r\n"
        b"--rel\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-ID: <resource@id>\r\n\r\n"
        b"Wrong resource text\r\n"
        b"--rel\r\n"
        b"Content-Type: text/html\r\n"
        b"Content-ID: (note) <root@id>\r\n\r\n"
        b"<p>Correct root text</p>\r\n"
        b"--rel--\r\n"
    )

    source = normalize_email(_envelope(raw))

    assert source.text == "Correct root text"


def test_canonical_content_id_rejects_trailing_garbage() -> None:
    assert email_normalize._canonical_msg_id_text("<root@id> garbage") is None


def test_format_flowed_counts_quote_depth_before_space_unstuffing() -> None:
    value = " >literal flowed \r\ncontinuation"

    decoded = email_normalize._decode_format_flowed(value, delsp=False)

    assert decoded == ">literal flowed continuation"


def test_format_flowed_unstuffs_space_after_quote_markers() -> None:
    value = "> quoted flowed \r\n> continuation"

    decoded = email_normalize._decode_format_flowed(value, delsp=False)

    assert decoded == ">quoted flowed continuation"


def test_raw_header_lookup_is_case_insensitive_for_bytes_names() -> None:
    raw = b"MESSAGE-ID: <case@example.test>\r\n\r\nBody"

    assert email_normalize._raw_header_values(raw, b"message-id") == (
        b"<case@example.test>",
    )
    assert stable_email_identity(raw) == "<case@example.test>"


def test_invalid_message_id_defect_uses_fallback_identity() -> None:
    invalid = (
        b"Message-ID: <a@b\r\n"
        b"Subject: Same\r\n\r\n"
        b"Body one"
    )
    valid = (
        b"Message-ID: <a@b>\r\n"
        b"Subject: Same\r\n\r\n"
        b"Body two"
    )

    invalid_id = stable_email_identity(invalid)

    assert invalid_id.startswith(FALLBACK_IDENTITY_PREFIX)
    assert invalid_id != "<a@b>"
    assert stable_email_identity(valid) == "<a@b>"


def test_invalid_message_id_defect_does_not_ignore_body_changes() -> None:
    first = b"Message-ID: <a@b\r\n\r\nBody one"
    second = b"Message-ID: <a@b\r\n\r\nBody two"

    assert stable_email_identity(first) != stable_email_identity(second)


def test_obsolete_quoted_local_part_strips_surrounding_cfws() -> None:
    commented = stable_email_identity(
        b'Message-ID: <(note)"local"@example>\r\n\r\nBody one'
    )
    plain = stable_email_identity(
        b'Message-ID: <"local"@example>\r\n\r\nBody two'
    )

    assert commented == '<"local"@example>'
    assert plain == commented


def test_part_descriptor_canonicalizes_equivalent_content_ids() -> None:
    def build(content_id: str) -> bytes:
        raw = (
            b"Subject: Related descriptor\r\n"
            b"Content-Type: multipart/related; boundary=rel; "
            b"start=\"<root@id>\"\r\n\r\n"
            b"--rel\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-ID: <root@id>\r\n\r\n"
            b"<p>Root</p>\r\n"
            b"--rel\r\n"
            b"Content-Type: image/png\r\n"
            b"Content-ID: "
            + content_id.encode()
            + b"\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"YWJj\r\n"
            b"--rel--\r\n"
        )
        return raw

    plain = build("<res@id>")
    commented = build("(note) <res@id>")

    assert stable_email_identity(plain) == stable_email_identity(commented)


def test_part_descriptor_preserves_malformed_content_id_losslessly() -> None:
    def build(content_id: bytes) -> bytes:
        return (
            b"Subject: Related descriptor\r\n"
            b"Content-Type: multipart/related; boundary=rel; "
            b"start=\"<root@id>\"\r\n\r\n"
            b"--rel\r\n"
            b"Content-Type: text/html\r\n"
            b"Content-ID: <root@id>\r\n\r\n"
            b"<p>Root</p>\r\n"
            b"--rel\r\n"
            b"Content-Type: image/png\r\n"
            b"Content-ID: "
            + content_id
            + b"\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"YWJj\r\n"
            b"--rel--\r\n"
        )

    first = build(b"broken-one")
    second = build(b"broken-two")

    assert stable_email_identity(first) != stable_email_identity(second)


def test_raw_header_scan_stops_before_message_body() -> None:
    raw = (
        b"Subject: No ID header\r\n"
        b"X-Test: value\r\n\r\n"
        b"Message-ID: <body-only@example.test>\r\n"
        b"Body contents"
    )

    assert email_normalize._raw_header_values(raw, b"message-id") == ()
    assert stable_email_identity(raw).startswith(FALLBACK_IDENTITY_PREFIX)


@pytest.mark.parametrize(
    "raw",
    (
        b"Message-ID: <lf@example.test>\n\nBody",
        b"Message-ID: <cr@example.test>\r\rBody",
    ),
)
def test_raw_header_scan_supports_non_crlf_line_endings(raw: bytes) -> None:
    [value] = email_normalize._raw_header_values(raw, b"message-id")
    assert value.startswith(b"<")


def test_normalize_email_re_raises_normalization_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_parse(raw_message: bytes) -> object:
        raise EmailNormalizationError("sentinel normalization failure")

    monkeypatch.setattr(email_normalize, "_parse_message", fail_parse)

    with pytest.raises(
        EmailNormalizationError,
        match="^sentinel normalization failure$",
    ):
        normalize_email(_envelope(_plain_message()))


def test_stable_identity_wraps_unexpected_normalization_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_identity(message: object, raw_message: bytes) -> str:
        raise RuntimeError("unexpected identity failure")

    monkeypatch.setattr(email_normalize, "_message_identity", fail_identity)

    with pytest.raises(
        EmailNormalizationError,
        match="^Email message could not be normalized$",
    ):
        stable_email_identity(_plain_message())


def test_raw_header_scan_stops_at_malformed_header_boundary() -> None:
    raw = (
        b"Subject: valid\r\n"
        b"Malformed header without colon\r\n"
        b"Message-ID: <body-fake@example.test>\r\n"
        b"Body"
    )

    assert email_normalize._raw_header_values(raw, b"message-id") == ()
    assert stable_email_identity(raw).startswith(FALLBACK_IDENTITY_PREFIX)


def test_raw_header_scan_rejects_invalid_field_name_spacing() -> None:
    raw = (
        b"Message-ID : <fake@example.test>\r\n"
        b"Message-ID: <later@example.test>\r\n"
        b"Body"
    )

    assert email_normalize._raw_header_values(raw, b"message-id") == ()
    assert stable_email_identity(raw).startswith(FALLBACK_IDENTITY_PREFIX)


def test_raw_header_scan_rejects_first_line_continuation() -> None:
    raw = (
        b" continued text\r\n"
        b"Message-ID: <fake@example.test>\r\n"
        b"Body"
    )

    assert email_normalize._raw_header_values(raw, b"message-id") == ()


def test_iter_header_lines_handles_empty_input() -> None:
    assert list(email_normalize._iter_header_lines(b"")) == []


def test_alternative_falls_back_to_nested_mixed_representation() -> None:
    message = EmailMessage()
    message["Message-ID"] = "<mixed-alternative@example.test>"
    message.make_alternative()

    mixed = EmailMessage()
    mixed.make_mixed()
    plain = EmailMessage()
    plain.set_content("Plain portion")
    html = EmailMessage()
    html.set_content("<p>HTML portion</p>", subtype="html")
    mixed.attach(plain)
    mixed.attach(html)
    message.attach(mixed)

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text == "Plain portion\nHTML portion"


@pytest.mark.parametrize(
    "raw",
    (
        b"Message-ID: <(comment)@example.test>\r\n\r\nBody one",
        b"Message-ID: <local@(comment)>\r\n\r\nBody two",
    ),
)
def test_semantically_empty_message_id_side_uses_fallback(raw: bytes) -> None:
    identity = stable_email_identity(raw)

    assert identity.startswith(FALLBACK_IDENTITY_PREFIX)


def test_semantically_empty_message_id_side_keeps_body_in_identity() -> None:
    first = b"Message-ID: <(one)@example.test>\r\n\r\nBody one"
    second = b"Message-ID: <(two)@example.test>\r\n\r\nBody two"

    assert stable_email_identity(first) != stable_email_identity(second)


def test_lossy_identity_header_bytes_disambiguate_fallback() -> None:
    first = (
        b"Subject: Broken \x80 subject\r\n"
        b"From: sender@example.test\r\n\r\n"
        b"Friday at 5"
    )
    second = (
        b"Subject: Broken \x81 subject\r\n"
        b"From: sender@example.test\r\n\r\n"
        b"Friday at 5"
    )

    first_source = normalize_email(_envelope(first))
    second_source = normalize_email(_envelope(second))

    assert first_source.title == second_source.title
    assert stable_email_identity(first) != stable_email_identity(second)


def test_lossy_identity_header_descriptor_is_omitted_for_clean_headers() -> None:
    message = email_normalize._parse_message(
        b"Subject: Clean subject\r\n\r\nBody"
    )

    assert email_normalize._lossy_identity_header_descriptors(
        message,
        b"Subject: Clean subject\r\n\r\nBody",
    ) == {}


def test_lossy_identity_header_descriptor_handles_missing_raw_header() -> None:
    message = email_normalize._parse_message(
        b"Subject: Broken \x80 subject\r\n\r\nBody"
    )

    assert email_normalize._lossy_identity_header_descriptors(
        message,
        b"From: sender@example.test\r\n\r\nBody",
    ) == {}


def test_header_line_iterator_stops_before_large_body() -> None:
    body = b"x" * 100_000
    raw = b"Subject: Test\nMessage-ID: <linear@example.test>\n\n" + body

    assert list(email_normalize._iter_header_lines(raw)) == [
        b"Subject: Test",
        b"Message-ID: <linear@example.test>",
        b"",
    ]


def test_header_line_iterator_yields_final_line_without_separator() -> None:
    assert list(email_normalize._iter_header_lines(b"Subject: Test")) == [
        b"Subject: Test"
    ]


@pytest.mark.parametrize("subtype", ("global", "news"))
def test_unmarked_encapsulated_message_subtypes_are_atomic(
    subtype: str,
) -> None:
    forwarded = EmailMessage()
    forwarded["Subject"] = "Forwarded"
    forwarded.set_content("Forwarded body must not reach parser text")

    wrapper = EmailMessage()
    wrapper.set_type(f"message/{subtype}")
    wrapper.set_payload([forwarded])

    outer = EmailMessage()
    outer["Subject"] = "Outer"
    outer.set_content("Outer invitation")
    outer.make_mixed()
    outer.attach(wrapper)

    raw = outer.as_bytes(policy=policy.default)
    source = normalize_email(_envelope(raw))

    assert source.text == "Outer invitation"
    assert "Forwarded body" not in source.text
    assert stable_email_identity(raw).startswith(FALLBACK_IDENTITY_PREFIX)


def test_semantic_msg_id_token_rejects_missing_top_level_at_symbol() -> None:
    assert email_normalize._semantic_msg_id_token(["local-only"]) is None


def test_raw_header_values_flushes_valid_final_header_without_separator() -> None:
    assert email_normalize._raw_header_values(
        b"Message-ID: <eof@example.test>",
        b"message-id",
    ) == (b"<eof@example.test>",)


def test_lossy_attachment_filename_bytes_disambiguate_fallback_identity() -> None:
    def build(filename_octet: bytes) -> bytes:
        return (
            b"Subject: Attachment\r\n"
            b"Content-Type: multipart/mixed; boundary=part\r\n\r\n"
            b"--part\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n\r\n"
            b"Friday at 5\r\n"
            b"--part\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Disposition: attachment; filename=\"file"
            + filename_octet
            + b".txt\"\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"YWJj\r\n"
            b"--part--\r\n"
        )

    first = build(b"\x80")
    second = build(b"\x81")

    first_source = normalize_email(_envelope(first))
    second_source = normalize_email(_envelope(second))

    assert first_source.text == second_source.text == "Friday at 5"
    assert stable_email_identity(first) != stable_email_identity(second)


def test_lossy_filename_raw_header_descriptor_omitted_for_clean_filename() -> None:
    message = EmailMessage()
    message.set_content("Body")
    message.add_attachment(
        b"abc",
        maintype="application",
        subtype="octet-stream",
        filename="clean.txt",
    )
    attachment = list(message.iter_attachments())[0]

    descriptor = email_normalize._part_descriptor(attachment)

    assert descriptor["filename"] == "clean.txt"
    assert "filename_raw_headers" not in descriptor


def test_lossy_filename_raw_header_descriptor_handles_no_raw_headers() -> None:
    class LossyFilenamePart:
        def get_filename(self) -> str:
            return "file�.txt"

        def get_content_id(self) -> None:
            return None

        def get_content_type(self) -> str:
            return "application/octet-stream"

        def get_content_disposition(self) -> str:
            return "attachment"

        def raw_items(self) -> list[tuple[str, str]]:
            return []

        def get(self, name: str, default: str = "") -> str:
            return default

        def is_multipart(self) -> bool:
            return False

        def get_payload(self, decode: bool = False) -> bytes:
            return b"abc"

    part = LossyFilenamePart()
    descriptor = email_normalize._part_descriptor(part)  # type: ignore[arg-type]

    assert descriptor["filename"] == "file�.txt"
    assert "filename_raw_headers" not in descriptor


def test_attachment_filename_preserves_significant_internal_whitespace() -> None:
    def build(filename: str) -> bytes:
        message = EmailMessage()
        message["Subject"] = "Attachment"
        message.set_content("Friday at 5")
        message.add_attachment(
            b"abc",
            maintype="application",
            subtype="octet-stream",
            filename=filename,
        )
        return message.as_bytes(policy=policy.default)

    assert stable_email_identity(build("file  one.txt")) != stable_email_identity(
        build("file one.txt")
    )


def test_defective_encoded_word_filename_bytes_disambiguate_identity() -> None:
    def build(encoded_filename: bytes) -> bytes:
        return (
            b"Subject: Attachment\r\n"
            b"Content-Type: multipart/mixed; boundary=part\r\n\r\n"
            b"--part\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Friday at 5\r\n"
            b"--part\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Disposition: attachment; filename=\""
            + encoded_filename
            + b"\"\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"YWJj\r\n"
            b"--part--\r\n"
        )

    first = build(b"=?utf-8?b?QQ=A?=")
    second = build(b"=?utf-8?b?QQ=B?=")

    assert stable_email_identity(first) != stable_email_identity(second)


def test_lossy_filename_parameter_identity_ignores_header_folding_and_unrelated_params() -> None:
    unfolded = (
        b"Subject: Attachment\r\n"
        b"Content-Type: multipart/mixed; boundary=part\r\n\r\n"
        b"--part\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Friday at 5\r\n"
        b"--part\r\n"
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Disposition: attachment; filename=\"file\x80.txt\"\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"YWJj\r\n"
        b"--part--\r\n"
    )
    folded = (
        b"Subject: Attachment\r\n"
        b"Content-Type: multipart/mixed; boundary=part\r\n\r\n"
        b"--part\r\n"
        b"Content-Type: application/octet-stream; x-extra=ignored\r\n"
        b"Content-Disposition: attachment;\r\n"
        b" filename = \"file\x80.txt\"\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"YWJj\r\n"
        b"--part\r\n"
        b"Content-Type: text/plain\r\n\r\n"
        b"Friday at 5\r\n"
        b"--part--\r\n"
    )

    assert stable_email_identity(unfolded) == stable_email_identity(folded)


def test_defective_encoded_word_identity_headers_preserve_raw_values() -> None:
    first = (
        b"Subject: =?utf-8?b?QQ=A?=\r\n"
        b"From: sender@example.test\r\n\r\n"
        b"Friday at 5"
    )
    second = (
        b"Subject: =?utf-8?b?QQ=B?=\r\n"
        b"From: sender@example.test\r\n\r\n"
        b"Friday at 5"
    )

    assert normalize_email(_envelope(first)).title == normalize_email(_envelope(second)).title
    assert stable_email_identity(first) != stable_email_identity(second)


def test_rejected_message_id_folding_does_not_change_fallback_identity() -> None:
    unfolded = (
        b"Message-ID: broken- id\r\n"
        b"Subject: Same\r\n\r\n"
        b"Friday at 5"
    )
    folded = (
        b"Message-ID: broken-\r\n"
        b" id\r\n"
        b"Subject: Same\r\n\r\n"
        b"Friday at 5"
    )

    assert stable_email_identity(unfolded) == stable_email_identity(folded)


def test_related_root_rejects_ambiguous_content_id_child() -> None:
    raw = (
        b"Message-ID: <related-ambiguous@example.test>\r\n"
        b"Content-Type: multipart/related; boundary=rel; start=\"<root@id>\"\r\n\r\n"
        b"--rel\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-ID: <root@id>\r\n"
        b"Content-ID: <other@id>\r\n\r\n"
        b"Wrong ambiguous root\r\n"
        b"--rel\r\n"
        b"Content-Type: text/html\r\n"
        b"Content-ID: <root@id>\r\n\r\n"
        b"<p>Correct unique root</p>\r\n"
        b"--rel--\r\n"
    )

    assert normalize_email(_envelope(raw)).text == "Correct unique root"


def test_defective_base64_body_wire_bytes_disambiguate_identity() -> None:
    def build(suffix: bytes) -> bytes:
        return (
            b"Subject: Defective transfer\r\n"
            b"Content-Type: text/plain; charset=us-ascii\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"QQ=="
            + suffix
        )

    first = build(b"\xff")
    second = build(b"\xfe")

    assert normalize_email(_envelope(first)).text == "A"
    assert normalize_email(_envelope(second)).text == "A"
    assert stable_email_identity(first) != stable_email_identity(second)


def test_defective_base64_attachment_wire_bytes_disambiguate_identity() -> None:
    def build(suffix: bytes) -> bytes:
        return (
            b"Subject: Attachment\r\n"
            b"Content-Type: multipart/mixed; boundary=part\r\n\r\n"
            b"--part\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Friday at 5\r\n"
            b"--part\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Disposition: attachment; filename=\"file.bin\"\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"QQ=="
            + suffix
            + b"\r\n--part--\r\n"
        )

    assert stable_email_identity(build(b"\xff")) != stable_email_identity(build(b"\xfe"))


def test_html_hidden_attribute_excludes_subtree() -> None:
    assert html_to_text(
        "<p>Visible Friday</p>"
        "<div hidden>ignore the real event</div>"
        "<p>Visible 5 PM</p>"
    ) == "Visible Friday\nVisible 5 PM"


def test_html_hidden_subtree_with_void_elements_remains_hidden() -> None:
    assert html_to_text(
        "<div hidden>hidden<img alt=\"secret\"><br>still hidden</div>"
        "<p>Visible</p>"
    ) == "Visible"


def test_format_flowed_signature_separator_is_hard_join_boundary() -> None:
    decoded = email_normalize._decode_format_flowed(
        "hello \r\n-- \r\nsig",
        delsp=False,
    )

    assert decoded == "hello \n-- \nsig"


def test_encapsulated_multipart_boundary_does_not_change_identity() -> None:
    def build(boundary: str) -> bytes:
        return (
            b"Subject: Outer\r\n"
            b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
            b"--outer\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Outer invitation\r\n"
            b"--outer\r\n"
            b"Content-Type: message/rfc822\r\n\r\n"
            b"Subject: Forwarded\r\n"
            b"Content-Type: multipart/alternative; boundary="
            + boundary.encode()
            + b"\r\n\r\n--"
            + boundary.encode()
            + b"\r\nContent-Type: text/plain\r\n\r\nForwarded text\r\n--"
            + boundary.encode()
            + b"--\r\n"
            b"--outer--\r\n"
        )

    assert stable_email_identity(build("inner-x")) == stable_email_identity(
        build("inner-y")
    )


def test_hidden_void_element_is_skipped_without_opening_subtree() -> None:
    assert html_to_text(
        "<input hidden value=\"secret\"><p>Visible</p>"
    ) == "Visible"


def test_raw_encoded_word_detection_rejects_unparseable_marker() -> None:
    assert email_normalize._raw_encoded_words_are_defective(
        b"broken =?utf-8?b?not-closed"
    )


def test_raw_encoded_word_detection_rejects_invalid_q_escape() -> None:
    assert email_normalize._raw_encoded_words_are_defective(
        b"=?utf-8?q?bad=GZ?="
    )


def test_split_mime_parameters_handles_quoted_escape_and_semicolon() -> None:
    value = b'attachment; filename="a\\\";b.txt"; x=1'

    assert email_normalize._split_mime_parameters(value) == [
        b"attachment",
        b' filename="a\\\";b.txt"',
        b" x=1",
    ]


def test_semantic_fingerprint_includes_defective_transfer_wire() -> None:
    part = email_normalize._parse_message(
        b"Content-Type: application/octet-stream\r\n"
        b"Content-Transfer-Encoding: base64\r\n\r\n"
        b"QQ==\xff"
    )
    part.get_payload(decode=True)

    fingerprint = email_normalize._semantic_part_fingerprint(part)

    assert "transfer_wire" in fingerprint


def test_transfer_descriptor_handles_non_string_raw_payload() -> None:
    class InvalidBase64TestDefect(Exception):
        pass

    class FakePart:
        defects = [InvalidBase64TestDefect()]

        def get_payload(self, decode: bool = False) -> bytes:
            return b"raw"

        def get(self, name: str, default: str = "") -> str:
            return "base64"

        def as_bytes(self, policy: object = None) -> bytes:
            return (
                b"Content-Transfer-Encoding: base64\n\n"
                b"cmF3"
            )

    assert email_normalize._transfer_decode_descriptor(FakePart()) is None  # type: ignore[arg-type]


def test_transfer_descriptor_handles_non_base64_transfer_encoding() -> None:
    class InvalidQuotedPrintableTestDefect(Exception):
        pass

    class FakePart:
        defects = [InvalidQuotedPrintableTestDefect()]

        def get_payload(self, decode: bool = False) -> str:
            return "raw=ZZ"

        def get(self, name: str, default: str = "") -> str:
            return "quoted-printable"

        def as_bytes(self, policy: object = None) -> bytes:
            return (
                b"Content-Transfer-Encoding: quoted-printable\n\n"
                b"raw=ZZ"
            )

    descriptor = email_normalize._transfer_decode_descriptor(FakePart())  # type: ignore[arg-type]

    assert descriptor is not None
    assert descriptor["encoding"] == "quoted-printable"


def test_valid_padded_base64_encoded_word_matches_q_encoding_identity() -> None:
    base64_subject = (
        b"Subject: =?utf-8?b?QQ==?=\r\n"
        b"From: sender@example.test\r\n\r\n"
        b"Friday at 5"
    )
    q_subject = (
        b"Subject: =?utf-8?q?A?=\r\n"
        b"From: sender@example.test\r\n\r\n"
        b"Friday at 5"
    )

    assert stable_email_identity(base64_subject) == stable_email_identity(q_subject)


def test_rfc2231_filename_continuation_order_is_canonical() -> None:
    def build(parameters: bytes) -> bytes:
        return (
            b"Subject: Attachment\r\n"
            b"Content-Type: multipart/mixed; boundary=part\r\n\r\n"
            b"--part\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Friday at 5\r\n"
            b"--part\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Disposition: attachment; "
            + parameters
            + b"\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"YWJj\r\n"
            b"--part--\r\n"
        )

    ordered = build(
        b"filename*0*=utf-8''foo%FF; filename*1*=bar.txt"
    )
    reversed_segments = build(
        b"filename*1*=bar.txt; filename*0*=utf-8''foo%FF"
    )

    assert stable_email_identity(ordered) == stable_email_identity(
        reversed_segments
    )


def test_rejected_content_id_folding_does_not_change_identity() -> None:
    def build(content_id: bytes) -> bytes:
        return (
            b"Subject: Attachment\r\n"
            b"Content-Type: multipart/mixed; boundary=part\r\n\r\n"
            b"--part\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Friday at 5\r\n"
            b"--part\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Content-ID: "
            + content_id
            + b"\r\n"
            b"Content-Disposition: attachment; filename=\"file.bin\"\r\n\r\n"
            b"abc\r\n"
            b"--part--\r\n"
        )

    unfolded = build(b"broken- id")
    folded = build(b"broken-\r\n id")

    assert stable_email_identity(unfolded) == stable_email_identity(folded)


def test_encapsulated_defective_identity_headers_preserve_raw_values() -> None:
    def build(subject: bytes) -> bytes:
        return (
            b"Subject: Outer\r\n"
            b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
            b"--outer\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Outer invitation\r\n"
            b"--outer\r\n"
            b"Content-Type: message/rfc822\r\n\r\n"
            b"Subject: "
            + subject
            + b"\r\n\r\n"
            b"Forwarded body\r\n"
            b"--outer--\r\n"
        )

    assert stable_email_identity(
        build(b"=?utf-8?b?QQ=A?=")
    ) != stable_email_identity(
        build(b"=?utf-8?b?QQ=B?=")
    )


def test_encapsulated_content_type_charset_is_semantic() -> None:
    def build(charset: bytes) -> bytes:
        return (
            b"Subject: Outer\r\n"
            b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
            b"--outer\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Outer invitation\r\n"
            b"--outer\r\n"
            b"Content-Type: message/rfc822\r\n\r\n"
            b"Subject: Forwarded\r\n"
            b"Content-Type: text/plain; charset="
            + charset
            + b"\r\n\r\n"
            b"caf\xe9\r\n"
            b"--outer--\r\n"
        )

    assert stable_email_identity(
        build(b"iso-8859-1")
    ) != stable_email_identity(
        build(b"utf-8")
    )


def test_encapsulated_defective_filename_parameters_are_preserved() -> None:
    def build(filename: bytes) -> bytes:
        return (
            b"Subject: Outer\r\n"
            b"Content-Type: multipart/mixed; boundary=outer\r\n\r\n"
            b"--outer\r\n"
            b"Content-Type: text/plain\r\n\r\n"
            b"Outer invitation\r\n"
            b"--outer\r\n"
            b"Content-Type: message/rfc822\r\n\r\n"
            b"Subject: Forwarded\r\n"
            b"Content-Type: multipart/mixed; boundary=inner\r\n\r\n"
            b"--inner\r\n"
            b"Content-Type: application/octet-stream\r\n"
            b"Content-Disposition: attachment; filename=\""
            + filename
            + b"\"\r\n\r\n"
            b"abc\r\n"
            b"--inner--\r\n"
            b"--outer--\r\n"
        )

    assert stable_email_identity(
        build(b"=?utf-8?b?QQ=A?=")
    ) != stable_email_identity(
        build(b"=?utf-8?b?QQ=B?=")
    )


def test_multipart_alternative_prefers_last_same_type_representation() -> None:
    message = EmailMessage()
    message.make_alternative()

    first = EmailMessage()
    first.set_content("Older plain representation")
    second = EmailMessage()
    second.set_content("Preferred plain representation")
    message.attach(first)
    message.attach(second)

    source = normalize_email(_envelope(message.as_bytes(policy=policy.default)))

    assert source.text == "Preferred plain representation"


def test_related_duplicate_start_parameters_fall_back_to_first_child() -> None:
    raw = (
        b"Subject: Related\r\n"
        b"Content-Type: multipart/related; boundary=rel; "
        b"start=\"<root@id>\"; start=\"<other@id>\"\r\n\r\n"
        b"--rel\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-ID: <fallback@id>\r\n\r\n"
        b"Fallback first child\r\n"
        b"--rel\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-ID: <root@id>\r\n\r\n"
        b"Root child\r\n"
        b"--rel--\r\n"
    )

    assert normalize_email(_envelope(raw)).text == "Fallback first child"


def test_related_duplicate_matching_children_fall_back_to_first_child() -> None:
    raw = (
        b"Subject: Related\r\n"
        b"Content-Type: multipart/related; boundary=rel; start=\"<root@id>\"\r\n\r\n"
        b"--rel\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-ID: <fallback@id>\r\n\r\n"
        b"Fallback first child\r\n"
        b"--rel\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-ID: <root@id>\r\n\r\n"
        b"First matching child\r\n"
        b"--rel\r\n"
        b"Content-Type: text/plain\r\n"
        b"Content-ID: <root@id>\r\n\r\n"
        b"Second matching child\r\n"
        b"--rel--\r\n"
    )

    assert normalize_email(_envelope(raw)).text == "Fallback first child"


def test_defective_transfer_identity_ignores_outer_transport_syntax() -> None:
    def build(boundary: bytes, received: bytes) -> bytes:
        return (
            b"Received: "
            + received
            + b"\r\n"
            b"Subject: Outer\r\n"
            b"Content-Type: multipart/mixed; boundary="
            + boundary
            + b"\r\n\r\n--"
            + boundary
            + b"\r\nContent-Type: text/plain\r\n\r\n"
            b"Friday at 5\r\n--"
            + boundary
            + b"\r\nContent-Type: application/octet-stream\r\n"
            b"Content-Disposition: attachment; filename=\"file.bin\"\r\n"
            b"Content-Transfer-Encoding: base64\r\n\r\n"
            b"QQ==\xff\r\n--"
            + boundary
            + b"--\r\n"
        )

    first = build(b"one", b"mx-one")
    second = build(b"two", b"mx-two")

    assert stable_email_identity(first) == stable_email_identity(second)


def test_malformed_quoted_printable_wire_disambiguates_identity() -> None:
    malformed = (
        b"Subject: QP\r\n"
        b"Content-Type: text/plain; charset=us-ascii\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"A="
    )
    plain = (
        b"Subject: QP\r\n"
        b"Content-Type: text/plain; charset=us-ascii\r\n"
        b"Content-Transfer-Encoding: quoted-printable\r\n\r\n"
        b"A"
    )

    assert normalize_email(_envelope(malformed)).text == "A"
    assert normalize_email(_envelope(plain)).text == "A"
    assert stable_email_identity(malformed) != stable_email_identity(plain)


def test_raw_encoded_word_detection_rejects_unmatched_marker_before_match() -> None:
    assert email_normalize._raw_encoded_words_are_defective(
        b"=?broken =?utf-8?q?A?="
    )


def test_raw_encoded_word_detection_rejects_unmatched_marker_after_match() -> None:
    assert email_normalize._raw_encoded_words_are_defective(
        b"=?utf-8?q?A?= trailing =?broken"
    )


@pytest.mark.parametrize(
    ("key", "expected"),
    (
        ("filename*", (0, -1, 1, "filename*")),
        ("filename*2*", (1, 2, 1, "filename*2*")),
        ("filename*2", (1, 2, 0, "filename*2")),
        ("filename*weird", (2, 0, 0, "filename*weird")),
    ),
)
def test_mime_parameter_sort_key_forms(
    key: str,
    expected: tuple[object, ...],
) -> None:
    assert email_normalize._mime_parameter_sort_key(
        key,
        "filename",
    ) == expected


def test_semantic_content_type_parameters_canonicalize_start() -> None:
    part = email_normalize._parse_message(
        b"Content-Type: multipart/related; boundary=rel; "
        b"start=\"(note) <root@id>\"\r\n\r\n"
        b"--rel--\r\n"
    )

    assert ["start", "<root@id>"] in (
        email_normalize._semantic_content_type_parameters(part)
    )


def test_semantic_fingerprint_records_empty_lossy_filename_raw_parameters() -> None:
    class LossyFilenamePart:
        def get_filename(self) -> str:
            return "file�.txt"

        def get_all(self, name: str, default: object = None) -> list[object]:
            return []

        def get_content_type(self) -> str:
            return "application/octet-stream"

        def get_content_disposition(self) -> str:
            return "attachment"

        def raw_items(self) -> list[tuple[str, str]]:
            return []

        def is_multipart(self) -> bool:
            return False

        def get_payload(self, decode: bool = False) -> bytes:
            return b"abc"

        def get(self, name: str, default: str = "") -> str:
            return default

        def get_params(
            self,
            failobj: object = None,
            header: str = "content-type",
            unquote: bool = True,
        ) -> list[tuple[str, str]]:
            return [("application/octet-stream", "")]

        def as_bytes(self, policy: object = None) -> bytes:
            return b"Content-Type: application/octet-stream\n\nabc"

    fingerprint = email_normalize._semantic_part_fingerprint(
        LossyFilenamePart()  # type: ignore[arg-type]
    )

    assert fingerprint["filename_raw_parameters"] == []


def test_serialized_part_payload_without_separator_is_empty() -> None:
    class FakePart:
        def as_bytes(self, policy: object = None) -> bytes:
            return b"header-without-separator"

    assert email_normalize._serialized_part_payload_bytes(
        FakePart()  # type: ignore[arg-type]
    ) == b""


def test_quoted_printable_soft_line_break_is_valid() -> None:
    assert not email_normalize._quoted_printable_wire_is_defective(
        b"A=\nB"
    )


def test_quoted_printable_short_hex_escape_is_defective() -> None:
    assert email_normalize._quoted_printable_wire_is_defective(b"A=F")


def test_semantic_content_type_parameters_preserve_generic_parameter_value() -> None:
    part = email_normalize._parse_message(
        b"Content-Type: text/plain; x-mode=PreserveCase\r\n\r\nBody"
    )

    assert ["x-mode", "PreserveCase"] in (
        email_normalize._semantic_content_type_parameters(part)
    )
