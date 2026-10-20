"""Tests for the AI parsing boundary."""

from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.daylight_calendar_import import parser, providers
from custom_components.daylight_calendar_import.sources import SourceAttachment, SourceDocument, SourceKind, TextSourceAdapter


VALID = {
    "title": "Soccer Practice",
    "start": "2026-10-08T17:30:00-07:00",
    "end": "2026-10-08T18:30:00-07:00",
    "all_day": False,
    "location": "Community Park",
    "description": "Bring water",
    "confidence": 0.95,
}


def test_parse_ai_data():
    result = parser.parse_ai_data({"events": [VALID]})
    assert len(result.events) == 1
    assert result.events[0].title == "Soccer Practice"
    assert result.warnings == []


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "AI Task result must be an object"),
        ({}, "AI Task result must contain an events list"),
    ],
)
def test_parse_ai_data_rejects_bad_shapes(value, message):
    with pytest.raises(parser.ParseResultError) as error:
        parser.parse_ai_data(value)
    assert str(error.value) == message


def test_parse_ai_data_keeps_valid_events_and_reports_invalid_indices():
    result = parser.parse_ai_data({"events": [
        "bad", VALID, {**VALID, "title": ""}, {**VALID, "title": "Another event"},
    ]})
    assert [draft.title for draft in result.events] == ["Soccer Practice", "Another event"]
    assert result.warnings == [
        "event 0 must be an object",
        "event 2 is invalid: title must be a non-empty string",
    ]


def test_parse_ai_data_all_invalid_produces_no_drafts_with_warnings():
    result = parser.parse_ai_data({"events": [False]})
    assert result.events == []
    assert result.warnings == ["event 0 must be an object"]


async def test_async_parse_text(monkeypatch):
    generate = AsyncMock(return_value=SimpleNamespace(data={"events": [VALID], "unsupported_recurrence": False}))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    monkeypatch.setattr(
        parser.dt_util,
        "now",
        lambda *, time_zone: datetime(
            2026, 9, 25, 14, 30, tzinfo=ZoneInfo("America/Los_Angeles")
        ),
    )
    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="America/Los_Angeles")
    )
    outcome = await parser.async_parse_text(
        hass, text="  Soccer Thursday at 5:30  ", ai_task_entity="ai_task.test"
    )
    assert outcome.events[0].title == "Soccer Practice"
    assert outcome.warnings == []
    kwargs = generate.await_args.kwargs
    assert kwargs["entity_id"] == "ai_task.test"
    assert "Soccer Thursday at 5:30" in kwargs["instructions"]
    assert "Reference datetime: 2026-09-25T14:30:00-07:00" in kwargs["instructions"]
    assert "Home Assistant time zone: America/Los_Angeles" in kwargs["instructions"]
    assert kwargs["structure"] is parser.EVENTS_STRUCTURE
    assert "attached images or PDFs" in next(iter(kwargs["structure"].schema)).description
    assert "attachments" not in kwargs


async def test_async_parse_text_rejects_empty_input():
    with pytest.raises(ValueError, match=r"^text must not be empty$"):
        await parser.async_parse_text(object(), text="  ", ai_task_entity="ai_task.test")


async def test_provider_rejects_source_without_text():
    source = TextSourceAdapter().create("some text")
    empty = SourceDocument(id=source.id, kind=SourceKind.IMAGE, received_at=source.received_at)
    with pytest.raises(providers.SourceValidationError, match="Source must contain text or attachments") as caught:
        await providers.AITaskParserProvider(object(), "ai_task.test").async_parse(
            empty, reference_datetime="2026-09-25T14:30:00-07:00", time_zone="America/Los_Angeles"
        )
    assert str(caught.value) == "Source must contain text or attachments"
    assert caught.value.code == "empty_source"


def test_parser_capabilities_reject_unsupported_source_before_invocation():
    text_source = TextSourceAdapter().create("Some text")
    image = SourceAttachment("a", "image/png", 128, "media-source://media_source/local/a")
    pdf = SourceAttachment("b", "application/pdf", 128, "media-source://media_source/local/b")
    capabilities = providers.ParserCapabilities(True, False, False, 4, 1024)
    for attachment in (image, pdf):
        source = SourceDocument(text_source.id, SourceKind.IMAGE, text_source.received_at, attachments=(attachment,))
        with pytest.raises(providers.SourceValidationError) as caught:
            capabilities.validate(source)
        assert caught.value.code == "unsupported_capability"
        assert str(caught.value) == "Parser does not support this attachment"


@pytest.mark.parametrize(
    ("attachments", "capabilities", "code", "message"),
    [
        ((SourceAttachment("a", "text/plain", 1, "ref"),), (True, True, True, 4, 1024), "unsupported_media", "Unsupported attachment media type"),
        ((SourceAttachment("a", "image/png", 0, "ref"),), (True, True, True, 4, 1024), "empty_attachment", "Source attachment is empty"),
        ((SourceAttachment("a", "image/png", 1025, "ref"),), (True, True, True, 4, 1024), "source_too_large", "Source attachments exceed the size limit"),
        ((SourceAttachment("a", "image/png", 1, "ref"),) * 2, (True, True, True, 1, 1024), "too_many_attachments", "Too many source attachments"),
    ],
)
def test_parser_capability_validation_errors(attachments, capabilities, code, message):
    seed = TextSourceAdapter().create("source")
    source = SourceDocument(seed.id, SourceKind.IMAGE, seed.received_at, attachments=attachments)
    with pytest.raises(providers.SourceValidationError) as caught:
        providers.ParserCapabilities(*capabilities).validate(source)
    assert caught.value.code == code
    assert str(caught.value) == message


def test_parser_capabilities_accept_mixed_source_and_enforce_text_support():
    seed = TextSourceAdapter().create("source")
    image = SourceAttachment("a", "image/jpeg", 200, "ref")
    pdf = SourceAttachment("b", "application/pdf", 300, "ref")
    source = SourceDocument(seed.id, SourceKind.IMAGE, seed.received_at, text="See attached", attachments=(image, pdf))
    providers.ParserCapabilities(True, True, True, 2, 500).validate(source)
    attachment_only = SourceDocument(seed.id, SourceKind.IMAGE, seed.received_at, attachments=(image,))
    providers.ParserCapabilities(False, True, False, 2, 500).validate(attachment_only)
    with pytest.raises(providers.SourceValidationError) as caught:
        providers.ParserCapabilities(False, True, True, 2, 500).validate(source)
    assert caught.value.code == "unsupported_capability"
    assert str(caught.value) == "Parser does not support text"
    oversized = SourceDocument(seed.id, SourceKind.IMAGE, seed.received_at, attachments=(image, pdf))
    with pytest.raises(providers.SourceValidationError) as caught:
        providers.ParserCapabilities(False, True, True, 2, 499).validate(oversized)
    assert caught.value.code == "source_too_large"


async def test_source_boundary_passes_normalized_document_to_provider(monkeypatch):
    provider_parse = AsyncMock(return_value=parser.ParseOutcome([], []))
    monkeypatch.setattr(providers.AITaskParserProvider, "async_parse", provider_parse)
    source = TextSourceAdapter().create("source text", source_id="upstream-1")
    hass = SimpleNamespace(config=SimpleNamespace(time_zone="UTC"))

    outcome = await parser.async_parse_source(hass, source=source, ai_task_entity="ai_task.test")

    assert outcome == parser.ParseOutcome([], [])
    assert provider_parse.await_args.args == (source,)
    assert provider_parse.await_args.kwargs["time_zone"] == "UTC"
    assert provider_parse.await_args.kwargs["reference_datetime"]


async def test_image_capability_detected_before_ai_task_invocation(monkeypatch):
    image = SourceAttachment("a", "image/png", 25, "media-source://media_source/local/image.png")
    seed = TextSourceAdapter().create("seed")
    source = SourceDocument(seed.id, SourceKind.IMAGE, seed.received_at, attachments=(image,))
    generate = AsyncMock(return_value=SimpleNamespace(data={"events": [], "unsupported_recurrence": False}))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    component = SimpleNamespace(get_entity=lambda _id: SimpleNamespace(supported_features=0))
    hass = SimpleNamespace(data={providers.DATA_COMPONENT: component})
    provider = providers.AITaskParserProvider(hass, "ai_task.test")
    assert provider.capabilities.images is False
    with pytest.raises(providers.SourceValidationError) as caught:
        await provider.async_parse(source, reference_datetime="now", time_zone="UTC")
    assert caught.value.code == "unsupported_capability"
    generate.assert_not_awaited()

    component.get_entity = lambda _id: SimpleNamespace(supported_features=providers.AITaskEntityFeature.SUPPORT_ATTACHMENTS)
    assert provider.capabilities.images is True
    assert provider.capabilities.pdfs is True
    result = await provider.async_parse(source, reference_datetime="now", time_zone="UTC")
    assert result == parser.ParseOutcome([], [])
    assert generate.await_args.kwargs["attachments"] == [
        {"media_content_id": image.content_ref, "media_content_type": "image/png"}
    ]
    assert "attached images" in generate.await_args.kwargs["instructions"]
    assert "XXXX" not in generate.await_args.kwargs["instructions"]


async def test_mixed_pdf_keeps_text_and_attachment_in_one_ai_task_request(monkeypatch):
    seed = TextSourceAdapter().create("seed")
    attachment = SourceAttachment("pdf", "application/pdf", 512, "media-source://media_source/local/schedule.pdf", sha256="digest")
    source = SourceDocument(seed.id, SourceKind.PDF, seed.received_at,
                            text="Cover note\n\nExtracted dates", attachments=(attachment,))
    generate = AsyncMock(return_value=SimpleNamespace(data={"events": [VALID], "unsupported_recurrence": False}))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    component = SimpleNamespace(get_entity=lambda _id: SimpleNamespace(
        supported_features=providers.AITaskEntityFeature.SUPPORT_ATTACHMENTS))
    provider = providers.AITaskParserProvider(SimpleNamespace(data={providers.DATA_COMPONENT: component}), "ai_task.test")

    outcome = await provider.async_parse(source, reference_datetime="2026-10-01T12:00:00Z", time_zone="UTC")

    assert outcome.events[0].title == "Soccer Practice"
    assert outcome.warnings == []
    kwargs = generate.await_args.kwargs
    assert '"Cover note\\n\\nExtracted dates"' in kwargs["instructions"]
    assert kwargs["attachments"] == [{"media_content_id": attachment.content_ref,
                                       "media_content_type": "application/pdf"}]


async def test_image_capability_handles_unavailable_entity(monkeypatch):
    component = SimpleNamespace(get_entity=lambda _id: None)
    provider = providers.AITaskParserProvider(
        SimpleNamespace(data={providers.DATA_COMPONENT: component}),
        "ai_task.missing",
    )
    assert provider.capabilities.images is False

    generate = AsyncMock()
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    seed = TextSourceAdapter().create("seed")
    image = SourceAttachment(
        "a",
        "image/png",
        25,
        "media-source://media_source/local/image.png",
    )
    source = SourceDocument(
        seed.id,
        SourceKind.IMAGE,
        seed.received_at,
        attachments=(image,),
    )

    with pytest.raises(providers.ProviderError) as caught:
        await provider.async_parse(
            source,
            reference_datetime="now",
            time_zone="UTC",
        )

    assert caught.value.code == "provider_unavailable"
    assert str(caught.value) == "Configured AI Task is temporarily unavailable"
    generate.assert_not_awaited()


async def test_provider_maps_ai_task_failure_to_stable_category(monkeypatch):
    generate = AsyncMock(side_effect=HomeAssistantError("private upstream error"))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    source = TextSourceAdapter().create("Meeting on Friday")
    with pytest.raises(providers.ProviderError) as caught:
        await providers.AITaskParserProvider(object(), "ai_task.test").async_parse(
            source, reference_datetime="now", time_zone="UTC"
        )
    assert caught.value.code == "provider_error"
    assert isinstance(caught.value, HomeAssistantError)
    assert str(caught.value) == "AI Task could not parse the source"
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True


async def test_async_parse_text_preserves_ai_task_and_timezone_contract(monkeypatch):
    from unittest.mock import Mock

    generate = AsyncMock(return_value=SimpleNamespace(data={"events": [VALID], "unsupported_recurrence": False}))
    local_tz = ZoneInfo("America/Los_Angeles")
    get_time_zone = Mock(return_value=local_tz)
    now = Mock(
        return_value=datetime(
            2026, 9, 25, 14, 30, tzinfo=local_tz
        )
    )
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    monkeypatch.setattr(parser.dt_util, "get_time_zone", get_time_zone)
    monkeypatch.setattr(parser.dt_util, "now", now)

    hass = SimpleNamespace(
        config=SimpleNamespace(time_zone="America/Los_Angeles")
    )
    await parser.async_parse_text(
        hass, text="Soccer Thursday at 5:30", ai_task_entity="ai_task.test"
    )

    assert generate.await_args.args == (hass,)
    kwargs = generate.await_args.kwargs
    assert kwargs["task_name"] == parser.TASK_NAME
    assert kwargs["entity_id"] == "ai_task.test"
    get_time_zone.assert_called_once_with("America/Los_Angeles")
    now.assert_called_once_with(time_zone=local_tz)

def test_parser_surfaces_bounded_temporal_assumptions():
    from custom_components.daylight_calendar_import.parser import parse_ai_data
    item = {
        "title": "Practice", "start": "2026-10-09T17:00:00-07:00",
        "end": "2026-10-09T18:00:00-07:00", "all_day": False,
        "confidence": 0.8, "assumptions": ["Friday resolved from reference date"],
    }
    outcome = parse_ai_data({"events": [item]})
    assert len(outcome.events) == 1
    assert outcome.warnings == []
    assert outcome.event_assumptions == [("Friday resolved from reference date",)]
    assert parser.EVENTS_STRUCTURE.schema["events"].config["fields"]["assumptions"]["required"] is True


@pytest.mark.parametrize("assumptions", ["guess", [""], ["x" * 161], ["x"] * 9])
def test_parser_rejects_malformed_assumption_metadata(assumptions):
    from custom_components.daylight_calendar_import.parser import parse_ai_data
    item = {
        "title": "Practice", "start": "2026-10-09T17:00:00-07:00",
        "end": "2026-10-09T18:00:00-07:00", "all_day": False,
        "confidence": 0.8, "assumptions": assumptions,
    }
    outcome = parse_ai_data({"events": [item]})
    assert outcome.events == []
    assert "assumptions must be" in outcome.warnings[0]


def test_temporal_provenance_stays_aligned_after_invalid_candidate():
    result = parser.parse_ai_data({"events": [
        "invalid",
        {**VALID, "assumptions": ["  Thursday relative to source date  "]},
        {**VALID, "title": "Sibling", "assumptions": []},
    ]})
    assert len(result.events) == 2
    assert result.warnings == ["event 0 must be an object"]
    assert result.event_assumptions == [("Thursday relative to source date",), ()]


def test_temporal_assumption_limits_are_inclusive_and_errors_are_stable():
    from custom_components.daylight_calendar_import.models import (
        normalize_date_time_assumptions,
    )

    maximum_notes = ["x" * 160] * 8
    assert normalize_date_time_assumptions(maximum_notes) == tuple(maximum_notes)

    for invalid in (["x" * 160] * 9, ["x" * 161], ["  "], "not-a-list"):
        with pytest.raises(ValueError) as caught:
            normalize_date_time_assumptions(invalid)
        assert str(caught.value) == "assumptions must be up to eight short strings"


@pytest.mark.parametrize("text", [
    "Practice every Thursday until December", "Practice EVERY OTHER WEEK",
    "Soccer practice weekly through December", "Training daily", "Meeting monthly",
    "Classes weekly", "Weekly classes", "Appointments monthly", "Monthly appointments",
    "Weekly staff meetings", "Staff meetings weekly", "Standups daily", "Sessions yearly",
    "Practice occurring weekly", "Training running daily", "Class recurring monthly",
    "Not weekly and practices monthly",
    "Staff meeting weekly", "Appointment yearly", "Class annually",
    "This is no longer weekly, but practice monthly",
    "Recurring every day", "Repeating each morning", "Session every evening",
    "Practice every two weeks until December", "Class each three weeks",
    "Practice every twelve months", "Practice every one year", "Training every four days",
    "Practice every thirteen weeks", "Practice every twenty one weeks",
    "Training every thirty-six days", "Class every second Monday",
    "Practice occurs weekly through December", "Training runs daily",
    "The class occurs monthly", "Training runs yearly", "The class occurs annually",
    "No RRULE: Practice every Thursday until December",
    "This didn't repeat weekly before, but now occurs monthly",
    "Practice every 2 weeks", "Practice repeats daily", "Practice repeats monthly",
    "Practice repeats yearly", "Practice repeats annually", "RRULE:FREQ=WEEKLY",
    "Practice repeats weekly", "Practice recurring every week",
    "class meets weekly", "class each Thursday", "daily standup", "class recurs weekly",
    "Practice every day", "Class each day", "Every day at 7am", "Every day until December",
    "Practice each weekday at 8 until December", "Practice every weekday at 8",
    "Practice every other weekday", "Practice every 2 weekdays at 8",
    "Meet every 3 days at 8", "Meet every other day at 8",
    "Meet every Thursday at 8", "Meet every day at 8", "Meet monthly at 8",
    "Not a weekly class. Practice every Thursday at 8",
    "Candidates meet each other. Practice each Thursday at 8",
    "Practice each 2 weeks", "Practice each 3 days",
    "Weekly staff meeting", "Weekly team meeting",
    "Not weekly or monthly. Class each Thursday at 8",
    "Neither weekly nor monthly; practice every Thursday at 8",
    "Not a weekly meeting and practice every Thursday at 8",
    "The class is not weekly and meets monthly",
    "The class is not weekly, meets monthly",
    "The class is not weekly or recurs monthly",
    "Not weekly and not monthly, but meets daily",
    "Practice every morning at 8 through December",
    "Each afternoon from 3pm until December", "Every evening at 7pm",
    "Practice each night", "Every night until December",
])
def test_source_recurrence_cannot_be_collapsed_into_ordinary_drafts(text):
    outcome = parser.parse_ai_data(
        {"events": [VALID], "unsupported_recurrence": False}, source_text=text
    )
    assert outcome.events == []
    assert outcome.event_assumptions == []
    assert outcome.warnings == [parser.UNSUPPORTED_RECURRENCE_WARNING]
    assert outcome.warnings[0].startswith("unsupported_recurrence:")


@pytest.mark.parametrize("events", [[], [VALID], [VALID, {**VALID, "title": "Sibling"}]])
def test_provider_reported_recurrence_has_one_bounded_warning(events):
    outcome = parser.parse_ai_data({"events": events, "unsupported_recurrence": True})
    assert outcome == parser.ParseOutcome([], [parser.UNSUPPORTED_RECURRENCE_WARNING])


@pytest.mark.parametrize("flag", [None, 0, 1, "false", "true", [], {}])
def test_recurrence_contract_rejects_non_boolean_flags(flag):
    with pytest.raises(parser.ParseResultError, match="^unsupported_recurrence must be a boolean$"):
        parser.parse_ai_data({"events": [VALID], "unsupported_recurrence": flag})


@pytest.mark.parametrize("text", [
    "One meeting on Thursday", "Meet October 8 and October 15 at 5pm",
    "At the October 20 workshop, repeat every exercise twice",
    "At the October 20 workshop, repeats every instruction twice",
    "This is no longer a weekly practice; final practice October 20",
    "No longer repeats weekly; final practice October 20",
    "No longer practice weekly; final practice October 20",
    "Classes no longer occur weekly; final class October 20",
    "No longer weekly classes or monthly appointments; meet October 20",
    "Not occurring weekly and not running daily; meet October 20",
    "No more weekly classes; the final class is October 20",
    "Not a practice weekly or a meeting monthly; meet October 20",
    "This meeting didn’t repeat weekly; it occurred only once on October 20",
    "This meeting didn't recur monthly; meet October 20",
    "This meeting did not repeat weekly; meet October 20",
    "No RRULE: this meeting occurs only on October 20",
    "Not an RRULE: this meeting occurs only on October 20",
    "This isn't an RRULE: meet October 20",
    "Not every two weeks or monthly; meet October 20",
    "Not every twenty one weeks or monthly; meet October 20",
    "Not every second Monday; meet October 20",
    "Training doesn't run daily; meet October 20",
    "This is not a weekly class; the one-off class is October 20",
    "The class is not recurring weekly; meet October 20 at 8am",
    "Not a daily standup; a single standup on October 20",
    "No monthly meeting; instead meet October 20",
    "This is not an annual meeting; only meet October 20",
    "This is not a weekly or monthly meeting; the one-off meeting is October 20",
    "This is neither a weekly nor a monthly meeting; meet October 20",
    "Neither weekly nor monthly; the meeting is October 20",
    "Not a weekly class or a monthly session; meet October 20",
    "Not weekly, monthly or annual; meet October 20",
    "Not a weekly class, a monthly session, or an annual meeting; meet October 20",
    "No weekly or monthly staff meeting; meet October 20",
    "Does not repeat weekly or monthly; meet October 20",
    "Not weekly and not monthly; meet October 20",
    "This isn't a weekly meeting; the meeting is October 20",
    "These aren't monthly classes; the class is October 20",
    "This wasn’t a weekly meeting; the meeting is October 20",
    "They weren’t weekly meetings; the meeting is October 20",
    "The class doesn’t recur weekly; meet October 20",
    "Does not repeat weekly and does not meet monthly; meet October 20",
    "Neither repeats weekly nor meets monthly; meet October 20",
    "Every morning is a gift. Party on October 20",
    "The Every Evening Gala is on October 20",
    "Not every Thursday; the class only meets October 20",
    "Candidates meet each other Thursday, October 20 from 10am to 11am",
    "Candidates meet each other day on October 20",
    "Candidates meet each other Monday, October 20 at 10am",
    "Meet every student on October 20 from 10am to 11am",
    "Meets every participant on October 20 at 10am",
    "Meet every applicant on October 20 at 10am",
    "At the Weekley Center", "Bring every student's form",
    "Every day is a gift. Birthday party October 20 at 10am",
    "The Every Day 5K is October 20", "Class does not repeat weekly",
    "Class doesn't recur weekly", "Class never repeats every Thursday",
    "Weekly school newsletter: Field trip on October 20 from 10am to 11am",
    "Daily bulletin: Conference October 20 from 10am to 11am",
    "Monthly report: Meeting October 20 from 10am to 11am",
])
def test_explicit_independent_dates_remain_supported(text):
    outcome = parser.parse_ai_data(
        {"events": [VALID], "unsupported_recurrence": False}, source_text=text
    )
    assert len(outcome.events) == 1
    assert outcome.warnings == []


async def test_provider_independently_guards_recurring_source(monkeypatch):
    generate = AsyncMock(return_value=SimpleNamespace(data={
        "events": [VALID], "unsupported_recurrence": False,
    }))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    outcome = await providers.AITaskParserProvider(object(), "ai_task.test").async_parse(
        TextSourceAdapter().create("Soccer every Thursday until December"),
        reference_datetime="2026-10-01T12:00:00Z", time_zone="UTC",
    )
    assert outcome == parser.ParseOutcome([], [parser.UNSUPPORTED_RECURRENCE_WARNING])
    instructions = generate.await_args.kwargs["instructions"]
    assert "never expand recurrence or silently reduce it to a single occurrence" in instructions
    assert "source text or attachments" in instructions
    assert "unsupported_recurrence" in parser.EVENTS_STRUCTURE.schema


@pytest.mark.parametrize(
    ("start", "end", "expected_start", "expected_end"),
    [
        ("2026-11-03T18:00:00-07:00", "2026-11-03T19:30:00-07:00",
         "2026-11-03T18:00:00-08:00", "2026-11-03T19:30:00-08:00"),
        ("2026-03-10T18:00:00-08:00", "2026-03-10T19:30:00-08:00",
         "2026-03-10T18:00:00-07:00", "2026-03-10T19:30:00-07:00"),
        ("2026-10-25T15:00:00-07:00", "2026-10-25T17:00:00-07:00",
         "2026-10-25T15:00:00-07:00", "2026-10-25T17:00:00-07:00"),
        ("2026-11-01T00:30:00-07:00", "2026-11-01T02:30:00-07:00",
         "2026-11-01T00:30:00-07:00", "2026-11-01T02:30:00-08:00"),
    ],
)
def test_inferred_offsets_follow_event_date_dst(start, end, expected_start, expected_end):
    outcome = parser.parse_ai_data(
        {"events": [{**VALID, "start": start, "end": end,
                     "uses_reference_time_zone": True,
                     "assumptions": ["The event uses the Home Assistant time zone"]}]},
        time_zone="America/Los_Angeles",
    )
    assert len(outcome.events) == 1
    assert (outcome.events[0].start, outcome.events[0].end) == (expected_start, expected_end)
    assert outcome.event_assumptions == [("The event uses the Home Assistant time zone",)]
    assert len(outcome.warnings) == int((start, end) != (expected_start, expected_end))
    if outcome.warnings:
        assert "corrected inferred UTC offset" in outcome.warnings[0]


@pytest.mark.parametrize("original_offset", ["-07:00", "-08:00"])
def test_inferred_ambiguous_fall_back_keeps_valid_original_offset(original_offset):
    event = {**VALID, "start": f"2026-11-01T01:15:00{original_offset}",
             "end": f"2026-11-01T01:45:00{original_offset}",
             "uses_reference_time_zone": True}
    result = parser.parse_ai_data({"events": [event]}, time_zone="America/Los_Angeles")
    assert len(result.events) == 1
    assert result.events[0].start == event["start"]
    assert result.events[0].end == event["end"]
    assert "DST fall-back repeats this local hour" in result.warnings[0]
    assert any("DST fall-back repeats this local hour" in note
               for note in result.event_assumptions[0])


@pytest.mark.parametrize(
    ("start", "end", "expected_message"),
    [
        ("2026-03-08T02:30:00-08:00", "2026-03-08T04:30:00-07:00", "does not exist"),
        ("2026-03-08T01:30:00-08:00", "2026-03-08T02:30:00-08:00", "does not exist"),
        ("9999-12-31T23:00:00-08:00", "9999-12-31T23:30:00-08:00", "does not exist"),
        ("2026-11-03T19:00:00-07:00", "2026-11-03T18:00:00-07:00", "end must be after start"),
    ],
)
def test_inferred_invalid_local_times_do_not_create_pending_drafts(start, end, expected_message):
    result = parser.parse_ai_data(
        {"events": [{**VALID, "start": start, "end": end,
                     "uses_reference_time_zone": True}]},
        time_zone="America/Los_Angeles",
    )
    assert result.events == []
    assert expected_message in result.warnings[0]


def test_ambiguous_fall_back_with_wrong_offset_uses_first_fold_with_review_warning():
    result = parser.parse_ai_data(
        {"events": [{**VALID, "start": "2026-11-01T01:30:00-06:00",
                     "end": "2026-11-01T02:30:00-08:00",
                     "uses_reference_time_zone": True}]},
        time_zone="America/Los_Angeles",
    )
    assert len(result.events) == 1
    assert result.events[0].start == "2026-11-01T01:30:00-07:00"
    assert any("DST fall-back" in note for note in result.event_assumptions[0])
    assert any("corrected inferred UTC offset" in warning for warning in result.warnings)


def test_final_range_validation_follows_local_offset_repair():
    # Before correction, the end appears 30 minutes before the start in UTC.
    result = parser.parse_ai_data(
        {"events": [{**VALID, "start": "2026-11-01T01:30:00-08:00",
                     "end": "2026-11-01T02:00:00-07:00",
                     "uses_reference_time_zone": True}]},
        time_zone="America/Los_Angeles",
    )
    assert len(result.events) == 1
    assert result.events[0].end == "2026-11-01T02:00:00-08:00"
    assert any("DST fall-back" in note for note in result.event_assumptions[0])


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ("2026-11-03T18:00:00Z", "UTC-formatted inferred local time"),
        ("2026-11-03T18:00:00+09:00", "unrelated UTC offset"),
        ("2026-11-03T18:00:00", "timezone offsets"),
        ("bad-time", "valid ISO values"),
        (None, "valid ISO values"),
    ],
)
def test_inferred_local_clock_rejects_untrusted_timestamp_representation(value, message):
    result = parser.parse_ai_data(
        {"events": [{**VALID, "start": value, "uses_reference_time_zone": True}]},
        time_zone="America/Los_Angeles",
    )
    assert result.events == []
    assert message in result.warnings[0]


@pytest.mark.parametrize("offset", ["+02:00", "+01:00"])
def test_european_fall_back_hour_is_flagged_for_human_review(offset):
    result = parser.parse_ai_data(
        {"events": [{**VALID, "start": f"2026-10-25T02:15:00{offset}",
                     "end": f"2026-10-25T02:45:00{offset}",
                     "uses_reference_time_zone": True}]},
        time_zone="Europe/Berlin",
    )
    assert len(result.events) == 1
    assert result.events[0].start == f"2026-10-25T02:15:00{offset}"
    assert any("DST fall-back repeats this local hour" in note
               for note in result.event_assumptions[0])


def test_repeated_hour_disclosure_does_not_exceed_assumptions_limit():
    repeated = {**VALID, "start": "2026-11-01T01:15:00-07:00",
                "end": "2026-11-01T01:45:00-07:00",
                "uses_reference_time_zone": True,
                "assumptions": ["Existing assumption"] * 8}
    result = parser.parse_ai_data({"events": [repeated]}, time_zone="America/Los_Angeles")
    assert len(result.events) == 1
    assert result.event_assumptions[0] == ("Existing assumption",) * 8
    assert "DST fall-back" in result.warnings[0]


def test_repeated_hour_disclosure_is_not_duplicated():
    note = parser._FALL_BACK_REVIEW_NOTE
    repeated = {**VALID, "start": "2026-11-01T01:15:00-07:00",
                "end": "2026-11-01T01:45:00-07:00",
                "uses_reference_time_zone": True,
                "assumptions": [note]}
    result = parser.parse_ai_data({"events": [repeated]}, time_zone="America/Los_Angeles")
    assert len(result.events) == 1
    assert result.event_assumptions == [(note,)]


def test_explicit_source_timezone_is_not_rewritten_and_all_day_is_unchanged():
    winter = {**VALID, "start": "2026-11-03T18:00:00-07:00",
              "end": "2026-11-03T19:00:00-07:00"}
    all_day = {**VALID, "start": "2026-11-03", "end": "2026-11-04", "all_day": True,
               "uses_reference_time_zone": True}
    result = parser.parse_ai_data(
        {"events": [{**winter, "uses_reference_time_zone": False},
                    winter, all_day]},
        time_zone="America/Los_Angeles",
    )
    assert [event.start for event in result.events] == [
        "2026-11-03T18:00:00-07:00", "2026-11-03T18:00:00-07:00", "2026-11-03",
    ]
    assert result.warnings == []


@pytest.mark.parametrize("bad_marker", [None, 0, 1, "true", [], {}])
def test_inferred_zone_marker_must_be_boolean(bad_marker):
    result = parser.parse_ai_data(
        {"events": [{**VALID, "uses_reference_time_zone": bad_marker}]},
        time_zone="America/Los_Angeles",
    )
    assert result.events == []
    assert result.warnings == ["event 0 is invalid: uses_reference_time_zone must be a boolean"]


@pytest.mark.parametrize("zone", [None, "Unknown/Zone"])
def test_inferred_zone_cannot_be_validated_without_configured_zone(zone):
    result = parser.parse_ai_data(
        {"events": [{**VALID, "uses_reference_time_zone": True}]},
        time_zone=zone,
    )
    assert result.events == []
    assert result.warnings == ["event 0 is invalid: Home Assistant time zone is unavailable"]


async def test_provider_normalizes_inferred_offset_before_returning_draft(monkeypatch):
    source = TextSourceAdapter().create("School event November 3, 2026, 6-7:30 PM")
    image_event = {**VALID, "start": "2026-11-03T18:00:00-07:00",
                   "end": "2026-11-03T19:30:00-07:00",
                   "uses_reference_time_zone": True}
    generate = AsyncMock(return_value=SimpleNamespace(
        data={"events": [image_event], "unsupported_recurrence": False},
    ))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    provider = providers.AITaskParserProvider(object(), "ai_task.test")
    outcome = await provider.async_parse(
        source, reference_datetime="2026-10-19T14:00:00-07:00",
        time_zone="America/Los_Angeles",
    )
    assert outcome.events[0].start == "2026-11-03T18:00:00-08:00"
    assert outcome.events[0].end == "2026-11-03T19:30:00-08:00"
    assert "corrected inferred UTC offset" in outcome.warnings[0]
    fields = generate.await_args.kwargs["structure"].schema["events"].config["fields"]
    assert fields["uses_reference_time_zone"]["required"] is True
    instructions = generate.await_args.kwargs["instructions"]
    assert "Set uses_reference_time_zone to true" in instructions
    assert "Preserve explicit source zones" in instructions


@pytest.mark.parametrize(
    ("start", "end", "expected_start", "expected_end"),
    [
        (
            "2026-11-01T01:30:00-08:00", "2026-11-01T01:45:00-07:00",
            "2026-11-01T01:30:00-07:00", "2026-11-01T01:45:00-07:00",
        ),
        (
            "2026-11-01T01:50:00-08:00", "2026-11-01T01:10:00-07:00",
            "2026-11-01T01:50:00-07:00", "2026-11-01T01:10:00-08:00",
        ),
        (
            "2026-10-25T02:30:00+01:00", "2026-10-25T02:45:00+02:00",
            "2026-10-25T02:30:00+02:00", "2026-10-25T02:45:00+02:00",
        ),
    ],
)
def test_reversed_fall_back_offsets_retain_provisional_review_draft(
    start, end, expected_start, expected_end,
):
    zone = "Europe/Berlin" if start.startswith("2026-10") else "America/Los_Angeles"
    result = parser.parse_ai_data(
        {"events": [{**VALID, "start": start, "end": end,
                     "uses_reference_time_zone": True}]},
        time_zone=zone,
    )
    assert len(result.events) == 1
    assert (result.events[0].start, result.events[0].end) == (expected_start, expected_end)
    assert "DST fall-back repeats this local hour" in result.warnings[-1]
    assert parser._FALL_BACK_REVIEW_NOTE in result.event_assumptions[0]
    assert "corrected inferred UTC offset" in result.warnings[0]


def test_fall_back_does_not_rescue_truly_reversed_or_explicitly_zoned_range():
    for inferred in (True, False):
        result = parser.parse_ai_data(
            {"events": [{**VALID, "start": "2026-11-01T02:10:00-08:00",
                         "end": "2026-11-01T01:30:00-07:00",
                         "uses_reference_time_zone": inferred}]},
            time_zone="America/Los_Angeles",
        )
        assert result.events == []
        assert "end must be after start" in result.warnings[0]

    result = parser.parse_ai_data(
        {"events": [{**VALID, "start": "2026-11-01T01:30:00-08:00",
                     "end": "2026-11-01T01:45:00-07:00",
                     "uses_reference_time_zone": False}]},
        time_zone="America/Los_Angeles",
    )
    assert result.events == []
    assert "end must be after start" in result.warnings[0]
