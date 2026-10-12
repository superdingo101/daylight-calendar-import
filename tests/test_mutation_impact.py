"""Contracts for the candidate bounded PR impact policy."""
import subprocess
from pathlib import Path
import pytest
from scripts import mutation_impact as impact
from scripts import mutation_shadow as shadow

SRC = shadow.SOURCE
BASE, HEAD = "a" * 40, "b" * 40


def make(tmp_path, name, body="def fn():\n    return 1\n"):
    file = tmp_path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(body)
    return file


def mock_changes(monkeypatch, files, added=(), edited=()):
    def changed(base, head, kind="ACMRTD"):
        assert (base, head) == (BASE, HEAD)
        return set(added) if kind == "A" else (
            set(edited) if kind == "MRTD" else set(files)
        )
    monkeypatch.setattr(shadow, "changed", changed)
    monkeypatch.setattr(impact, "merge_base", lambda *a: "c" * 40)


def test_small_source_scope_is_eligible_but_not_quality_certified(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    module = SRC + "storage.py"
    make(tmp_path, module)
    mock_changes(monkeypatch, [module])
    result = impact.plan(BASE, HEAD, measure=False)
    assert result["mode"] == "scoped"
    assert result["selected_modules"] == [module]
    assert result["may_execute_mutmut"]
    assert not result["same_scope_baseline_verified"]
    assert not result["comprehensive_sharded_available"]


def test_five_modules_escalate_instead_of_full_single_runner(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    modules = [SRC + f"m{i}.py" for i in range(5)]
    for module in modules:
        make(tmp_path, module)
    mock_changes(monkeypatch, modules)
    result = impact.plan(BASE, HEAD, max_modules=4, measure=False)
    assert result["mode"] == "comprehensive-sharded"
    assert not result["may_execute_mutmut"]
    assert "budget" in result["reason"]


def test_modified_test_unions_old_and_new_coverage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    modified = SRC + "storage.py"
    old = SRC + "dedup.py"
    new = SRC + "settings.py"
    test = "tests/test_storage.py"
    for name in (modified, old, new, test):
        make(tmp_path, name)
    mock_changes(monkeypatch, [modified, test], edited=[test])
    monkeypatch.setattr(impact, "base_test_coverage",
                        lambda *args: {"execution_modules": [old]})
    monkeypatch.setattr(impact, "_run_probe",
                        lambda *args: {"execution_modules": [new],
                                       "import_inclusive_modules": [SRC + "irrelevant.py"]})
    result = impact.plan(BASE, HEAD)
    assert result["mode"] == "scoped"
    assert result["selected_modules"] == sorted([old, new, modified])
    assert result["base_execution_modules"] == [old]
    assert result["head_execution_modules"] == [new]


def test_deleted_test_uses_old_execution_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    missing_test = "tests/test_removed.py"
    module = SRC + "storage.py"
    make(tmp_path, module)
    mock_changes(monkeypatch, [missing_test], edited=[missing_test])
    monkeypatch.setattr(impact, "_run_probe",
                        lambda *args: pytest.fail("Deleted test must not run on head"))
    monkeypatch.setattr(impact, "base_test_coverage",
                        lambda *args: {"execution_modules": [module]})
    result = impact.plan(BASE, HEAD)
    assert result["mode"] == "scoped"
    assert result["selected_modules"] == [module]


def test_failed_base_probe_must_escalate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    test = "tests/test_existing.py"
    make(tmp_path, test)
    mock_changes(monkeypatch, [test], edited=[test])
    monkeypatch.setattr(impact, "_run_probe",
                        lambda *args: {"execution_modules": [SRC + "storage.py"]})
    def broken(*args):
        raise subprocess.CalledProcessError(1, ["pytest", test])
    monkeypatch.setattr(impact, "base_test_coverage", broken)
    result = impact.plan(BASE, HEAD)
    assert result["mode"] == "comprehensive-sharded"
    assert not result["may_execute_mutmut"]
    assert "CalledProcessError" in result["reason"]


def test_new_test_selects_executed_function_not_imports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    test, module = "tests/test_new.py", SRC + "dedup.py"
    make(tmp_path, test)
    make(tmp_path, module)
    mock_changes(monkeypatch, [test], added=[test])
    monkeypatch.setattr(impact, "_run_probe",
                        lambda *args: {"execution_modules": [module],
                                       "import_inclusive_modules": [SRC + "all.py"]})
    result = impact.plan(BASE, HEAD)
    assert result["mode"] == "scoped"
    assert result["selected_modules"] == [module]


def test_new_test_with_no_function_execution_escalates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    test = "tests/test_new.py"
    make(tmp_path, test)
    mock_changes(monkeypatch, [test], added=[test])
    monkeypatch.setattr(impact, "_run_probe", lambda *args: {"execution_modules": []})
    result = impact.plan(BASE, HEAD)
    assert result["mode"] == "comprehensive-sharded"
    assert not result["may_execute_mutmut"]


@pytest.mark.parametrize("path", [SRC + "services.yaml", SRC + "removed.py",
                                  "tests/conftest.py"])
def test_unknown_change_never_triggers_unsharded_full(tmp_path, monkeypatch, path):
    monkeypatch.chdir(tmp_path)
    mock_changes(monkeypatch, [path])
    result = impact.plan(BASE, HEAD)
    assert result["mode"] == "comprehensive-sharded"
    assert not result["may_execute_mutmut"]


def test_unmeasured_test_impact_is_not_eligible(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    test = "tests/test_new.py"
    make(tmp_path, test)
    mock_changes(monkeypatch, [test], added=[test])
    result = impact.plan(BASE, HEAD, measure=False)
    assert result["mode"] == "coverage-required"
    assert not result["may_execute_mutmut"]


def test_invalid_budget_rejected():
    with pytest.raises(ValueError):
        impact.plan(BASE, HEAD, max_modules=0)
    with pytest.raises(ValueError):
        impact.plan(BASE, HEAD, budget=0)


def test_base_worktree_measures_previous_version_and_is_cleaned(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()
    git("init", "-q")
    git("config", "user.name", "CI")
    git("config", "user.email", "ci@example.invalid")
    test = "tests/test_history.py"
    make(tmp_path, test, "def test_before(): assert True\n")
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")
    git("checkout", "-qb", "feature")
    make(tmp_path, test, "def test_after(): assert True\n")
    git("add", ".")
    git("commit", "-qm", "edit test")
    head = git("rev-parse", "HEAD")
    seen = []
    def fake_probe(files, *, cwd=None):
        seen.append((files, (cwd / test).read_text()))
        return {"execution_modules": [SRC + "storage.py"]}
    monkeypatch.setattr(impact, "_run_probe", fake_probe)
    report = impact.base_test_coverage(base, head, [test])
    assert report["execution_modules"] == [SRC + "storage.py"]
    assert "test_before" in seen[0][1]
    assert git("worktree", "list", "--porcelain").count("worktree ") == 1


def test_ci_only_test_contract_remains_synchronized():
    from scripts import pr_mutation
    assert shadow.CI_TESTS == pr_mutation.CI_TESTS
