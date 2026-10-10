"""Contract tests for mutation caching and release validation evidence."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
from types import SimpleNamespace

import pytest

from scripts import mutation_cache
from scripts import release_mutation_proof


def _git(*args):
    return subprocess.check_output(["git", *args], text=True).strip()


def test_cache_invalidates_test_changes_but_not_production_function_changes(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _git("init")
    paths = {
        "custom_components/daylight_calendar_import/example.py": "def a(): return 1\n",
        "tests/test_example.py": "def test_a(): assert True\n",
        "requirements_test.txt": "pytest\n",
        "README.md": "Documentation\n",
    }
    for name, content in paths.items():
        path = Path(name)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    _git("add", ".")
    original = mutation_cache.fingerprint()

    Path("custom_components/daylight_calendar_import/example.py").write_text("def a(): return 2\n")
    assert mutation_cache.fingerprint() == original

    Path("tests/test_example.py").write_text("def test_a(): assert False\n")
    assert mutation_cache.fingerprint() != original

    Path("tests/test_example.py").write_text(paths["tests/test_example.py"])
    assert mutation_cache.fingerprint() == original

    Path("requirements_test.txt").write_text("pytest==9\n")
    assert mutation_cache.fingerprint() != original


def test_bad_or_missing_provenance_cannot_restore_cache(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _git("init")
    Path("tests").mkdir()
    Path("tests/test.py").write_text("pass\n")
    _git("add", ".")
    Path("mutants/custom_components").mkdir(parents=True)
    Path("mutants/custom_components/a.meta").write_text("{}")
    mutation_cache.STATS.parent.mkdir(parents=True, exist_ok=True)
    mutation_cache.STATS.write_text("{}")
    assert not mutation_cache.validate()

    mutation_cache.CACHE_META.write_text(
        json.dumps({"schema": mutation_cache.CACHE_SCHEMA, "fingerprint": mutation_cache.fingerprint()})
    )
    assert mutation_cache.validate()
    Path("tests/test.py").write_text("assert True\n")
    assert not mutation_cache.validate()


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
