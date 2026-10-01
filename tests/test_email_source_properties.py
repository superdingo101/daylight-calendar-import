"""Property tests for transport-neutral email-source contracts."""

from datetime import UTC, datetime

from hypothesis import given, settings, strategies as st

from custom_components.daylight_calendar_import.email.source import (
    EmailDisposition,
    EmailEnvelope,
    EmailProvenance,
    EmailSourceConfig,
    EmailSourceType,
)


PROPERTY_SETTINGS = settings(max_examples=100, deadline=None, derandomize=True)
NONEMPTY_TEXT = st.text(min_size=1, max_size=80).filter(lambda value: bool(value.strip()))
OPTIONAL_TEXT = st.one_of(st.none(), NONEMPTY_TEXT)


@PROPERTY_SETTINGS
@given(
    source_id=NONEMPTY_TEXT,
    transport_reference=NONEMPTY_TEXT,
    mailbox=OPTIONAL_TEXT,
    upstream_source_id=OPTIONAL_TEXT,
    raw_message=st.binary(max_size=512),
)
def test_email_envelope_contract_preserves_transport_neutral_values(
    source_id: str,
    transport_reference: str,
    mailbox: str | None,
    upstream_source_id: str | None,
    raw_message: bytes,
) -> None:
    provenance = EmailProvenance(
        source_id=source_id,
        source_type=EmailSourceType.DIRECT_IMAP,
        transport_reference=transport_reference,
        mailbox=mailbox,
    )
    envelope = EmailEnvelope(
        received_at=datetime(2026, 10, 1, tzinfo=UTC),
        raw_message=raw_message,
        provenance=provenance,
        upstream_source_id=upstream_source_id,
    )

    assert envelope.raw_message == raw_message
    assert envelope.provenance == provenance
    assert envelope.upstream_source_id == upstream_source_id


@PROPERTY_SETTINGS
@given(
    source_id=NONEMPTY_TEXT,
    mark_seen=st.booleans(),
    move_to_folder=OPTIONAL_TEXT,
    add_flag=OPTIONAL_TEXT,
)
def test_email_source_config_preserves_acknowledgement_policy(
    source_id: str,
    mark_seen: bool,
    move_to_folder: str | None,
    add_flag: str | None,
) -> None:
    disposition = EmailDisposition(
        mark_seen=mark_seen,
        move_to_folder=move_to_folder,
        add_flag=add_flag,
    )
    config = EmailSourceConfig(source_id=source_id, disposition=disposition)

    assert config.source_id == source_id
    assert config.source_type is EmailSourceType.DIRECT_IMAP
    assert config.disposition == disposition
