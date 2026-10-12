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
    monkeypatch.setattr(impact, "shared_test_risk", lambda *args: None)
    monkeypatch.setattr(impact, "base_test_coverage",
                        lambda *args: {"execution_modules": [old], "collection_function_modules": []})
    monkeypatch.setattr(impact, "_run_probe",
                        lambda *args: {"execution_modules": [new], "collection_function_modules": [],
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
    monkeypatch.setattr(impact, "shared_test_risk", lambda *args: None)
    monkeypatch.setattr(impact, "_run_probe",
                        lambda *args: pytest.fail("Deleted test must not run on head"))
    monkeypatch.setattr(impact, "base_test_coverage",
                        lambda *args: {"execution_modules": [module], "collection_function_modules": []})
    result = impact.plan(BASE, HEAD)
    assert result["mode"] == "scoped"
    assert result["selected_modules"] == [module]


def test_failed_base_probe_must_escalate(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    test = "tests/test_existing.py"
    make(tmp_path, test)
    mock_changes(monkeypatch, [test], edited=[test])
    monkeypatch.setattr(impact, "shared_test_risk", lambda *args: None)
    monkeypatch.setattr(impact, "_run_probe",
                        lambda *args: {"execution_modules": [SRC + "storage.py"], "collection_function_modules": []})
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
                        lambda *args: {"execution_modules": [module], "collection_function_modules": [],
                                       "import_inclusive_modules": [SRC + "all.py"]})
    result = impact.plan(BASE, HEAD)
    assert result["mode"] == "scoped"
    assert result["selected_modules"] == [module]


def test_new_test_with_no_function_execution_escalates(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    test = "tests/test_new.py"
    make(tmp_path, test)
    mock_changes(monkeypatch, [test], added=[test])
    monkeypatch.setattr(impact, "_run_probe", lambda *args: {"execution_modules": [], "collection_function_modules": []})
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
        return {"execution_modules": [SRC + "storage.py"], "collection_function_modules": []}
    monkeypatch.setattr(impact, "_run_probe", fake_probe)
    report = impact.base_test_coverage(base, head, [test])
    assert report["execution_modules"] == [SRC + "storage.py"]
    assert "test_before" in seen[0][1]
    assert git("worktree", "list", "--porcelain").count("worktree ") == 1


def test_ci_only_test_contract_remains_synchronized():
    from scripts import pr_mutation
    assert shadow.CI_TESTS == pr_mutation.CI_TESTS


# Independent-review regressions: collection-time functions are not import-only
# lines; edited test modules that export fixtures may affect other test files.

def test_old_or_head_collection_function_cannot_claim_bounded_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    test = "tests/test_new.py"
    module = SRC + "called_at_collection.py"
    make(tmp_path, test)
    make(tmp_path, module)
    mock_changes(monkeypatch, [test], added=[test])
    monkeypatch.setattr(impact, "_run_probe", lambda *args: {
        "execution_modules": [module],
        "collection_function_modules": [module],
        "import_inclusive_modules": [module],
    })
    result = impact.plan(BASE, HEAD)
    assert result["mode"] == "comprehensive-sharded"
    assert not result["may_execute_mutmut"]
    assert module in result["head_collection_function_modules"]
    assert "collection" in result["reason"]


def test_old_revision_collection_function_is_checked(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    test = "tests/test_edited.py"
    module = SRC + "formerly_collection.py"
    make(tmp_path, test)
    make(tmp_path, module)
    mock_changes(monkeypatch, [test], edited=[test])
    monkeypatch.setattr(impact, "shared_test_risk", lambda *args: None)
    monkeypatch.setattr(impact, "_run_probe", lambda *args: {
        "execution_modules": [module], "collection_function_modules": [],
    })
    monkeypatch.setattr(impact, "base_test_coverage", lambda *args: {
        "execution_modules": [], "collection_function_modules": [module],
    })
    result = impact.plan(BASE, HEAD)
    assert result["mode"] == "comprehensive-sharded"
    assert result["base_collection_function_modules"] == [module]
    assert not result["may_execute_mutmut"]


def test_fixture_export_in_edited_test_is_fail_closed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()

    git("init", "-q")
    git("config", "user.name", "CI")
    git("config", "user.email", "ci@example.invalid")
    test = "tests/test_shared.py"
    consumer = "tests/test_consumer.py"
    shared = (
        "import pytest\n"
        "@pytest.fixture\n"
        "def borrowed():\n"
        "    return 1\n"
        "def test_own(): assert True\n"
    )
    make(tmp_path, test, shared)
    make(tmp_path, consumer,
         "from test_shared import borrowed\n"
         "def test_consumer(borrowed): assert borrowed == 1\n")
    git("add", ".")
    git("commit", "-qm", "baseline fixture")
    base = git("rev-parse", "HEAD")
    git("checkout", "-qb", "feature")
    make(tmp_path, test, shared.replace("return 1", "return 2"))
    git("add", ".")
    git("commit", "-qm", "edit shared fixture")
    head = git("rev-parse", "HEAD")
    def cannot_probe(*args, **kwargs):
        pytest.fail("Shared fixture must escalate before partial test execution")
    monkeypatch.setattr(impact, "_run_probe", cannot_probe)
    monkeypatch.setattr(impact, "base_test_coverage", cannot_probe)
    result = impact.plan(base, head)
    assert result["mode"] == "comprehensive-sharded"
    assert "Exportable fixture/helper" in result["reason"]
    assert not result["may_execute_mutmut"]


def test_removed_shared_fixture_from_base_is_detected(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()
    git("init", "-q")
    git("config", "user.name", "CI")
    git("config", "user.email", "ci@example.invalid")
    test = "tests/test_shared.py"
    make(tmp_path, test, "import pytest\n@pytest.fixture\ndef borrowed(): return 1\ndef test_own(): assert True\n")
    git("add", ".")
    git("commit", "-qm", "baseline fixture")
    base = git("rev-parse", "HEAD")
    git("checkout", "-qb", "feature")
    make(tmp_path, test, "import pytest\ndef test_own(): assert True\n")
    git("add", ".")
    git("commit", "-qm", "remove shared fixture")
    head = git("rev-parse", "HEAD")
    result = impact.plan(base, head)
    assert result["mode"] == "comprehensive-sharded"
    assert "Old revision:" in result["reason"]


def test_oversize_changed_modules_skip_both_coverage_probes(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    modules = [SRC + f"production_{i}.py" for i in range(5)]
    test = "tests/test_modified.py"
    for path in [*modules, test]:
        make(tmp_path, path)
    mock_changes(monkeypatch, [*modules, test], edited=[test])
    def cannot_probe(*args, **kwargs):
        pytest.fail("Already oversized scope must not launch coverage")
    monkeypatch.setattr(impact, "shared_test_risk", cannot_probe)
    monkeypatch.setattr(impact, "_run_probe", cannot_probe)
    monkeypatch.setattr(impact, "base_test_coverage", cannot_probe)
    result = impact.plan(BASE, HEAD, max_modules=4)
    assert result["mode"] == "comprehensive-sharded"
    assert "directly changed" in result["reason"]
    assert not result["may_execute_mutmut"]


def test_pure_test_function_is_not_assumed_exported_fixture():
    assert impact._shared_test_risk_in_source(
        "import pytest\n@pytest.mark.parametrize('n', [1, 2])\ndef test_item(n): assert n > 0\n",
        "tests/test_isolated.py",
    ) is None


@pytest.mark.parametrize("source", [
    "import pytest\n@pytest.fixture\ndef borrowed(): return 1\ndef test_a(): assert True\n",
    "def helper(): return 1\ndef test_a(): assert True\n",
    "pytest_plugins = ['some_plugin']\ndef test_a(): assert True\n",
    "class Helper:\n    pass\ndef test_a(): assert True\n",
])
def test_exported_test_behavior_requires_escalation(source):
    assert impact._shared_test_risk_in_source(source, "tests/test_shared.py")
