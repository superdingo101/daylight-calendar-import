"""Transport-neutral email-source contract tests."""

from collections.abc import AsyncIterator
from datetime import UTC, datetime

from custom_components.daylight_calendar_import.email.source import (
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
        self.acknowledged: tuple[EmailEnvelope, EmailDisposition] | None = None

    @property
    def config(self) -> EmailSourceConfig:
        return self._config

    async def async_collect(self) -> AsyncIterator[EmailEnvelope]:
        yield self._envelope

    async def async_acknowledge(
        self,
        envelope: EmailEnvelope,
        *,
        disposition: EmailDisposition,
    ) -> None:
        self.acknowledged = (envelope, disposition)


def _envelope() -> EmailEnvelope:
    return EmailEnvelope(
        received_at=datetime(2026, 10, 1, 12, 30, tzinfo=UTC),
        raw_message=b"Message-ID: <event@example.test>\r\n\r\nEvent details",
        provenance=EmailProvenance(
            source_id="mailbox-1",
            source_type=EmailSourceType.DIRECT_IMAP,
            transport_reference="uid:42",
            mailbox="INBOX",
        ),
        upstream_source_id="<event@example.test>",
    )


def test_email_source_config_defaults_to_direct_imap_and_preserve() -> None:
    config = EmailSourceConfig(source_id="mailbox-1")

    assert config.source_type is EmailSourceType.DIRECT_IMAP
    assert config.source_type.value == "direct_imap"
    assert config.disposition == EmailDisposition()
    assert config.disposition.mark_seen is False
    assert config.disposition.move_to_folder is None
    assert config.disposition.add_flag is None


def test_email_envelope_keeps_transport_state_out_of_source_domain() -> None:
    envelope = _envelope()

    assert envelope.received_at.tzinfo is UTC
    assert envelope.raw_message.endswith(b"Event details")
    assert envelope.upstream_source_id == "<event@example.test>"
    assert envelope.provenance == EmailProvenance(
        source_id="mailbox-1",
        source_type=EmailSourceType.DIRECT_IMAP,
        transport_reference="uid:42",
        mailbox="INBOX",
    )


async def test_email_source_protocol_supports_collect_and_acknowledge() -> None:
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
    await source.async_acknowledge(envelope, disposition=disposition)
    assert source.acknowledged == (envelope, disposition)
