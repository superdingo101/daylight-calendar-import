"""Recorded response replay validates integration boundaries, not AI accuracy."""
from datetime import datetime
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from zoneinfo import ZoneInfo

import pytest

from custom_components.daylight_calendar_import import parser, providers
from custom_components.daylight_calendar_import.sources import SourceAttachment, SourceDocument, SourceKind
from scripts.evaluate_extraction_corpus import score_case, evaluate

CORPUS = Path(__file__).parent / "fixtures" / "extraction"
CASES = sorted(CORPUS.glob("*.json"))


@pytest.mark.parametrize("path", CASES, ids=lambda path: path.stem)
async def test_recorded_extraction_boundary(path, monkeypatch):
    case = json.loads(path.read_text())
    assert path.stem == case["id"]
    reference = datetime.fromisoformat(case["reference_datetime"])
    parser_text = case.get("parser_source_text", case["source_text"])
    attachments = tuple(SourceAttachment(
        id=str(index), media_type=item["media_type"],
        size_bytes=(CORPUS / item["path"]).stat().st_size,
        content_ref=f"media-source://media_source/local/corpus/{item['path']}",
        filename=item["name"],
    ) for index, item in enumerate(case.get("attachments", [])))
    source = SourceDocument(case["id"], SourceKind(case.get("source_kind", "manual_text")),
                            reference, text=parser_text, attachments=attachments)
    generate = AsyncMock(return_value=SimpleNamespace(data=case["provider_response"]))
    monkeypatch.setattr(providers.ai_task, "async_generate_data", generate)
    component = SimpleNamespace(get_entity=lambda _id: SimpleNamespace(
        available=True, supported_features=providers.AITaskEntityFeature.SUPPORT_ATTACHMENTS))
    hass = SimpleNamespace(config=SimpleNamespace(time_zone=case["time_zone"]),
                           data={providers.DATA_COMPONENT: component})
    outcome = await parser.async_parse_source(hass, source=source,
                                              ai_task_entity="ai_task.corpus", reference_datetime=reference)
    assert [event.as_dict() for event in outcome.events] == case["expected"]["events"]
    assert outcome.warnings == case["expected"]["warnings"]
    assert [list(notes) for notes in outcome.event_assumptions] == case["expected"]["event_assumptions"]
    generate.assert_awaited_once()
    request = generate.await_args.kwargs
    assert parser_text.strip() in request["instructions"]
    assert f"Home Assistant time zone: {case['time_zone']}" in request["instructions"]
    assert f"Reference datetime: {reference.astimezone(ZoneInfo(case['time_zone'])).isoformat()}" in request["instructions"]
    if attachments:
        assert request["attachments"] == [{"media_content_id": item.content_ref,
                                            "media_content_type": item.media_type} for item in attachments]
    else:
        assert "attachments" not in request


def test_evaluator_detects_missing_extra_and_wrong_fields():
    case = json.loads((CORPUS / "explicit-practice.json").read_text())
    response = case["provider_response"]
    score = score_case(case, response)
    assert all(value == {"correct": 1, "total": 1} for value in score["fields"].values())
    missing = score_case(case, {"events": []})
    assert not missing["event_count_correct"]
    assert missing["fields"]["start"] == {"correct": 0, "total": 1}
    extra = score_case(case, {"events": response["events"] * 2})
    assert extra["fields"]["start"] == {"correct": 1, "total": 2}
    wrong = score_case(case, {"events": [{**response["events"][0], "start": "wrong"}]})
    assert wrong["fields"]["start"] == {"correct": 0, "total": 1}
    with pytest.raises(ValueError, match="list of event objects"):
        score_case(case, {"events": [None]})


def test_optional_runner_protocol(tmp_path):
    import sys
    adapter = tmp_path / "adapter.py"
    adapter.write_text("import json,sys,pathlib\nc=json.load(sys.stdin)\nassert 'expected' not in c and 'provider_response' not in c\np=pathlib.Path(c['fixture_directory']) / (c['id'] + '.json')\njson.dump(json.loads(p.read_text())['provider_response'],sys.stdout)\n")
    isolated = tmp_path / "corpus"
    isolated.mkdir()
    for name in ("explicit-practice", "no-events-newsletter", "meeting-join-details"):
        (isolated / f"{name}.json").write_bytes((CORPUS / f"{name}.json").read_bytes())
    report = evaluate(isolated, [sys.executable, str(adapter)])
    assert report["event_count_accuracy"] == 1
    assert report["event_totals"] == {"expected_events": 2, "actual_events": 2,
                                      "matched_events": 2, "missed_events": 0, "spurious_events": 0}
    assert report["date_totals"] == {"correct": 2, "total": 2}
    assert report["meeting_detail_totals"] == {"correct": 4, "total": 4}
    assert "004201" not in json.dumps(report)
    assert "example.invalid" not in json.dumps(report)
    assert all(total["correct"] == total["total"] for total in report["field_totals"].values())
    with pytest.raises(ValueError, match="no JSON cases"):
        evaluate(tmp_path, [sys.executable, str(adapter)])


@pytest.mark.parametrize(("field", "value"), [
    ("all_day", 0), ("all_day", 0.0), ("all_day", "false"),
    ("all_day", None), ("assumptions", None), ("assumptions", {}),
    ("assumptions", ""), ("assumptions", [False]),
    ("title", 1), ("start", False), ("end", []),
    ("location", 0), ("description", False),
])
def test_evaluator_malformed_field_types_never_receive_credit(field, value):
    fixture = json.loads((CORPUS / "explicit-practice.json").read_text())
    candidate = {**fixture["provider_response"]["events"][0], field: value}
    result = score_case(fixture, {"events": [candidate]})
    assert result["fields"][field] == {"correct": 0, "total": 1}


@pytest.mark.parametrize("field", ["title", "start", "end", "all_day", "assumptions"])
def test_evaluator_missing_required_fields_never_receive_credit(field):
    fixture = json.loads((CORPUS / "explicit-practice.json").read_text())
    candidate = dict(fixture["provider_response"]["events"][0])
    del candidate[field]
    result = score_case(fixture, {"events": [candidate]})
    assert result["fields"][field] == {"correct": 0, "total": 1}


@pytest.mark.parametrize("value", [None, "", "  "])
def test_evaluator_optional_fields_normalize_absent_null_and_empty_text(value):
    fixture = json.loads((CORPUS / "explicit-practice.json").read_text())
    fixture["expected"]["events"][0].update(location=None, description=None)
    candidate = {**fixture["provider_response"]["events"][0],
                 "location": value, "description": value}
    result = score_case(fixture, {"events": [candidate]})
    assert result["fields"]["location"]["correct"] == 1
    assert result["fields"]["description"]["correct"] == 1
    del candidate["location"], candidate["description"]
    result = score_case(fixture, {"events": [candidate]})
    assert result["fields"]["location"]["correct"] == 1
    assert result["fields"]["description"]["correct"] == 1


def test_evaluator_does_not_credit_numeric_one_as_all_day_true():
    fixture = json.loads((CORPUS / "explicit-practice.json").read_text())
    fixture["expected"]["events"][0]["all_day"] = True
    candidate = {**fixture["provider_response"]["events"][0], "all_day": 1}
    result = score_case(fixture, {"events": [candidate]})
    assert result["fields"]["all_day"] == {"correct": 0, "total": 1}


def test_evaluator_normalizes_assumptions_like_production():
    fixture = json.loads((CORPUS / "explicit-practice.json").read_text())
    notes = ["Friday resolved from reference date", "Local time zone applied"]
    fixture["expected"]["event_assumptions"] = [notes]
    candidate = {**fixture["provider_response"]["events"][0],
                 "assumptions": [f"  {note}\n" for note in notes]}
    result = score_case(fixture, {"events": [candidate]})
    assert result["fields"]["assumptions"] == {"correct": 1, "total": 1}
    assert candidate["assumptions"] == [f"  {note}\n" for note in notes]


@pytest.mark.parametrize("padding", ["", "  ", "\n"])
def test_evaluator_matches_reordered_events_and_reserves_repeated_title_intervals(padding):
    from copy import deepcopy
    case = json.loads((CORPUS / "explicit-practice.json").read_text())
    first = case["provider_response"]["events"][0]
    second = {**first, "start": "2026-10-16T17:00:00-07:00",
              "end": "2026-10-16T18:00:00-07:00"}
    case["expected"]["events"].append({key: value for key, value in second.items()
                                         if key != "assumptions"})
    case["expected"]["event_assumptions"].append([])
    padded_second = {**second, **{key: f"{padding}{second[key]}{padding}"
                                   for key in ("start", "end")}}
    reordered = score_case(case, {"events": [padded_second, deepcopy(first)]})
    assert reordered["matched_events"] == 2
    assert reordered["dates"] == {"correct": 2, "total": 2}
    for field in ("start", "end"):
        assert reordered["fields"][field] == {"correct": 2, "total": 2}
    missing = score_case(case, {"events": [padded_second]})
    assert missing["missed_events"] == 1
    assert missing["spurious_events"] == 0
    assert missing["dates"] == {"correct": 1, "total": 2}


def test_evaluator_equal_counts_can_include_missed_and_spurious_events():
    case = json.loads((CORPUS / "explicit-practice.json").read_text())
    other = {**case["provider_response"]["events"][0], "title": "Unrelated event"}
    score = score_case(case, {"events": [other]})
    assert score["event_count_correct"]
    assert score["matched_events"] == 0
    assert score["missed_events"] == score["spurious_events"] == 1
    assert score["dates"] == {"correct": 0, "total": 1}


def test_evaluator_meeting_snippets_detect_query_passcode_and_dialing_loss():
    from copy import deepcopy
    case = json.loads((CORPUS / "meeting-join-details.json").read_text())
    assert score_case(case, case["provider_response"])["meeting_details"] == {
        "correct": 4, "total": 4}
    for snippet in case["expected"]["meeting_details"][0]["description"]:
        response = deepcopy(case["provider_response"])
        event = response["events"][0]
        event["description"] = event["description"].replace(snippet, "omitted")
        assert score_case(case, response)["meeting_details"] == {"correct": 3, "total": 4}
    assert score_case(case, {"events": []})["meeting_details"] == {"correct": 0, "total": 4}


@pytest.mark.parametrize("details", [None, [], [None], [{"title": ["Project meeting"]}],
                                    [{"description": [""]}], [{"description": ["absent"]}],
                                    [{"description": ["Passcode: 004201"] * 2}]])
def test_evaluator_rejects_invalid_meeting_annotations(details):
    case = json.loads((CORPUS / "meeting-join-details.json").read_text())
    case["expected"]["meeting_details"] = details
    with pytest.raises(ValueError, match="meeting detail|meeting_details"):
        score_case(case, case["provider_response"])


@pytest.mark.parametrize(("start", "correct"), [
    ("2026-10-15T19:00:00-07:00", 1), ("2026-10-16T17:00:00-07:00", 0),
    ("2026-10-15T99:00:00-07:00", 0), ("2026-10-15T17:00:00", 0),
    ("2026-10-15", 0), (False, 0),
])
def test_evaluator_calendar_dates_are_independent_of_clock_times(start, correct):
    case = json.loads((CORPUS / "explicit-practice.json").read_text())
    candidate = {**case["provider_response"]["events"][0], "start": start}
    result = score_case(case, {"events": [candidate]})
    assert result["dates"] == {"correct": correct, "total": 1}
