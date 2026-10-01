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
        transport_reference=DirectImapReference(uid_validity=1234, uid=42),
        mailbox="INBOX",
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
    ("uid_validity", "uid", "message"),
    (
        (0, 42, "uid_validity must be positive"),
        (-1, 42, "uid_validity must be positive"),
        (1234, 0, "uid must be positive"),
        (1234, -1, "uid must be positive"),
    ),
)
def test_direct_imap_reference_rejects_invalid_identifiers(
    uid_validity: int, uid: int, message: str
) -> None:
    with pytest.raises(ValueError, match=message):
        DirectImapReference(uid_validity=uid_validity, uid=uid)


def test_email_envelope_keeps_sensitive_raw_message_out_of_repr() -> None:
    envelope = _envelope()

    assert envelope.received_at.tzinfo is UTC
    assert envelope.raw_message.endswith(b"Event details")
    assert envelope.upstream_source_id == "<event@example.test>"
    assert envelope.provenance == _provenance()
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
