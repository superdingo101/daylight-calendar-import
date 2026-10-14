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
    attachments = tuple(SourceAttachment(
        id=str(index), media_type=item["media_type"],
        size_bytes=(CORPUS / item["path"]).stat().st_size,
        content_ref=f"media-source://media_source/local/corpus/{item['path']}",
        filename=item["name"],
    ) for index, item in enumerate(case.get("attachments", [])))
    source = SourceDocument(case["id"], SourceKind(case.get("source_kind", "manual_text")),
                            reference, text=case["source_text"], attachments=attachments)
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
    assert case["source_text"].strip() in request["instructions"]
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
    for name in ("explicit-practice", "no-events-newsletter"):
        (isolated / f"{name}.json").write_bytes((CORPUS / f"{name}.json").read_bytes())
    report = evaluate(isolated, [sys.executable, str(adapter)])
    assert report["event_count_accuracy"] == 1
    assert all(total["correct"] == total["total"] for total in report["field_totals"].values())
    with pytest.raises(ValueError, match="no JSON cases"):
        evaluate(tmp_path, [sys.executable, str(adapter)])
