"""Property-based tests for deterministic domain contracts."""

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta, timezone
import json

from hypothesis import given, settings, strategies as st
import pytest

from custom_components.daylight_calendar_import.dedup import (
    event_fingerprint,
    source_fingerprint,
)
from custom_components.daylight_calendar_import.models import (
    DraftValidationError,
    EventDraft,
)
from custom_components.daylight_calendar_import.providers import (
    IMAGE_MEDIA_TYPES,
    PDF_MEDIA_TYPE,
    ParserCapabilities,
    SourceValidationError,
)
from custom_components.daylight_calendar_import.sources import (
    SourceAttachment,
    SourceDocument,
    SourceKind,
)
from custom_components.daylight_calendar_import.storage import (
    PendingEvent,
    PendingImport,
)


PROPERTY_SETTINGS = settings(max_examples=100, deadline=None, derandomize=True)

_TEXT_ALPHABET = st.characters(blacklist_categories=("Cs",))
NONEMPTY_TEXT = st.text(
    alphabet=_TEXT_ALPHABET,
    min_size=1,
    max_size=80,
).map(lambda value: value.strip() or "x")
OPTIONAL_TEXT = st.one_of(st.none(), NONEMPTY_TEXT)
VALID_CONFIDENCE = st.floats(
    min_value=0.0,
    max_value=1.0,
    allow_nan=False,
    allow_infinity=False,
)
UTC_DATETIMES = st.datetimes(
    min_value=datetime(2000, 1, 1),
    max_value=datetime(2035, 12, 1),
    timezones=st.just(UTC),
)
POSITIVE_DURATION = st.timedeltas(
    min_value=timedelta(microseconds=1),
    max_value=timedelta(days=30),
)
FIXED_OFFSETS = st.integers(
    min_value=-12 * 60,
    max_value=14 * 60,
).map(lambda minutes: timezone(timedelta(minutes=minutes)))


@st.composite
def event_drafts(draw) -> EventDraft:
    """Generate valid timed and all-day event drafts."""
    all_day = draw(st.booleans())
    if all_day:
        start_date = draw(
            st.dates(
                min_value=date(2000, 1, 1),
                max_value=date(2035, 12, 1),
            )
        )
        days = draw(st.integers(min_value=1, max_value=365))
        start = start_date.isoformat()
        end = (start_date + timedelta(days=days)).isoformat()
    else:
        start_utc = draw(UTC_DATETIMES)
        duration = draw(POSITIVE_DURATION)
        end_utc = start_utc + duration
        start_offset = draw(FIXED_OFFSETS)
        end_offset = draw(FIXED_OFFSETS)
        start = start_utc.astimezone(start_offset).isoformat()
        end = end_utc.astimezone(end_offset).isoformat()

    return EventDraft.from_mapping(
        {
            "title": draw(NONEMPTY_TEXT),
            "start": start,
            "end": end,
            "all_day": all_day,
            "location": draw(OPTIONAL_TEXT),
            "description": draw(OPTIONAL_TEXT),
            "confidence": draw(VALID_CONFIDENCE),
        }
    )


@st.composite
def source_attachments(draw) -> SourceAttachment:
    """Generate arbitrary attachment contract values."""
    return SourceAttachment(
        id=draw(NONEMPTY_TEXT),
        media_type=draw(
            st.sampled_from(
                (
                    "image/jpeg",
                    "image/png",
                    "image/webp",
                    PDF_MEDIA_TYPE,
                    "application/octet-stream",
                    "text/plain",
                )
            )
        ),
        size_bytes=draw(st.integers(min_value=0, max_value=20 * 1024 * 1024)),
        content_ref=draw(NONEMPTY_TEXT),
        filename=draw(OPTIONAL_TEXT),
        sha256=draw(
            st.one_of(
                st.none(),
                st.text(
                    alphabet="0123456789abcdef",
                    min_size=64,
                    max_size=64,
                ),
            )
        ),
    )


@st.composite
def pending_events(draw) -> PendingEvent:
    """Generate storage-safe pending events."""
    return PendingEvent(
        id=draw(NONEMPTY_TEXT),
        draft=draw(event_drafts()),
        status=draw(st.sampled_from(("pending", "write_uncertain"))),
        calendar_entity=draw(OPTIONAL_TEXT),
    )


@st.composite
def pending_imports(draw) -> PendingImport:
    """Generate serializable pending imports."""
    created = draw(UTC_DATETIMES).isoformat()
    events = tuple(
        draw(st.lists(pending_events(), min_size=1, max_size=4))
    )
    return PendingImport(
        id=draw(NONEMPTY_TEXT),
        created_at=created,
        source_text=draw(NONEMPTY_TEXT),
        events=events,
        source_fingerprint=draw(OPTIONAL_TEXT),
    )


@PROPERTY_SETTINGS
@given(draft=event_drafts())
def test_event_draft_round_trips_through_mapping(draft: EventDraft) -> None:
    """Every generated valid draft survives its public mapping contract."""
    assert EventDraft.from_mapping(draft.as_dict()) == draft


@PROPERTY_SETTINGS
@given(draft=event_drafts())
def test_event_draft_rejects_zero_length_range(draft: EventDraft) -> None:
    """Equal start and end values are invalid for every valid event shape."""
    raw = draft.as_dict()
    raw["end"] = raw["start"]
    with pytest.raises(DraftValidationError, match="end must be after start"):
        EventDraft.from_mapping(raw)


@PROPERTY_SETTINGS
@given(confidence=st.sampled_from((float("nan"), float("inf"), float("-inf"))))
def test_event_draft_rejects_non_finite_confidence(confidence: float) -> None:
    """Non-finite confidence values never enter the domain model."""
    with pytest.raises(DraftValidationError, match="confidence must be between 0 and 1"):
        EventDraft.from_mapping(
            {
                "title": "Event",
                "start": "2026-10-08T17:30:00+00:00",
                "end": "2026-10-08T18:30:00+00:00",
                "all_day": False,
                "confidence": confidence,
            }
        )


@PROPERTY_SETTINGS
@given(draft=event_drafts(), confidence=VALID_CONFIDENCE)
def test_event_fingerprint_ignores_confidence(
    draft: EventDraft, confidence: float
) -> None:
    """AI confidence is intentionally outside event identity."""
    assert event_fingerprint(draft) == event_fingerprint(
        replace(draft, confidence=confidence)
    )


@PROPERTY_SETTINGS
@given(draft=event_drafts())
def test_event_fingerprint_normalizes_whitespace(draft: EventDraft) -> None:
    """Equivalent surrounding and repeated whitespace preserves identity."""
    title = "   ".join(draft.title.split())
    variant = replace(draft, title=f" \t{title}\n ")
    assert event_fingerprint(draft) == event_fingerprint(variant)


@PROPERTY_SETTINGS
@given(
    start_utc=UTC_DATETIMES,
    duration=POSITIVE_DURATION,
    first_offset=FIXED_OFFSETS,
    second_offset=FIXED_OFFSETS,
    title=NONEMPTY_TEXT,
)
def test_event_fingerprint_normalizes_equivalent_timezone_instants(
    start_utc: datetime,
    duration: timedelta,
    first_offset: timezone,
    second_offset: timezone,
    title: str,
) -> None:
    """Equivalent instants written with different offsets fingerprint equally."""
    end_utc = start_utc + duration
    first = EventDraft(
        title=title,
        start=start_utc.astimezone(first_offset).isoformat(),
        end=end_utc.astimezone(first_offset).isoformat(),
        all_day=False,
    )
    second = EventDraft(
        title=title,
        start=start_utc.astimezone(second_offset).isoformat(),
        end=end_utc.astimezone(second_offset).isoformat(),
        all_day=False,
    )
    assert event_fingerprint(first) == event_fingerprint(second)


@PROPERTY_SETTINGS
@given(draft=event_drafts())
def test_event_fingerprint_changes_when_end_changes(draft: EventDraft) -> None:
    """A meaningful temporal change produces a distinct event identity."""
    if draft.all_day:
        changed_end = (
            date.fromisoformat(draft.end) + timedelta(days=1)
        ).isoformat()
    else:
        changed_end = (
            datetime.fromisoformat(draft.end) + timedelta(seconds=1)
        ).isoformat()
    assert event_fingerprint(draft) != event_fingerprint(
        replace(draft, end=changed_end)
    )


@PROPERTY_SETTINGS
@given(source_id=NONEMPTY_TEXT)
def test_source_fingerprint_normalizes_outer_whitespace(source_id: str) -> None:
    """Opaque source IDs are stable across accidental outer whitespace."""
    assert source_fingerprint(source_id) == source_fingerprint(
        f" \t{source_id}\n "
    )


@PROPERTY_SETTINGS
@given(limit=st.integers(min_value=1, max_value=2 * 1024 * 1024))
def test_parser_capabilities_enforce_exact_total_byte_boundary(limit: int) -> None:
    """The exact byte limit is accepted and one byte more is rejected."""
    capabilities = ParserCapabilities(
        text=False,
        images=True,
        pdfs=False,
        max_attachments=1,
        max_total_bytes=limit,
    )
    attachment = SourceAttachment(
        id="image",
        media_type="image/png",
        size_bytes=limit,
        content_ref="upload:image",
    )
    source = SourceDocument(
        id="source",
        kind=SourceKind.IMAGE,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        attachments=(attachment,),
    )
    capabilities.validate(source)

    oversized = replace(
        source,
        attachments=(replace(attachment, size_bytes=limit + 1),),
    )
    with pytest.raises(SourceValidationError) as err:
        capabilities.validate(oversized)
    assert err.value.code == "source_too_large"


@PROPERTY_SETTINGS
@given(max_attachments=st.integers(min_value=1, max_value=6))
def test_parser_capabilities_enforce_attachment_count_boundary(
    max_attachments: int,
) -> None:
    """Exactly the configured attachment count is accepted, then rejected."""
    capabilities = ParserCapabilities(
        text=False,
        images=True,
        pdfs=False,
        max_attachments=max_attachments,
        max_total_bytes=max_attachments + 1,
    )
    attachment = SourceAttachment(
        id="image",
        media_type="image/jpeg",
        size_bytes=1,
        content_ref="upload:image",
    )
    accepted = SourceDocument(
        id="source",
        kind=SourceKind.IMAGE,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        attachments=tuple(
            replace(attachment, id=f"image-{index}")
            for index in range(max_attachments)
        ),
    )
    capabilities.validate(accepted)

    rejected = replace(
        accepted,
        attachments=accepted.attachments
        + (replace(attachment, id="one-too-many"),),
    )
    with pytest.raises(SourceValidationError) as err:
        capabilities.validate(rejected)
    assert err.value.code == "too_many_attachments"


@PROPERTY_SETTINGS
@given(
    source_type=st.sampled_from(("text", "image", "pdf")),
    supported=st.booleans(),
)
def test_parser_capabilities_match_declared_media_support(
    source_type: str, supported: bool
) -> None:
    """Each declared capability independently controls its source type."""
    capabilities = ParserCapabilities(
        text=supported if source_type == "text" else True,
        images=supported if source_type == "image" else True,
        pdfs=supported if source_type == "pdf" else True,
        max_attachments=1,
        max_total_bytes=100,
    )
    if source_type == "text":
        source = SourceDocument(
            id="source",
            kind=SourceKind.MANUAL_TEXT,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
            text="event text",
        )
    else:
        media_type = "image/webp" if source_type == "image" else PDF_MEDIA_TYPE
        source = SourceDocument(
            id="source",
            kind=SourceKind.IMAGE if source_type == "image" else SourceKind.PDF,
            received_at=datetime(2026, 1, 1, tzinfo=UTC),
            attachments=(
                SourceAttachment(
                    id="attachment",
                    media_type=media_type,
                    size_bytes=1,
                    content_ref="upload:attachment",
                ),
            ),
        )

    if supported:
        capabilities.validate(source)
    else:
        with pytest.raises(SourceValidationError) as err:
            capabilities.validate(source)
        assert err.value.code == "unsupported_capability"


@PROPERTY_SETTINGS
@given(size=st.integers(max_value=0))
def test_parser_capabilities_reject_non_positive_attachment_sizes(
    size: int,
) -> None:
    """Zero and negative attachment sizes are always invalid."""
    capabilities = ParserCapabilities(
        text=False,
        images=True,
        pdfs=True,
        max_attachments=1,
        max_total_bytes=100,
    )
    source = SourceDocument(
        id="source",
        kind=SourceKind.IMAGE,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        attachments=(
            SourceAttachment(
                id="attachment",
                media_type="image/png",
                size_bytes=size,
                content_ref="upload:attachment",
            ),
        ),
    )
    with pytest.raises(SourceValidationError) as err:
        capabilities.validate(source)
    assert err.value.code == "empty_attachment"


@PROPERTY_SETTINGS
@given(
    media_type=st.sampled_from(
        ("image/gif", "application/zip", "text/plain", "video/mp4")
    )
)
def test_parser_capabilities_reject_unknown_media_types(media_type: str) -> None:
    """Unsupported MIME types cannot bypass otherwise-enabled capabilities."""
    assert media_type not in IMAGE_MEDIA_TYPES
    assert media_type != PDF_MEDIA_TYPE
    capabilities = ParserCapabilities(
        text=True,
        images=True,
        pdfs=True,
        max_attachments=1,
        max_total_bytes=100,
    )
    source = SourceDocument(
        id="source",
        kind=SourceKind.IMAGE,
        received_at=datetime(2026, 1, 1, tzinfo=UTC),
        attachments=(
            SourceAttachment(
                id="attachment",
                media_type=media_type,
                size_bytes=1,
                content_ref="upload:attachment",
            ),
        ),
    )
    with pytest.raises(SourceValidationError) as err:
        capabilities.validate(source)
    assert err.value.code == "unsupported_media"


@PROPERTY_SETTINGS
@given(attachment=source_attachments())
def test_source_attachment_contract_preserves_generated_values(
    attachment: SourceAttachment,
) -> None:
    """The attachment contract does not silently normalize metadata."""
    rebuilt = SourceAttachment(
        id=attachment.id,
        media_type=attachment.media_type,
        size_bytes=attachment.size_bytes,
        content_ref=attachment.content_ref,
        filename=attachment.filename,
        sha256=attachment.sha256,
    )
    assert rebuilt == attachment


@PROPERTY_SETTINGS
@given(
    kind=st.sampled_from(tuple(SourceKind)),
    received_at=UTC_DATETIMES,
    text=OPTIONAL_TEXT,
    title=OPTIONAL_TEXT,
    attachments=st.lists(source_attachments(), max_size=4),
    metadata=st.dictionaries(
        keys=NONEMPTY_TEXT,
        values=st.one_of(
            st.none(),
            st.booleans(),
            st.integers(),
            st.text(alphabet=_TEXT_ALPHABET, max_size=40),
        ),
        max_size=4,
    ),
    upstream_source_id=OPTIONAL_TEXT,
)
def test_source_document_contract_preserves_generated_values(
    kind: SourceKind,
    received_at: datetime,
    text: str | None,
    title: str | None,
    attachments: list[SourceAttachment],
    metadata: dict[str, object],
    upstream_source_id: str | None,
) -> None:
    """The normalized source contract preserves ordering and metadata exactly."""
    source = SourceDocument(
        id="source-id",
        kind=kind,
        received_at=received_at,
        text=text,
        title=title,
        attachments=tuple(attachments),
        metadata=metadata,
        upstream_source_id=upstream_source_id,
    )
    assert source.kind is kind
    assert source.received_at == received_at
    assert source.text == text
    assert source.title == title
    assert source.attachments == tuple(attachments)
    assert source.metadata == metadata
    assert source.upstream_source_id == upstream_source_id


@PROPERTY_SETTINGS
@given(event=pending_events())
def test_pending_event_json_storage_round_trip(event: PendingEvent) -> None:
    """Pending-event storage remains JSON-safe and lossless."""
    payload = json.loads(json.dumps(event.as_dict(), ensure_ascii=False))
    restored = PendingEvent.from_dict(payload)
    assert restored == event
    assert restored.as_dict() == payload


@PROPERTY_SETTINGS
@given(event=pending_events())
def test_pending_event_legacy_storage_without_calendar_entity(
    event: PendingEvent,
) -> None:
    """Older persisted events without a destination remain readable."""
    payload = event.as_dict()
    payload.pop("calendar_entity")
    restored = PendingEvent.from_dict(payload)
    assert restored.id == event.id
    assert restored.draft == event.draft
    assert restored.status == event.status
    assert restored.calendar_entity is None


@PROPERTY_SETTINGS
@given(pending=pending_imports())
def test_pending_import_json_storage_round_trip(pending: PendingImport) -> None:
    """Pending-import persistence is lossless across a JSON storage boundary."""
    payload = json.loads(json.dumps(pending.as_dict(), ensure_ascii=False))
    restored = PendingImport.from_dict(payload)
    assert restored == pending
    assert restored.as_dict() == payload
