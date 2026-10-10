"""Create and verify evidence of full release mutation testing.

The publisher trusts only artifacts from successful *Update Integration Version*
workflow runs on main, never incremental PR results or arbitrary status checks.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

PROOF_FILE = Path("release-mutation-proof.json")
PROOF_SCHEMA = 1
TAG_PATTERN = re.compile(r"v\d+\.\d+\.\d+(?:-[0-9A-Za-z][0-9A-Za-z.-]*)?\Z")


def git_value(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def create(tag: str) -> None:
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("Invalid release tag")
    manifest = json.loads(Path("custom_components/daylight_calendar_import/manifest.json").read_text())
    if manifest.get("version") != tag[1:]:
        raise ValueError("Candidate manifest version does not match requested release")
    stats = json.loads(Path("mutants/mutmut-cicd-stats.json").read_text())
    if not stats.get("total") or stats.get("check_was_interrupted_by_user"):
        raise ValueError("Missing or incomplete mutation statistics")
    proof = {
        "schema": PROOF_SCHEMA,
        "mode": "clean",
        "release_tag": tag,
        "source_commit": git_value("rev-parse", "HEAD"),
        "source_tree": git_value("rev-parse", "HEAD^{tree}"),
        "workflow_run_id": int(os.environ["GITHUB_RUN_ID"]),
        "score_stats": stats,
    }
    PROOF_FILE.write_text(json.dumps(proof, indent=2, sort_keys=True) + "\n")


def find_verified_run(tag: str, source_tree: str) -> tuple[int, dict]:
    """Fail closed when no successful first-party clean run tested this tree."""
    repo = os.environ["GITHUB_REPOSITORY"]
    url = f"repos/{repo}/actions/workflows/update-integration-version.yml/runs?status=success&per_page=100"
    runs = json.loads(subprocess.check_output(["gh", "api", url], text=True))["workflow_runs"]
    artifact_name = f"release-clean-{tag}"
    for run in runs:
        if (run.get("conclusion") != "success"
                or run.get("event") != "workflow_dispatch"
                or run.get("head_branch") != "main"):
            continue
        run_id = run["id"]
        artifacts = json.loads(subprocess.check_output(
            ["gh", "api", f"repos/{repo}/actions/runs/{run_id}/artifacts"], text=True,
        ))["artifacts"]
        if not any(a["name"] == artifact_name and not a["expired"] for a in artifacts):
            continue
        with tempfile.TemporaryDirectory(prefix="release-mutation-") as directory:
            result = subprocess.run(
                ["gh", "run", "download", str(run_id), "--repo", repo,
                 "--name", artifact_name, "--dir", directory],
                capture_output=True, text=True, check=False,
            )
            if result.returncode:
                continue
            file_path = Path(directory) / PROOF_FILE.name
            try:
                proof = json.loads(file_path.read_text())
            except (OSError, ValueError):
                continue
        if (proof.get("schema") == PROOF_SCHEMA
                and proof.get("mode") == "clean"
                and proof.get("release_tag") == tag
                and proof.get("source_tree") == source_tree
                and proof.get("workflow_run_id") == run_id
                and re.fullmatch(r"[0-9a-f]{40}", proof.get("source_commit", ""))):
            return run_id, proof
    raise RuntimeError(
        "No successful, unexpired clean mutation proof matches this release tree. "
        "Run Update Integration Version again against the desired release candidate."
    )


def verify(tag: str) -> None:
    if not TAG_PATTERN.fullmatch(tag):
        raise ValueError("Invalid release tag")
    manifest = json.loads(Path("custom_components/daylight_calendar_import/manifest.json").read_text())
    if manifest.get("version") != tag[1:]:
        raise ValueError("Manifest version does not match release tag")
    source_commit = git_value("rev-parse", "HEAD")
    tree = git_value("rev-parse", "HEAD^{tree}")
    run_id, proof = find_verified_run(tag, tree)
    print(f"Verified clean mutation run {run_id} for {tag} on tree {tree}")
    print(f"Tested commit: {proof['source_commit']}; published commit: {source_commit}")
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        with Path(summary_path).open("a") as output:
            output.write(
                f"## Release mutation validation\n\n"
                f"- Clean validation: **passed**, [workflow run](https://github.com/{os.environ['GITHUB_REPOSITORY']}/actions/runs/{run_id})\n"
                f"- Release: `{tag}`\n"
                f"- Verified source tree: `{tree}`\n"
                f"- Publishing commit: `{source_commit}`\n"
            )


def main() -> int:
    if len(sys.argv) != 3 or sys.argv[1] not in {"create", "verify"}:
        print("Usage: release_mutation_proof.py [create|verify] vX.Y.Z", file=sys.stderr)
        return 2
    try:
        if sys.argv[1] == "create":
            create(sys.argv[2])
        else:
            verify(sys.argv[2])
    except (ValueError, RuntimeError, subprocess.CalledProcessError, KeyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
