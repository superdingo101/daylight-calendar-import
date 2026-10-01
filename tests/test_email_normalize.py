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
    assert email_normalize._canonical_message_id("not-an-id") is None


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
