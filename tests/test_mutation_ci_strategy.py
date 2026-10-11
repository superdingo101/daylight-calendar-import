"""Contracts for PR-scoped mutation runs and clean release validation."""
from pathlib import Path

import pytest
import yaml


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
