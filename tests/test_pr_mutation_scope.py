"""Unit tests for PR mutation selection, including stacked PRs."""
from pathlib import Path
import json
import subprocess
import tomllib

import pytest
from scripts import pr_mutation

BASE = "a" * 40
HEAD = "b" * 40
SRC = pr_mutation.SOURCE


def test_stacked_pr_uses_exact_supplied_git_shas(monkeypatch):
    calls = []
    def git(args):
        calls.append(args)
        return (SRC + "storage.py\0").encode().replace(b"\\0", b"\0")
    monkeypatch.setattr(pr_mutation.subprocess, "check_output", git)
    assert pr_mutation.changed_paths(BASE, HEAD) == {SRC + "storage.py"}
    assert calls[0][-2:] == [f"{BASE}...{HEAD}", "--"]
    assert "--no-renames" in calls[0]



def test_target_branch_advances_without_polluting_pr_mutation_scope(tmp_path, monkeypatch):
    """A newer base must not turn base-only tests into PR test deletions."""
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI")
    source = Path(SRC + "storage.py")
    source.parent.mkdir(parents=True)
    source.write_text("VALUE = 1\n")
    git("add", ".")
    git("commit", "-m", "shared ancestor")
    ancestor = git("rev-parse", "HEAD")

    git("checkout", "-b", "feature")
    source.write_text("VALUE = 2\n")
    git("add", ".")
    git("commit", "-m", "feature change")
    feature = git("rev-parse", "HEAD")

    git("checkout", "-b", "target", ancestor)
    base_only = Path("tests/test_base_only.py")
    base_only.parent.mkdir()
    base_only.write_text("def test_new(): assert True\n")
    git("add", ".")
    git("commit", "-m", "base advances")
    target = git("rev-parse", "HEAD")

    # Wrong direct-tip diff interprets a base-only test as removed.
    assert str(base_only) in subprocess.check_output(
        ["git", "diff", "--name-only", target, feature], text=True
    ).splitlines()
    # Correct three-dot comparison captures only the feature's changes.
    assert pr_mutation.changed_paths(target, feature) == {str(source)}
    git("checkout", "feature")
    assert pr_mutation.select(target, feature)[0] == "scoped"



@pytest.mark.parametrize("destination", ["archive/test_storage.py", "tests/test_renamed.py"])
def test_renamed_application_test_preserves_deleted_old_path_and_runs_full(
    tmp_path, monkeypatch, destination,
):
    """Git rename detection must not hide deleted test paths from the safety gate."""
    monkeypatch.chdir(tmp_path)

    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init")
    git("config", "user.email", "ci@example.invalid")
    git("config", "user.name", "CI")
    original = Path("tests/test_storage.py")
    original.parent.mkdir()
    original.write_text("def test_example(): assert True\n", encoding="utf-8")
    git("add", ".")
    git("commit", "-m", "original test")
    base = git("rev-parse", "HEAD")

    renamed = Path(destination)
    renamed.parent.mkdir(parents=True, exist_ok=True)
    git("mv", str(original), str(renamed))
    git("commit", "-m", "move existing application test")
    head = git("rev-parse", "HEAD")

    assert pr_mutation.changed_paths(base, head) == {str(original), str(renamed)}
    assert str(original) in pr_mutation.changed_paths(base, head, "MRTD")
    assert pr_mutation.select(base, head)[0] == "full"


@pytest.mark.parametrize("kind", ["M", "D"])
def test_exporter_only_test_changes_are_not_application_mutation_inputs(
    monkeypatch, kind,
):
    ci_test = "tests/test_export_mutation_survivors.py"

    def changed(base, head, diff_kind="ACMRTD"):
        return {ci_test} if diff_kind in ("ACMRTD", "MRTD") else set()

    monkeypatch.setattr(pr_mutation, "changed_paths", changed)
    assert ci_test in pr_mutation.CI_TESTS
    assert pr_mutation.select(BASE, HEAD)[0] == "skip"


def test_rejects_git_options_as_refs():
    with pytest.raises(ValueError):
        pr_mutation.changed_paths("--help", HEAD)


def test_changed_source_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name = SRC + "journal.py"
    path = Path(name)
    path.parent.mkdir(parents=True)
    path.write_text("def f(): return 1")
    monkeypatch.setattr(pr_mutation, "changed_paths", lambda *a, **kw: {name})
    assert pr_mutation.select(BASE, HEAD)[0:2] == ("scoped", [name])


def test_changed_test_maps_coverage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    t = Path("tests/test_journal.py")
    t.parent.mkdir()
    t.touch()
    def changed(base, head, kind="ACMRTD"):
        return set() if kind == "MRTD" else {str(t)}
    monkeypatch.setattr(pr_mutation, "changed_paths", changed)
    monkeypatch.setattr(pr_mutation, "test_coverage", lambda tests: {SRC + "journal.py"})
    assert pr_mutation.select(BASE, HEAD)[1] == [SRC + "journal.py"]


@pytest.mark.parametrize("path", ["tests/conftest.py", "requirements_test.txt", "pyproject.toml"])
def test_global_inputs_trigger_full(monkeypatch, path):
    monkeypatch.setattr(pr_mutation, "changed_paths", lambda *a, **kw: {path})
    assert pr_mutation.select(BASE, HEAD)[0] == "full"


def test_frontend_only_skips(monkeypatch):
    monkeypatch.setattr(pr_mutation, "changed_paths", lambda *a, **kw: {SRC + "frontend/panel.js"})
    assert pr_mutation.select(BASE, HEAD)[0] == "skip"


def test_configuration_round_trip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = '[project]\nname="demo"\n\n[tool.mutmut]\nsource_paths=["src/"]\n\n[tool.other]\nflag=true\n'
    Path("pyproject.toml").write_text(source)
    pr_mutation.configure_only_mutate([SRC + "journal.py"])
    obj = tomllib.loads(Path("pyproject.toml").read_text())
    assert obj["tool"]["mutmut"]["only_mutate"] == [SRC + "journal.py"]
    assert obj["tool"]["other"]["flag"] is True
    pr_mutation.restore()
    assert Path("pyproject.toml").read_text() == source

def test_existing_test_edits_trigger_safe_full_fallback(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    test = Path("tests/test_existing.py")
    test.parent.mkdir()
    test.touch()
    def changed(base, head, kind="ACMRTD"):
        return {str(test)}  # tracked existing test, so also in MRTD diff
    monkeypatch.setattr(pr_mutation, "changed_paths", changed)
    monkeypatch.setattr(pr_mutation, "test_coverage",
                        lambda _: pytest.fail("Cannot infer lost coverage from edited test"))
    assert pr_mutation.select(BASE, HEAD)[0] == "full"


def test_shared_helper_change_triggers_full_run(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    helper = Path("tests/hosted_contract.py")
    helper.parent.mkdir()
    helper.touch()
    monkeypatch.setattr(pr_mutation, "changed_paths", lambda *a, **k: {str(helper)})
    assert pr_mutation.select(BASE, HEAD)[0] == "full"


def test_new_test_plus_changed_source_unions_both_modules(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source = Path(SRC + "notification_journal.py")
    source.parent.mkdir(parents=True)
    source.touch()
    test = Path("tests/test_notification.py")
    test.parent.mkdir()
    test.touch()
    def changed(base, head, kind="ACMRTD"):
        return set() if kind == "MRTD" else {str(source), str(test)}
    monkeypatch.setattr(pr_mutation, "changed_paths", changed)
    monkeypatch.setattr(pr_mutation, "test_coverage",
                        lambda files: {SRC + "storage.py"})
    assert pr_mutation.select(BASE, HEAD)[1] == [
        str(source), SRC + "storage.py",
    ]


def test_new_test_with_no_coverage_and_no_changed_source_falls_back_to_full(
    tmp_path, monkeypatch,
):
    monkeypatch.chdir(tmp_path)
    test = Path("tests/test_new.py")
    test.parent.mkdir()
    test.touch()
    monkeypatch.setattr(
        pr_mutation, "changed_paths",
        lambda base, head, kind="ACMRTD": set() if kind == "MRTD" else {str(test)},
    )
    monkeypatch.setattr(pr_mutation, "test_coverage", lambda _: set())
    assert pr_mutation.select(BASE, HEAD)[0] == "full"



def test_deleted_only_ci_contract_tests_do_not_force_full_mutation(monkeypatch):
    deleted = {"tests/test_mutation_cache_reuse.py", "tests/test_pr_mutation_rebuild_workflow.py"}
    monkeypatch.setattr(pr_mutation, "changed_paths", lambda *a, **kw: deleted)
    assert pr_mutation.select(BASE, HEAD)[0] == "skip"


def test_deleted_application_tests_still_force_full_mutation(monkeypatch):
    monkeypatch.setattr(pr_mutation, "changed_paths", lambda *a, **kw: {"tests/test_storage.py"})
    assert pr_mutation.select(BASE, HEAD)[0] == "full"



def test_coverage_json_keeps_fully_covered_modules_when_skip_covered_true(
    tmp_path, monkeypatch,
):
    """Coverage JSON includes 100%-covered modules even if text output skips them.

    Exercise the same coverage run/json commands as test_coverage(), so this
    does not merely mock the JSON response or the covered-file selector.
    """
    monkeypatch.chdir(tmp_path)
    Path("pyproject.toml").write_text(
        '[tool.pytest.ini_options]\n'
        'asyncio_mode = "auto"\n'
        'testpaths = ["tests"]\n'
        'pythonpath = ["."]\n'
        '[tool.coverage.run]\n'
        'source = ["custom_components.daylight_calendar_import"]\n'
        '[tool.coverage.report]\n'
        'skip_covered = true\n'
        'fail_under = 100\n',
        encoding="utf-8",
    )
    module = Path(SRC + "fully_covered.py")
    module.parent.mkdir(parents=True)
    module.write_text("VALUE = 7\n", encoding="utf-8")
    added_test = Path("tests/test_new_coverage.py")
    added_test.parent.mkdir()
    added_test.write_text(
        "from custom_components.daylight_calendar_import.fully_covered import VALUE\n"
        "def test_value(): assert VALUE == 7\n",
        encoding="utf-8",
    )

    assert pr_mutation.test_coverage([str(added_test)]) == {str(module)}
    # The JSON report is temporary, but the input setting was enabled.
    assert "skip_covered = true" in Path("pyproject.toml").read_text()
    assert not Path(".pr-mutation-coverage.json").exists()
