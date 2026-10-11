"""Contracts for PR-scoped mutation checks and clean release proof."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest
import yaml

from scripts import release_mutation_proof


def _git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def test_release_proof_contains_exact_git_tree_and_run(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _git("init")
    _git("config", "user.email", "ci@example.invalid")
    _git("config", "user.name", "CI")
    manifest = Path("custom_components/daylight_calendar_import/manifest.json")
    manifest.parent.mkdir(parents=True)
    manifest.write_text('{"version":"0.6.0"}')
    _git("add", ".")
    _git("commit", "-m", "candidate")
    mutation_stats = Path("mutants/mutmut-cicd-stats.json")
    mutation_stats.parent.mkdir()
    mutation_stats.write_text('{"total":100,"killed":97,"survived":3,"check_was_interrupted_by_user":false}')
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    release_mutation_proof.create("v0.6.0")
    proof = json.loads(release_mutation_proof.PROOF_FILE.read_text())
    assert proof["mode"] == "clean"
    assert proof["release_tag"] == "v0.6.0"
    assert proof["source_commit"] == _git("rev-parse", "HEAD")
    assert proof["source_tree"] == _git("rev-parse", "HEAD^{tree}")
    assert proof["workflow_run_id"] == 12345
    with pytest.raises(ValueError, match="manifest"):
        release_mutation_proof.create("v0.6.1")


@pytest.mark.parametrize("valid_tree,successful", [(True, True), (False, True), (True, False)])
def test_publisher_checks_successful_first_party_run_and_matching_tree(
    monkeypatch, valid_tree, successful,
):
    monkeypatch.setenv("GITHUB_REPOSITORY", "superdingo101/daylight-calendar-import")
    run = {"id": 123, "event": "workflow_dispatch", "head_branch": "main",
           "conclusion": "success" if successful else "failure"}
    proof = {"schema": 1, "mode": "clean", "release_tag": "v0.6.0",
             "source_tree": "correct" if valid_tree else "wrong",
             "source_commit": "a" * 40, "workflow_run_id": 123}

    def fake_check_output(cmd, *, text):
        if "/artifacts" in cmd[2]:
            return json.dumps({"artifacts": [{"name": "release-clean-v0.6.0", "expired": False}]})
        return json.dumps({"workflow_runs": [run]})

    def fake_run(cmd, *, capture_output, text, check):
        directory = Path(cmd[cmd.index("--dir") + 1])
        (directory / release_mutation_proof.PROOF_FILE.name).write_text(json.dumps(proof))
        return SimpleNamespace(returncode=0)

    monkeypatch.setattr(release_mutation_proof.subprocess, "check_output", fake_check_output)
    monkeypatch.setattr(release_mutation_proof.subprocess, "run", fake_run)
    if successful and valid_tree:
        assert release_mutation_proof.find_verified_run("v0.6.0", "correct")[0] == 123
    else:
        with pytest.raises(RuntimeError, match="No successful"):
            release_mutation_proof.find_verified_run("v0.6.0", "correct")



ROOT = Path(__file__).resolve().parents[1]


def workflow(name):
    path = ROOT / ".github" / "workflows" / name
    if not path.exists() and ROOT.name == "mutants":
        pytest.skip("CI workflow not copied into mutmut sandbox")
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def test_pr_mutation_scope_never_requires_rebuild_cache():
    job = workflow("mutation.yml")["jobs"]["mutation"]
    assert job["name"] == "Mutation score"
    steps = job["steps"]
    run = "\n".join(str(step.get("run", "")) for step in steps)
    assert "scripts/pr_mutation.py prepare" in run
    assert "scripts/pr_mutation.py restore" in run
    assert "mutmut run" in run
    assert "scripts/check_mutation_score.py" in run
    assert all("actions/cache/restore" not in str(step.get("uses", "")) for step in steps)


def test_full_nightly_and_release_checks_remain():
    clean = workflow("mutation-clean.yml")
    job = clean["jobs"]["clean"]
    trigger = clean.get("on", clean.get(True))
    assert "schedule" in trigger and "workflow_call" in trigger
    assert job["timeout-minutes"] == 60
    scripts = "\n".join(str(step.get("run", "")) for step in job["steps"])
    assert "rm -rf mutants" in scripts
    assert "mutmut run" in scripts
    assert "Release Mutation Validation" in scripts
    updater = workflow("update-integration-version.yml")
    assert updater["jobs"]["clean-release-mutation"]["uses"] == "./.github/workflows/mutation-clean.yml"
    publisher = workflow("publish-hacs-release.yml")
    scripts = "\n".join(str(step.get("run", "")) for step in publisher["jobs"]["publish"]["steps"])
    assert "release_mutation_proof.py verify" in scripts
