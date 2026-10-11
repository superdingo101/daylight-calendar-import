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
    # The old baseline-rebuild cache is forbidden; a new, PR-scoped cache
    # is allowed only with provenance validation and post-run stamping.
    restore = [step for step in steps if step.get("uses") == "actions/cache/restore@v4"]
    save = [step for step in steps if step.get("uses") == "actions/cache/save@v4"]
    assert len(restore) == len(save) == 1
    assert "github.event.pull_request.number" in restore[0]["with"]["key"]
    assert restore[0]["with"]["key"] == save[0]["with"]["key"]
    assert "scripts/pr_mutation_cache.py restore" in run
    assert "scripts/pr_mutation_cache.py stamp" in run
    assert all("scripts/mutation_cache.py" not in str(step) for step in steps)


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

def test_nightly_reporting_does_not_require_a_deleted_cache_step(monkeypatch, tmp_path):
    from scripts import report_nightly_mutation

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "superdingo101/daylight-calendar-import")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("NIGHTLY_CLEAN_OUTCOME", "success")
    monkeypatch.delenv("NIGHTLY_CACHE_OUTCOME", raising=False)
    stats = Path("mutants/mutmut-cicd-stats.json")
    stats.parent.mkdir()
    stats.write_text(json.dumps({
        "total": 100, "skipped": 0, "killed": 97,
        "timeout": 0, "survived": 3,
    }))
    calls = []

    def gh(*arguments):
        calls.append(arguments)
        if arguments[:2] == ("issue", "list"):
            return "[]"
        if arguments[:2] == ("issue", "create"):
            return "https://github.com/superdingo101/daylight-calendar-import/issues/123"
        return ""

    monkeypatch.setattr(report_nightly_mutation, "gh", gh)
    report_nightly_mutation.main()
    assert not Path("nightly-mutation-health.md").exists()
    assert [args[:2] for args in calls] == [("issue", "list")]

    clean = workflow("mutation-clean.yml")
    reporter_job = clean["jobs"]["nightly-report"]
    assert reporter_job["needs"] == "clean"
    assert "always()" in reporter_job["if"]
    assert "schedule" in reporter_job["if"]
    assert reporter_job["timeout-minutes"] == 5
    assert any("download-artifact" in step.get("uses", "")
               for step in reporter_job["steps"])
    nightly = next(step for step in reporter_job["steps"]
                   if step.get("name") == "Update visible nightly mutation health")
    assert "needs.clean.result" in nightly["env"]["NIGHTLY_CLEAN_OUTCOME"]
    assert "continue-on-error" not in nightly  # Reporting API errors must fail the job.
    assert "NIGHTLY_CACHE_OUTCOME" not in str(nightly.get("env"))
    assert all("report_nightly_mutation.py" not in str(step.get("run", ""))
               for step in clean["jobs"]["clean"]["steps"])


@pytest.fixture
def nightly_reporter(monkeypatch, tmp_path):
    """Prepare a nightly run with passing steps and 97% mutation score."""
    from scripts import report_nightly_mutation

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GITHUB_REPOSITORY", "superdingo101/daylight-calendar-import")
    monkeypatch.setenv("GITHUB_RUN_ID", "12345")
    monkeypatch.setenv("NIGHTLY_CLEAN_OUTCOME", "success")
    stats = Path("mutants/mutmut-cicd-stats.json")
    stats.parent.mkdir()
    stats.write_text(json.dumps({
        "total": 100, "skipped": 0, "killed": 97, "timeout": 0, "survived": 3,
    }))
    return report_nightly_mutation, stats


def _nightly_calls(monkeypatch, reporter, issues):
    calls = []

    def gh(*args):
        calls.append(args)
        if args[:2] == ("issue", "list"):
            return json.dumps(issues)
        if args[:2] == ("issue", "create"):
            return "https://github.com/superdingo101/daylight-calendar-import/issues/123"
        return ""

    monkeypatch.setattr(reporter, "gh", gh)
    return calls


def test_nightly_pass_closes_existing_issue(monkeypatch, nightly_reporter):
    reporter, _stats = nightly_reporter
    calls = _nightly_calls(monkeypatch, reporter, [
        {"number": 115, "title": reporter.ISSUE_TITLE}
    ])
    reporter.main()
    assert [args[1] for args in calls] == ["list", "comment", "close"]
    assert calls[1][:3] == ("issue", "comment", "115")
    assert "**PASS** — 97.00% mutation score" in calls[1][-1]
    assert calls[2] == ("issue", "close", "115", "--repo",
                        "superdingo101/daylight-calendar-import",
                        "--reason", "completed")
    assert not Path("nightly-mutation-health.md").exists()


def test_nightly_failure_creates_issue(monkeypatch, nightly_reporter):
    reporter, _stats = nightly_reporter
    monkeypatch.setenv("NIGHTLY_CLEAN_OUTCOME", "failure")
    calls = _nightly_calls(monkeypatch, reporter, [])
    reporter.main()
    assert [args[1] for args in calls] == ["list", "create", "comment"]
    assert reporter.ISSUE_TITLE in calls[1]
    report = Path("nightly-mutation-health.md").read_text()
    assert "- Status: **FAIL**" in report
    assert "- Mutation score: **97.00%**" in report
    assert calls[2][:3] == ("issue", "comment", "123")
    assert "**FAIL** — 97.00% mutation score" in calls[2][-1]


def test_nightly_repeated_failure_updates_issue(monkeypatch, nightly_reporter):
    reporter, _stats = nightly_reporter
    monkeypatch.setenv("NIGHTLY_CLEAN_OUTCOME", "failure")
    calls = _nightly_calls(monkeypatch, reporter, [
        {"number": 115, "title": reporter.ISSUE_TITLE}
    ])
    reporter.main()
    assert [args[1] for args in calls] == ["list", "edit", "comment"]
    assert calls[1][:3] == ("issue", "edit", "115")
    assert calls[2][:3] == ("issue", "comment", "115")


def test_nightly_failure_without_stats_still_opens_issue(monkeypatch, nightly_reporter):
    reporter, stats = nightly_reporter
    stats.unlink()
    monkeypatch.setenv("NIGHTLY_CLEAN_OUTCOME", "failure")
    calls = _nightly_calls(monkeypatch, reporter, [])
    reporter.main()
    assert [args[1] for args in calls] == ["list", "create", "comment"]
    assert "unavailable (run failed before statistics)" in Path("nightly-mutation-health.md").read_text()
    assert "mutation score unavailable" in calls[2][-1]


def test_nightly_pass_does_not_touch_unrelated_issue(monkeypatch, nightly_reporter):
    reporter, _stats = nightly_reporter
    calls = _nightly_calls(monkeypatch, reporter, [
        {"number": 999, "title": "Unrelated issue"}
    ])
    reporter.main()
    assert [args[1] for args in calls] == ["list"]



@pytest.mark.parametrize("outcome", ["failure", "cancelled"])
def test_nightly_job_failure_or_timeout_creates_alert(monkeypatch, nightly_reporter, outcome):
    reporter, stats = nightly_reporter
    stats.unlink()  # A timed-out job might never upload diagnostic statistics.
    monkeypatch.setenv("NIGHTLY_CLEAN_OUTCOME", outcome)
    calls = _nightly_calls(monkeypatch, reporter, [])
    reporter.main()
    assert [args[1] for args in calls] == ["list", "create", "comment"]
    assert "- Status: **FAIL**" in Path("nightly-mutation-health.md").read_text()


def test_nightly_issue_query_filters_before_limit(monkeypatch, nightly_reporter):
    reporter, _stats = nightly_reporter
    calls = _nightly_calls(monkeypatch, reporter, [])
    reporter.main()
    query = calls[0]
    assert query[:2] == ("issue", "list")
    assert query[query.index("--search") + 1] == f'in:title "{reporter.ISSUE_TITLE}"'
    assert "--limit" in query
    assert [args[1] for args in calls] == ["list"]
