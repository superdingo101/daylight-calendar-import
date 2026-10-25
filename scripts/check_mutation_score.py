"""Enforce the repository mutation-testing baseline."""

from __future__ import annotations

import json
import os
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

    summary = (
        "## Mutation testing\n\n"
        f"- Score: **{score:.2f}%** (minimum {minimum:.2f}%)\n"
        f"- Killed: {stats['killed']}\n"
        f"- Survived: {stats['survived']}\n"
        f"- No tests: {stats['no_tests']}\n"
        f"- Timeout: {stats['timeout']}\n"
        f"- Suspicious: {stats['suspicious']}\n"
        f"- Segfault: {stats['segfault']}\n"
        f"- Skipped: {stats['skipped']}\n"
        f"- Total: {stats['total']}\n"
    )
    print(summary)

    github_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if github_summary:
        with Path(github_summary).open("a") as stream:
            stream.write(summary)

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
