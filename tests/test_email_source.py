"""Transport-neutral email-source contract tests."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

import pytest

from custom_components.daylight_calendar_import.email_source import (
    DirectImapReference,
    EmailDisposition,
    EmailEnvelope,
    EmailProvenance,
    EmailSource,
    EmailSourceConfig,
    EmailSourceType,
)


IMAP_MAX = 4_294_967_295
IMAP_OVER_MAX = 4_294_967_296


class FakeEmailSource:
    """Minimal alternate transport used to exercise the public contract."""

    def __init__(self, envelope: EmailEnvelope) -> None:
        self._envelope = envelope
        self._config = EmailSourceConfig(source_id="fake-source")
        self.acknowledged: tuple[EmailProvenance, EmailDisposition] | None = None

    @property
    def config(self) -> EmailSourceConfig:
        return self._config

    async def async_collect(self) -> AsyncIterator[EmailEnvelope]:
        yield self._envelope

    async def async_acknowledge(
        self,
        provenance: EmailProvenance,
        *,
        disposition: EmailDisposition,
    ) -> None:
        self.acknowledged = (provenance, disposition)


def _provenance() -> EmailProvenance:
    return EmailProvenance(
        source_id="mailbox-1",
        source_type=EmailSourceType.DIRECT_IMAP,
        transport_reference=DirectImapReference(
            mailbox="INBOX",
            uid_validity=1234,
            uid=42,
        ),
    )


def _envelope() -> EmailEnvelope:
    return EmailEnvelope(
        received_at=datetime(2026, 10, 1, 12, 30, tzinfo=UTC),
        raw_message=b"Message-ID: <event@example.test>\r\n\r\nEvent details",
        provenance=_provenance(),
        upstream_source_id="<event@example.test>",
    )


def test_email_source_config_defaults_to_direct_imap_and_non_destructive() -> None:
    first = EmailSourceConfig(source_id="mailbox-1")
    second = EmailSourceConfig(source_id="mailbox-2")

    assert first.source_type is EmailSourceType.DIRECT_IMAP
    assert first.source_type.value == "direct_imap"
    assert first.disposition == EmailDisposition()
    assert first.disposition.mark_seen is False
    assert first.disposition.move_to_folder is None
    assert first.disposition.add_flag is None
    assert first.disposition is not second.disposition


@pytest.mark.parametrize(
    ("mailbox", "message"),
    (
        ("", "mailbox must be a non-empty string"),
        ("   ", "mailbox must be a non-empty string"),
        (None, "mailbox must be a non-empty string"),
        (42, "mailbox must be a non-empty string"),
    ),
)
def test_direct_imap_reference_rejects_invalid_mailbox(
    mailbox: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        DirectImapReference(  # type: ignore[arg-type]
            mailbox=mailbox,
            uid_validity=1234,
            uid=42,
        )


@pytest.mark.parametrize(
    ("uid_validity", "uid", "message"),
    (
        (0, 42, "uid_validity must be a nonzero unsigned 32-bit integer"),
        (-1, 42, "uid_validity must be a nonzero unsigned 32-bit integer"),
        (IMAP_OVER_MAX, 42, "uid_validity must be a nonzero unsigned 32-bit integer"),
        (True, 42, "uid_validity must be a nonzero unsigned 32-bit integer"),
        (1.5, 42, "uid_validity must be a nonzero unsigned 32-bit integer"),
        (1234, 0, "uid must be a nonzero unsigned 32-bit integer"),
        (1234, -1, "uid must be a nonzero unsigned 32-bit integer"),
        (1234, IMAP_OVER_MAX, "uid must be a nonzero unsigned 32-bit integer"),
        (1234, True, "uid must be a nonzero unsigned 32-bit integer"),
        (1234, 1.5, "uid must be a nonzero unsigned 32-bit integer"),
    ),
)
def test_direct_imap_reference_rejects_invalid_identifiers(
    uid_validity: object, uid: object, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        DirectImapReference(  # type: ignore[arg-type]
            mailbox="INBOX",
            uid_validity=uid_validity,
            uid=uid,
        )


def test_direct_imap_reference_accepts_identifier_boundaries() -> None:
    low = DirectImapReference(mailbox="INBOX", uid_validity=1, uid=1)
    high = DirectImapReference(
        mailbox="Archive",
        uid_validity=IMAP_MAX,
        uid=IMAP_MAX,
    )

    assert (low.uid_validity, low.uid) == (1, 1)
    assert (high.uid_validity, high.uid) == (IMAP_MAX, IMAP_MAX)
    assert high.mailbox == "Archive"


def test_email_envelope_keeps_sensitive_raw_message_out_of_repr() -> None:
    envelope = _envelope()

    assert envelope.received_at.tzinfo is UTC
    assert envelope.raw_message.endswith(b"Event details")
    assert envelope.upstream_source_id == "<event@example.test>"
    assert envelope.provenance == _provenance()
    assert envelope.provenance.transport_reference.mailbox == "INBOX"
    assert "Event details" not in repr(envelope)
    assert "raw_message" not in repr(envelope)


async def test_email_source_protocol_acknowledges_persistable_provenance() -> None:
    envelope = _envelope()
    source = FakeEmailSource(envelope)

    assert isinstance(source, EmailSource)
    assert source.config == EmailSourceConfig(source_id="fake-source")
    assert [item async for item in source.async_collect()] == [envelope]

    disposition = EmailDisposition(
        mark_seen=True,
        move_to_folder="Processed",
        add_flag="daylight-processed",
    )
    await source.async_acknowledge(envelope.provenance, disposition=disposition)
    assert source.acknowledged == (envelope.provenance, disposition)
