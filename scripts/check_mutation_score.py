"""Enforce the repository mutation-testing baseline."""

from __future__ import annotations

import json
from pathlib import Path
import sys


BASELINE_PATH = Path("mutation-baseline.json")
STATS_PATH = Path("mutants/mutmut-cicd-stats.json")


def main() -> int:
    """Report mutation results and fail when quality regresses."""
    baseline = json.loads(BASELINE_PATH.read_text())
    stats = json.loads(STATS_PATH.read_text())

    tested = stats["total"] - stats["skipped"]
    score = 0.0 if tested <= 0 else (
        (stats["killed"] + stats["timeout"]) / tested * 100
    )
    minimum = float(baseline["minimum_score"])

    print(
        "Mutation score: "
        f"{score:.2f}% "
        f"({stats['killed']} killed, {stats['survived']} survived, "
        f"{stats['total']} total; minimum {minimum:.2f}%)"
    )

    failures: list[str] = []
    if score + 1e-9 < minimum:
        failures.append(
            f"mutation score {score:.2f}% is below the {minimum:.2f}% baseline"
        )

    for key in ("no_tests", "suspicious", "segfault"):
        if stats.get(key, 0):
            failures.append(f"{key} must remain zero (got {stats[key]})")

    if stats.get("check_was_interrupted_by_user", False):
        failures.append("mutation run was interrupted")

    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
