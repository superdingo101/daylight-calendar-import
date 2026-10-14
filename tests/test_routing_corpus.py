"""Recorded extraction corpus tests for deterministic routing authority.

AI responses are recordings: these tests prove control-plane and validation
invariants, not that a live model always rejects hostile source instructions.
"""

from datetime import datetime
import json
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image
from pypdf import PdfReader
import pytest

from custom_components.daylight_calendar_import.calendar_match import (
    CalendarCandidate, classify_calendar_event,
)
from custom_components.daylight_calendar_import.calendar_routing import extract_calendar_route
from custom_components.daylight_calendar_import.parser import parse_ai_data
from custom_components.daylight_calendar_import.source_routing import plan_source_routing
from custom_components.daylight_calendar_import.sources import SourceDocument, SourceKind

ROOT = Path(__file__).parent / "fixtures" / "extraction"
CASES = [json.loads(path.read_text()) for path in sorted(ROOT.glob("routing-*.json"))]
ALIASES = {"kids": "calendar.kids", "work": "calendar.work"}
ALLOWED = {"calendar.family", "calendar.kids", "calendar.work"}


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_corpus_routes_only_explicit_unquoted_allowed_control(case):
    source = SourceDocument(
        id=case["id"], kind=SourceKind(case["source_kind"]),
        received_at=datetime.fromisoformat(case["reference_datetime"]),
        text=case["source_text"], title=case.get("source_title"),
    )
    expected = case["routing_expectation"]
    result = plan_source_routing(
        source, default_calendar="calendar.family", allowed_calendars=ALLOWED,
        aliases=ALIASES,
    )
    assert result.calendar_entity == expected["calendar_entity"]
    assert result.requires_confirmation is expected["requires_confirmation"]
    assert bool(result.warnings) is expected["requires_confirmation"]
    assert result.parser_source.text == expected["parser_text"]
    assert case["parser_source_text"] == result.parser_source.text
    assert source.text == case["source_text"]
    extracted = extract_calendar_route(
        subject=source.title if source.kind is SourceKind.EMAIL else None,
        body=source.text, allowed_calendars=ALLOWED, aliases=ALIASES,
    )
    assert extracted.result.status == expected["status"]
    outcome = parse_ai_data(case["provider_response"])
    assert [event.as_dict() for event in outcome.events] == case["expected"]["events"]
    assert outcome.warnings == case["expected"]["warnings"]
    assert [list(value) for value in outcome.event_assumptions] == case["expected"]["event_assumptions"]


def test_newsletter_duplicate_and_conflict_expectations():
    case = next(case for case in CASES if case["id"] == "routing-newsletter")
    draft = parse_ai_data(case["provider_response"]).events[0]
    for raw in case["calendar_candidates"]:
        fields = {key: value for key, value in raw.items() if key != "expected_kind"}
        result = classify_calendar_event(
            draft, CalendarCandidate(**fields), local_zone=ZoneInfo(case["time_zone"]),
        )
        assert (result.kind if result else None) == raw["expected_kind"]
        if result:
            assert result.calendar_entity == raw["calendar_entity"]


def test_visual_corpus_has_real_image_scan_and_extractable_pdf():
    with Image.open(ROOT / "assets" / "routing-poster.png") as image:
        assert image.format == "PNG"
        assert image.size == (900, 350)
    scanned = PdfReader(ROOT / "assets" / "routing-scan.pdf")
    assert len(scanned.pages) == 1
    assert not scanned.pages[0].extract_text().strip()
    assert len(scanned.pages[0].images) == 1
    text_pdf = PdfReader(ROOT / "assets" / "routing-text.pdf")
    assert len(text_pdf.pages) == 1
    text = text_pdf.pages[0].extract_text()
    assert "Robotics showcase" in text
    assert "October 20, 2026, 5-6pm PDT" in text
    assert "Bring the permission slip." in text


@pytest.mark.parametrize("case_id", [
    "routing-forwarded", "routing-hostile", "routing-unknown", "routing-conflicting",
])
async def test_corpus_route_and_hostile_fields_survive_review_boundary(
    hass, monkeypatch, case_id,
):
    """Source and provider instructions cannot authorize automatic calendar writes."""
    from unittest.mock import AsyncMock
    from pytest_homeassistant_custom_component.common import MockConfigEntry
    from custom_components.daylight_calendar_import.const import (
        ATTR_SOURCE_ID, ATTR_TEXT, CONF_AI_TASK_ENTITY, CONF_CALENDAR_ALIASES,
        CONF_CALENDAR_ENTITIES, CONF_CALENDAR_ENTITY, DOMAIN, SERVICE_SUBMIT_TEXT,
    )
    from custom_components.daylight_calendar_import.storage import PendingEventEditError

    case = next(case for case in CASES if case["id"] == case_id)
    parsed = AsyncMock(return_value=parse_ai_data(case["provider_response"]))
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.parse_source_with_provider", parsed,
    )
    entry = MockConfigEntry(
        domain=DOMAIN, title="Corpus import",
        data={CONF_AI_TASK_ENTITY: "ai_task.test", CONF_CALENDAR_ENTITY: "calendar.family",
              CONF_CALENDAR_ENTITIES: sorted(ALLOWED)},
        options={CONF_CALENDAR_ENTITIES: sorted(ALLOWED), CONF_CALENDAR_ALIASES: ALIASES},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # The service boundary intentionally submits the forwarded body as pasted
    # text, using exactly the same source directive resolver as email ingestion.
    request = {ATTR_TEXT: case["source_text"], ATTR_SOURCE_ID: case_id}
    result = await hass.services.async_call(
        DOMAIN, SERVICE_SUBMIT_TEXT, request, blocking=True, return_response=True,
    )
    pending = result["pending"]
    assert pending is not None
    assert result["duplicate_source"] is False
    assert parsed.await_args.kwargs["source"].text == case["routing_expectation"]["parser_text"]
    event = pending["events"][0]
    assert event["status"] == "pending"
    assert event["calendar_entity"] == case["routing_expectation"]["calendar_entity"]
    assert event.get("routing_unresolved", False) is case["routing_expectation"]["requires_confirmation"]
    store = hass.data[DOMAIN][entry.entry_id]
    repeated = await hass.services.async_call(
        DOMAIN, SERVICE_SUBMIT_TEXT, request, blocking=True, return_response=True,
    )
    assert repeated["duplicate_source"] is True
    assert repeated["pending"] is None
    parsed.assert_awaited_once()
    if case["routing_expectation"]["requires_confirmation"]:
        writer = AsyncMock()
        with pytest.raises(PendingEventEditError, match="Confirm all event destinations"):
            await store.async_process_events(pending["id"], writer)
        writer.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    stored = hass.data[DOMAIN][entry.entry_id].get(pending["id"])
    assert stored.events[0].status == "pending"
    assert stored.events[0].calendar_entity == event["calendar_entity"]
    assert stored.events[0].routing_unresolved is event.get("routing_unresolved", False)


def test_optional_evaluator_sends_actual_routed_evidence_without_control_or_gold(
    monkeypatch, tmp_path,
):
    """The optional adapter sees the same source evidence as production parsing."""
    from types import SimpleNamespace
    from scripts import evaluate_extraction_corpus as evaluator

    fixtures = {case["id"]: case for case in CASES}
    # Also protect the unrouted corpus contract: existing cases use source_text.
    plain = json.loads((ROOT / "explicit-practice.json").read_text())
    fixtures[plain["id"]] = plain
    for case in fixtures.values():
        (tmp_path / f"{case['id']}.json").write_text(json.dumps(case))
    seen = []

    def capture(runner, *, input, text, capture_output, check, timeout):
        request = json.loads(input)
        case = fixtures[request["id"]]
        source = SourceDocument(
            id=case["id"], kind=SourceKind(case.get("source_kind", "manual_text")),
            received_at=datetime.fromisoformat(case["reference_datetime"]),
            text=case["source_text"], title=case.get("source_title"),
        )
        plan = plan_source_routing(
            source, default_calendar="calendar.family", allowed_calendars=ALLOWED,
            aliases=ALIASES,
        )
        assert request["source_text"] == plan.parser_source.text
        assert set(request) <= {
            "id", "source_text", "reference_datetime", "time_zone",
            "source_kind", "attachments", "fixture_directory",
        }
        assert request.get("attachments", []) == case.get("attachments", [])
        assert request["fixture_directory"] == str(tmp_path.resolve())
        assert runner == ["reference-adapter"]
        assert text and capture_output and check and timeout == 120
        seen.append(request["id"])
        return SimpleNamespace(stdout=json.dumps(case["provider_response"]))

    monkeypatch.setattr(evaluator.subprocess, "run", capture)
    report = evaluator.evaluate(tmp_path, ["reference-adapter"])
    assert sorted(seen) == sorted(fixtures)
    assert report["event_count_accuracy"] == 1
