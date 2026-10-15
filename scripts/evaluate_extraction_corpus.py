"""Optional, non-gating reference-provider evaluation of synthetic corpus cases.

An explicit adapter command reads one case JSON from stdin and writes the AI
Task structured response JSON to stdout. No credentials or provider output are
stored by this script; only fixture IDs and numeric metrics are printed.
"""
from __future__ import annotations

import argparse
from datetime import date, datetime
import json
from pathlib import Path
import subprocess

FIELDS = ("title", "start", "end", "all_day", "location", "description", "assumptions")


def _title(event: dict) -> str | None:
    value = event.get("title")
    return value.strip() if isinstance(value, str) and value.strip() else None


def _matches(expected: list[dict], actual: list[dict]) -> dict[int, int]:
    """Match exact normalized titles, preferring identical intervals for repeats.

    Matching is deliberately conservative: renamed events are missed/spurious,
    not guessed from dates. Reserve exact intervals before pairing remaining
    repeated titles, so a missing occurrence cannot shift every later event.
    """
    matched = {}
    available = set(range(len(actual)))
    for exact_interval in (True, False):
        for index, event in enumerate(expected):
            if index in matched:
                continue
            for candidate in sorted(available):
                other = actual[candidate]
                if _title(event) is None or _title(event) != _title(other):
                    continue
                if exact_interval and any(
                    (value.strip() if isinstance(value, str) else value)
                    != (other_value.strip() if isinstance(other_value, str) else other_value)
                    for key in ("start", "end")
                    for value, other_value in [(event.get(key), other.get(key))]
                ):
                    continue
                matched[index] = candidate
                available.remove(candidate)
                break
    return matched


def _meeting_details(case: dict, expected: list[dict]) -> list[dict]:
    details = case["expected"].get("meeting_details", [{} for _ in expected])
    if not isinstance(details, list) or len(details) != len(expected):
        raise ValueError("meeting_details must align with expected events")
    for event, fields in zip(expected, details):
        if not isinstance(fields, dict):
            raise ValueError("meeting_details must contain field objects")
        for field, snippets in fields.items():
            if (field not in {"location", "description"} or not isinstance(snippets, list)
                    or any(not isinstance(text, str) or not text.strip() for text in snippets)
                    or len(set(snippets)) != len(snippets)):
                raise ValueError("meeting_details must contain unique nonempty field snippets")
            if any(not isinstance(event.get(field), str) or text not in event[field]
                   for text in snippets):
                raise ValueError("meeting detail snippets must occur in the expected field")
    return details


def _calendar_date(value: object, *, all_day: bool) -> date | None:
    if not isinstance(value, str):
        return None
    try:
        if all_day:
            return date.fromisoformat(value.strip())
        # A date alone is not a valid timed-event timestamp.
        if len(value.strip()) <= 10:
            return None
        timestamp = datetime.fromisoformat(value.strip())
        return timestamp.date() if timestamp.utcoffset() is not None else None
    except ValueError:
        return None


def score_case(case: dict, response: dict) -> dict:
    """Score conservative event correspondence and exact normalized field values.

    Confidence is excluded because calibration is not an extraction field.
    Meeting details measure annotated expected snippets, not hallucination rate.
    """
    actual = response.get("events") if isinstance(response, dict) else None
    if not isinstance(actual, list) or any(not isinstance(event, dict) for event in actual):
        raise ValueError("reference response must contain a list of event objects")
    expected = case["expected"]["events"]
    assumptions = case["expected"]["event_assumptions"]
    details = _meeting_details(case, expected)
    matches = _matches(expected, actual)
    slots = max(len(expected), len(actual))
    counts = {field: {"correct": 0, "total": slots} for field in FIELDS}
    dates = {"correct": 0, "total": slots}
    meeting = {"correct": 0, "total": sum(len(snippets) for fields in details
                                           for snippets in fields.values())}
    for index, actual_index in matches.items():
        for field in FIELDS:
            wanted = assumptions[index] if field == "assumptions" else expected[index].get(field)
            candidate = actual[actual_index]
            if field not in candidate and field not in {"location", "description"}:
                continue
            got = candidate.get(field)
            if field == "all_day":
                valid_type = isinstance(got, bool)
            elif field == "assumptions":
                valid_type = (isinstance(got, list) and len(got) <= 8
                              and all(isinstance(note, str) and note.strip()
                                      and len(note) <= 160 for note in got))
            elif field in {"location", "description"}:
                valid_type = got is None or isinstance(got, str)
            else:
                valid_type = isinstance(got, str) and bool(got.strip())
            if isinstance(got, str):
                got = got.strip() or None
            elif field == "assumptions" and valid_type:
                got = [note.strip() for note in got]
            counts[field]["correct"] += valid_type and got == wanted
        candidate = actual[actual_index]
        dates["correct"] += all(
            _calendar_date(candidate.get(key), all_day=expected[index]["all_day"])
            == _calendar_date(expected[index][key], all_day=expected[index]["all_day"])
            for key in ("start", "end"))
        for field, snippets in details[index].items():
            value = candidate.get(field)
            meeting["correct"] += sum(isinstance(value, str) and snippet in value
                                      for snippet in snippets)
    return {"id": case["id"], "expected_events": len(expected),
            "actual_events": len(actual), "event_count_correct": len(actual) == len(expected),
            "matched_events": len(matches), "missed_events": len(expected) - len(matches),
            "spurious_events": len(actual) - len(matches),
            "dates": dates, "meeting_details": meeting, "fields": counts}


def evaluate(corpus: Path, runner: list[str], *, timeout: float = 120) -> dict:
    """Run a caller-supplied provider adapter without a shell or CI dependency."""
    cases = sorted(corpus.glob("*.json"))
    if not cases:
        raise ValueError("corpus contains no JSON cases")
    results = []
    for path in cases:
        case = json.loads(path.read_text())
        # Absolute paths let an adapter load attachment bytes from any cwd.
        request = {key: value for key, value in case.items()
                   if key in {"id", "source_text", "reference_datetime", "time_zone",
                              "source_kind", "attachments"}}
        # Routing fixtures record the actual post-routing parser evidence.
        # Keep this stdlib runner independent of Home Assistant, and never
        # expose outer routing directives or expected routing decisions to the AI.
        request["source_text"] = case.get("parser_source_text", case["source_text"])
        request["fixture_directory"] = str(corpus.resolve())
        completed = subprocess.run(runner, input=json.dumps(request), text=True,
                                   capture_output=True, check=True, timeout=timeout)
        results.append(score_case(case, json.loads(completed.stdout)))
    totals = {field: {key: sum(result["fields"][field][key] for result in results)
                      for key in ("correct", "total")} for field in FIELDS}
    return {"cases": results, "field_totals": totals,
            "event_totals": {key: sum(result[key] for result in results)
                             for key in ("expected_events", "actual_events", "matched_events",
                                         "missed_events", "spurious_events")},
            "date_totals": {key: sum(result["dates"][key] for result in results)
                            for key in ("correct", "total")},
            "meeting_detail_totals": {key: sum(result["meeting_details"][key] for result in results)
                                      for key in ("correct", "total")},
            "event_count_accuracy": sum(result["event_count_correct"] for result in results) / len(results)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=Path("tests/fixtures/extraction"))
    parser.add_argument("--timeout", type=float, default=120)
    parser.add_argument("runner", nargs=argparse.REMAINDER)
    args = parser.parse_args()
    runner = args.runner[1:] if args.runner[:1] == ["--"] else args.runner
    if not runner:
        parser.error("supply an explicit provider adapter command after --")
    if args.timeout <= 0:
        parser.error("timeout must be positive")
    print(json.dumps(evaluate(args.corpus, runner, timeout=args.timeout), indent=2))


if __name__ == "__main__":
    main()
