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
        "Team logo Unsafe"
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
