"""Optional, non-gating reference-provider evaluation of synthetic corpus cases.

An explicit adapter command reads one case JSON from stdin and writes the AI
Task structured response JSON to stdout. No credentials or provider output are
stored by this script; only aggregate field accuracy is printed.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import subprocess

FIELDS = ("title", "start", "end", "all_day", "location", "description", "assumptions")


def score_case(case: dict, response: dict) -> dict:
    """Compare ordered expected events; extra/missing events count as failures.

    Order is part of the corpus contract. Confidence is deliberately excluded
    because provider calibration is not an objective extraction field.
    """
    actual = response.get("events")
    if not isinstance(actual, list) or any(not isinstance(event, dict) for event in actual):
        raise ValueError("reference response must contain a list of event objects")
    expected = case["expected"]["events"]
    assumptions = case["expected"]["event_assumptions"]
    slots = max(len(expected), len(actual))
    counts = {field: {"correct": 0, "total": slots} for field in FIELDS}
    for index in range(min(len(expected), len(actual))):
        for field in FIELDS:
            wanted = assumptions[index] if field == "assumptions" else expected[index].get(field)
            candidate = actual[index]
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
    return {"id": case["id"], "expected_events": len(expected),
            "actual_events": len(actual), "event_count_correct": len(actual) == len(expected),
            "fields": counts}


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
