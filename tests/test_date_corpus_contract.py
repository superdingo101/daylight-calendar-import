"""Independent calendar invariants for synthetic temporal extraction fixtures."""
from datetime import datetime, timedelta
import json
from pathlib import Path

CORPUS = Path(__file__).parent / "fixtures" / "extraction"


def case(name):
    return json.loads((CORPUS / f"dates-{name}.json").read_text())


def test_workshop_corpus_contains_every_explicit_event_in_order():
    fixture = case("24-workshops")
    events = fixture["expected"]["events"]
    assert len(events) == 24
    for index, event in enumerate(events, 1):
        assert event["title"] == f"Workshop {index:02}"
        assert event["title"] in fixture["source_text"]
        assert event["start"][:10] in fixture["source_text"]
        assert datetime.fromisoformat(event["end"]) - datetime.fromisoformat(event["start"]) == timedelta(hours=1)


def test_dst_offsets_represent_real_elapsed_hour():
    for name in ("dst-fall-back", "dst-spring-forward"):
        event = case(name)["expected"]["events"][0]
        assert datetime.fromisoformat(event["end"]) - datetime.fromisoformat(event["start"]) == timedelta(hours=1)
    event = case("dst-fall-back")["expected"]["events"][0]
    assert event["start"][:19] == event["end"][:19]


def test_all_day_exclusive_end_and_relative_context():
    events = case("all-day-and-multiday")["expected"]["events"]
    assert [(event["start"], event["end"]) for event in events] == [
        ("2026-10-16", "2026-10-17"), ("2026-10-17", "2026-10-20")]
    assert all(event["all_day"] for event in events)
    relative = case("relative-tomorrow")
    assert relative["expected"]["events"][0]["start"][:10] == "2026-10-15"
    assert len(relative["expected"]["event_assumptions"][0]) == 2


def test_incomplete_cancelled_and_rescheduled_sources_do_not_invent_drafts():
    for name in ("ambiguous-numeric", "missing-year", "missing-end", "cancellation-only"):
        assert case(name)["expected"]["events"] == []
    replacement = case("reschedule-announcement")["expected"]["events"]
    assert len(replacement) == 1
    assert replacement[0]["start"][:10] == "2026-10-22"
