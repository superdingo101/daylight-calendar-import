"""Normalized source and text-adapter behavior."""

from datetime import UTC
from uuid import UUID

import pytest

from custom_components.daylight_calendar_import.sources import (
    SourceAttachment,
    SourceDocument,
    SourceKind,
    TextSourceAdapter,
)


def test_text_adapter_normalizes_identity_and_upstream_id():
    adapter = TextSourceAdapter()
    first = adapter.create("  Soccer practice Thursday  ", source_id="message-123")
    second = adapter.create("Soccer practice Thursday")

    UUID(first.id)
    assert first.id != second.id
    assert first.kind is SourceKind.MANUAL_TEXT
    assert first.received_at.tzinfo is UTC
    assert first.text == "Soccer practice Thursday"
    assert first.upstream_source_id == "message-123"
    assert first.attachments == ()
    assert first.metadata == {}
    assert second.upstream_source_id is None
    assert second.metadata is not first.metadata


def test_text_adapter_rejects_empty_source():
    with pytest.raises(ValueError, match="text must not be empty"):
        TextSourceAdapter().create("  ")


def test_source_contract_can_describe_image_pdf_or_combined_text():
    attachment = SourceAttachment(
        id="page-1", media_type="application/pdf", size_bytes=42,
        content_ref="upload:page-1", filename="flyer.pdf",
    )
    pdf = SourceDocument(
        id="source-1", kind=SourceKind.PDF, received_at=TextSourceAdapter().create("x").received_at,
        attachments=(attachment,),
    )
    assert pdf.text is None
    assert pdf.attachments == (attachment,)
    assert SourceDocument(
        id="source-2", kind=SourceKind.IMAGE, received_at=pdf.received_at,
        text="Invitation details", attachments=(attachment,),
    ).text == "Invitation details"
    assert SourceKind.EMAIL.value == "email"
