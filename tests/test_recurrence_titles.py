"""Source titles remain event evidence through the live provider boundary."""

import json

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from custom_components.daylight_calendar_import import providers
from custom_components.daylight_calendar_import.email_normalize import normalize_email
from custom_components.daylight_calendar_import.email_source import (
    DirectImapReference, EmailEnvelope, EmailProvenance, EmailSourceType,
)
from custom_components.daylight_calendar_import.models import UNSUPPORTED_RECURRENCE_WARNING
from custom_components.daylight_calendar_import.parser import PROMPT_TEMPLATE
from custom_components.daylight_calendar_import.sources import (
    SourceAttachment, SourceDocument, SourceKind,
)

REFERENCE = "2026-10-01T12:00:00+00:00"
BODY = "Join us on October 8, 2026, from 17:30 to 18:30 UTC.\nBring water."
EVENT = {
    "title": "Staff meeting",
    "start": "2026-10-08T17:30:00+00:00",
    "end": "2026-10-08T18:30:00+00:00",
    "all_day": False,
    "confidence": 0.95,
    "assumptions": [],
}


@pytest.fixture
def provider(monkeypatch):
    """Simulate a provider false negative without bypassing the adapter."""
    generate = AsyncMock(return_value=SimpleNamespace(data={
        "unsupported_recurrence": False, "events": [EVENT],
    }))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    entity = SimpleNamespace(
        supported_features=providers.AITaskEntityFeature.SUPPORT_ATTACHMENTS,
    )
    component = SimpleNamespace(get_entity=lambda _id: entity)
    hass = SimpleNamespace(data={providers.DATA_COMPONENT: component})
    return providers.AITaskParserProvider(hass, "ai_task.test"), generate


async def test_normalized_email_subject_reaches_prompt_and_recurrence_guard(provider):
    adapter, generate = provider
    source = normalize_email(EmailEnvelope(
        received_at=datetime(2026, 10, 1, tzinfo=UTC),
        raw_message=("Subject: Weekly staff meeting\r\n\r\n" + BODY).encode(),
        provenance=EmailProvenance(
            source_id="test-mailbox", source_type=EmailSourceType.DIRECT_IMAP,
            transport_reference=DirectImapReference("INBOX", 1, 1),
        ),
    ))

    outcome = await adapter.async_parse(source, reference_datetime=REFERENCE, time_zone="UTC")

    assert source.title == "Weekly staff meeting"
    assert source.text == BODY
    assert outcome.events == []
    assert outcome.warnings == [UNSUPPORTED_RECURRENCE_WARNING]
    assert generate.await_args.kwargs["instructions"] == PROMPT_TEMPLATE.format(
        text=json.dumps(f"Source title: Weekly staff meeting\n\nSource body:\n{BODY}", ensure_ascii=False),
        reference_datetime=REFERENCE, time_zone="UTC",
    )


@pytest.mark.parametrize("body", (None, "", "  "))
async def test_title_is_evidence_for_attachment_only_source(provider, body):
    adapter, generate = provider
    attachment = SourceAttachment("image", "image/png", 25, "media-source://test/image.png")
    source = SourceDocument(
        "attachment", SourceKind.IMAGE, datetime.now(UTC), text=body,
        title="Weekly team meeting", attachments=(attachment,),
    )

    outcome = await adapter.async_parse(source, reference_datetime=REFERENCE, time_zone="UTC")

    assert outcome.events == []
    assert outcome.warnings == [UNSUPPORTED_RECURRENCE_WARNING]
    assert "Source title: Weekly team meeting" in generate.await_args.kwargs["instructions"]
    assert generate.await_args.kwargs["attachments"] == [{
        "media_content_id": attachment.content_ref, "media_content_type": "image/png",
    }]


async def test_generic_weekly_newsletter_title_preserves_one_off_event(provider):
    adapter, generate = provider
    source = SourceDocument(
        "newsletter", SourceKind.EMAIL, datetime.now(UTC), text=BODY,
        title="Weekly newsletter",
    )

    outcome = await adapter.async_parse(source, reference_datetime=REFERENCE, time_zone="UTC")

    assert len(outcome.events) == 1
    assert outcome.warnings == []
    assert "Weekly newsletter" in generate.await_args.kwargs["instructions"]


async def test_title_does_not_replace_recurring_body(provider):
    adapter, generate = provider
    body = "Practice every Thursday at 17:30."
    source = SourceDocument(
        "newsletter", SourceKind.EMAIL, datetime.now(UTC), text=body,
        title="Weekly newsletter",
    )

    outcome = await adapter.async_parse(source, reference_datetime=REFERENCE, time_zone="UTC")

    assert outcome.events == []
    assert outcome.warnings == [UNSUPPORTED_RECURRENCE_WARNING]
    assert body in generate.await_args.kwargs["instructions"]


@pytest.mark.parametrize("title", (None, "", " \n "))
async def test_untitled_source_keeps_existing_body_prompt(provider, title):
    adapter, generate = provider
    source = SourceDocument(
        "untitled", SourceKind.MANUAL_TEXT, datetime.now(UTC),
        text=f"  {BODY}  ", title=title,
    )

    outcome = await adapter.async_parse(source, reference_datetime=REFERENCE, time_zone="UTC")

    assert len(outcome.events) == 1
    assert outcome.warnings == []
    assert generate.await_args.kwargs["instructions"] == PROMPT_TEMPLATE.format(
        text=json.dumps(BODY, ensure_ascii=False), reference_datetime=REFERENCE, time_zone="UTC",
    )


async def test_title_alone_does_not_satisfy_source_validation(provider):
    adapter, generate = provider
    source = SourceDocument(
        "empty", SourceKind.EMAIL, datetime.now(UTC), title="Weekly staff meeting",
    )

    with pytest.raises(providers.SourceValidationError) as error:
        await adapter.async_parse(source, reference_datetime=REFERENCE, time_zone="UTC")

    assert error.value.code == "empty_source"
    generate.assert_not_awaited()
