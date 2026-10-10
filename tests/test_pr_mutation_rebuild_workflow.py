"""Contracts for explicit recovery when neither automatic mutation cache is reusable.

These tests deliberately inspect the trusted Actions workflow, not untrusted
PR code, to make the recovery/security boundaries hard to accidentally remove.
"""

from pathlib import Path

import pytest
import yaml


WORKFLOWS = Path(__file__).resolve().parents[1] / ".github" / "workflows"


def _load(name: str) -> dict:
    path = WORKFLOWS / name
    # Mutmut isolates tests under mutants/ and does not copy .github/. Ordinary
    # pytest checks these workflows; mutant collection must not fail because a
    # non-production CI file is absent in mutmut's sandbox.
    if not path.is_file() and WORKFLOWS.parents[1].name == "mutants":
        pytest.skip("GitHub Actions workflow files are not in the mutmut sandbox")
    # Outside mutmut, a missing/renamed workflow is a real regression and
    # must raise FileNotFoundError instead of silently skipping CI contracts.
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def _steps(job: dict) -> list[dict]:
    return job["steps"]


def _find(job: dict, name: str) -> dict:
    return next(step for step in _steps(job) if step.get("name") == name)


def test_manual_rebuild_is_only_explicitly_dispatched() -> None:
    workflow = _load("rebuild-pr-mutation-cache.yml")
    trigger = workflow.get("on", workflow.get(True))
    assert set(trigger) == {"workflow_dispatch"}
    number = trigger["workflow_dispatch"]["inputs"]["pr_number"]
    assert number["required"] is True
    assert number["type"] == "number"
    assert workflow["permissions"] == {"contents": "read"}
    assert workflow["concurrency"]["cancel-in-progress"] is False


def test_recovery_pins_the_actual_pr_merge_and_never_accepts_forks() -> None:
    workflow = _load("rebuild-pr-mutation-cache.yml")
    resolver = workflow["jobs"]["resolve"]
    verify = _find(resolver, "Require main dispatch and trusted same-repo PR")
    text = verify["run"]
    assert 'refs/heads/main' in text
    assert '.state == "open"' in text
    assert '.head.repo.full_name == $repo' in text
    assert '.merge_commit_sha' in text
    assert ".base.ref ==" not in text  # Stacked PRs are supported

    rebuild = workflow["jobs"]["rebuild"]
    checkout = next(step for step in _steps(rebuild) if "actions/checkout@" in step.get("uses", ""))
    assert checkout["with"]["persist-credentials"] is False
    assert checkout["with"]["fetch-depth"] == 0
    assert "needs.resolve.outputs.merge_sha" in checkout["with"]["ref"]
    assert "contents" in rebuild["permissions"]
    assert "write" not in str(rebuild["permissions"])
    exact = _find(rebuild, "Verify pinned PR source")["run"]
    assert 'git rev-parse HEAD^2' in exact
    assert 'EXPECTED_HEAD' in exact
    changed = _find(rebuild, "Verify PR head has not moved")["run"]
    assert 'EXPECTED_MERGE' in str(_find(rebuild, "Verify PR head has not moved")["env"])
    assert 'EXPECTED_HEAD' in str(_find(rebuild, "Verify PR head has not moved")["env"])
    assert 'exit 1' in changed


def test_explicit_clean_job_does_not_restore_old_state_and_checks_mutation_score() -> None:
    job = _load("rebuild-pr-mutation-cache.yml")["jobs"]["rebuild"]
    assert job["timeout-minutes"] == 60
    steps = _steps(job)
    labels = [step.get("name", "") for step in steps]
    assert "Discard previous mutation state" in labels
    assert "Run full mutations once" in labels
    assert "Enforce the existing mutation threshold" in labels
    assert "Stamp fully verified PR mutation state" in labels
    assert "Save PR recovery baseline" in labels
    assert labels.index("Enforce the existing mutation threshold") < labels.index(
        "Stamp fully verified PR mutation state"
    )
    assert labels.index("Verify PR head has not moved") < labels.index(
        "Save PR recovery baseline"
    )
    assert "mutmut run" in _find(job, "Run full mutations once")["run"]
    assert "check_mutation_score.py" in _find(job, "Enforce the existing mutation threshold")["run"]
    assert "rm -rf mutants" in _find(job, "Discard previous mutation state")["run"]
    assert not any("cache/restore" in step.get("uses", "") for step in steps)
    saved = _find(job, "Save PR recovery baseline")
    assert "rebuild-pr-" in saved["with"]["key"]
    assert "needs.resolve.outputs.merge_sha" in saved["with"]["key"]


def test_rebuild_cache_is_restored_before_shared_main_fallback() -> None:
    job = _load("mutation.yml")["jobs"]["mutation"]
    labels = [step.get("name", "") for step in _steps(job)]
    assert labels.index("Validate PR mutation cache") < labels.index(
        "Restore rebuilt PR mutation baseline"
    )
    assert labels.index("Validate rebuilt PR mutation baseline") < labels.index(
        "Restore main mutation baseline"
    )
    assert labels.index("Require reusable mutation baseline") < labels.index(
        "Run incremental mutations"
    )
    recovered = _find(job, "Restore rebuilt PR mutation baseline")
    assert "rebuild-pr-" in recovered["with"]["key"]
    main = _find(job, "Restore main mutation baseline")
    assert "steps.rebuilt-cache.outputs.valid != 'true'" in main["if"]
    assert job["timeout-minutes"] == 25
    assert "exit 1" in _find(job, "Require reusable mutation baseline")["run"]


def test_successful_recovery_uses_normal_required_check_not_an_imitation() -> None:
    workflow = _load("rebuild-pr-mutation-cache.yml")
    finish = workflow["jobs"]["finish"]
    assert finish["permissions"]["actions"] == "write"
    assert finish["permissions"]["statuses"] == "write"
    assert not any("checkout" in step.get("uses", "") for step in _steps(finish))
    text = _find(finish, "Publish result and rerun failed PR mutation check")["run"]
    assert '"PR Mutation Cache Rebuild"' in text
    assert "rerun-failed-jobs" in text
    # GitHub's workflow_runs[].head_sha is the PR head, even though the
    # event checkout runs on the synthetic test merge. Require the current
    # validated head SHA so an older failed run cannot be accidentally retried.
    assert '.head_sha == $head' in text
    assert '--arg head "$EXPECTED"' in text
    assert '"Mutation score"' not in text
    assert "OUTCOME" in text


def test_missing_workflow_fails_in_a_normal_checkout(monkeypatch, tmp_path) -> None:
    monkeypatch.setitem(globals(), "WORKFLOWS", tmp_path / ".github" / "workflows")
    with pytest.raises(FileNotFoundError):
        _load("rebuild-pr-mutation-cache.yml")


def test_missing_workflow_skips_only_inside_mutmut_sandbox(monkeypatch, tmp_path) -> None:
    sandbox = tmp_path / "mutants" / ".github" / "workflows"
    monkeypatch.setitem(globals(), "WORKFLOWS", sandbox)
    with pytest.raises(pytest.skip.Exception):
        _load("rebuild-pr-mutation-cache.yml")
