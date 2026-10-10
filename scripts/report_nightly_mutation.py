"""Expose the nightly clean mutation result in a persistent GitHub issue."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess

ISSUE_TITLE = "Mutation Health — Nightly Clean Validation"
STATS = Path("mutants/mutmut-cicd-stats.json")


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True).strip()


def main() -> None:
    repo = os.environ["GITHUB_REPOSITORY"]
    run_id = os.environ["GITHUB_RUN_ID"]
    url = f"https://github.com/{repo}/actions/runs/{run_id}"
    success = all(
        os.environ.get(name) == "success"
        for name in ("MUTATION_OUTCOME", "ENFORCE_OUTCOME", "EXPORT_OUTCOME",
                     "DIAGNOSTICS_OUTCOME", "NIGHTLY_CACHE_OUTCOME")
    )
    stats = json.loads(STATS.read_text()) if STATS.is_file() else {}
    total = stats.get("total", 0) - stats.get("skipped", 0)
    score = (stats.get("killed", 0) + stats.get("timeout", 0)) / total * 100 if total > 0 else None
    status = "PASS" if success else "FAIL"
    text = (
        "## Nightly clean mutation health\n\n"
        f"- Status: **{status}**\n"
        f"- Last checked: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}\n"
        f"- Mutation score: **{score:.2f}%**\n" if score is not None else
        "## Nightly clean mutation health\n\n"
        f"- Status: **{status}**\n"
        "- Mutation score: unavailable (run failed before statistics)\n"
    )
    text += (
        f"- Surviving mutants: {stats.get('survived', 'unavailable')}\n"
        f"- Mutants evaluated: {total if total > 0 else 'unavailable'}\n"
        f"- Details and survivor reports: [GitHub Actions run]({url})\n"
        "\nNightly clean testing does not replace the separate release validation gate.\n"
    )
    with Path("nightly-mutation-health.md").open("w") as output:
        output.write(text)
    issues = json.loads(gh("issue", "list", "--repo", repo, "--state", "open",
                          "--limit", "100", "--json", "number,title"))
    match = next((x for x in issues if x["title"] == ISSUE_TITLE), None)
    if match:
        number = str(match["number"])
        gh("issue", "edit", number, "--repo", repo, "--body-file", "nightly-mutation-health.md")
    else:
        created = gh("issue", "create", "--repo", repo, "--title", ISSUE_TITLE,
                     "--body-file", "nightly-mutation-health.md")
        number = created.rstrip("/").split("/")[-1]
    gh("issue", "comment", number, "--repo", repo, "--body",
       f"**{status}** — {score:.2f}% mutation score — {url}" if score is not None else
       f"**{status}** — mutation score unavailable — {url}")
    print(f"Nightly mutation health: https://github.com/{repo}/issues/{number}")


if __name__ == "__main__":
    main()
