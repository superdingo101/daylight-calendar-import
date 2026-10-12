"""Safety contracts for informational mutation planning."""
from pathlib import Path
import subprocess
import pytest
from scripts import mutation_shadow as shadow

SOURCE = shadow.SOURCE

def make(tmp_path, *names):
    for name in names:
        file = tmp_path / name
        file.parent.mkdir(parents=True, exist_ok=True)
        file.write_text("def work():\n    return 1\n")

def test_mixed_source_and_test_does_not_import_expand(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name = SOURCE + "storage.py"
    test = "tests/test_added.py"
    make(tmp_path, name, test)
    mode, modules, _, _ = shadow.classify({name,test},set(),{test})
    assert mode == "coverage-required" and modules == [name]

def test_edited_and_deleted_tests_never_skip(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    name="tests/test_storage.py"
    make(tmp_path,name)
    assert shadow.classify({name},{name},set())[0] == "needs-base-and-head-coverage"
    Path(name).unlink()
    assert shadow.classify({name},{name},set())[0] == "needs-base-and-head-coverage"

@pytest.mark.parametrize("name",["tests/conftest.py","requirements_test.txt","pyproject.toml","tests/fixtures/data.json"])
def test_shared_changes_escalate(name):
    assert shadow.classify({name},set(),set())[0] == "comprehensive-sharded"

def test_ci_only_skip():
    assert shadow.classify({"README.md","tests/test_mutation_shadow.py"},set(),set())[0] == "skip"

def test_new_test_requires_coverage(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    make(tmp_path,"tests/test_added.py")
    assert shadow.classify({"tests/test_added.py"},set(),{"tests/test_added.py"})[0] == "coverage-required"

def test_fixture_coverage_and_imports(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    package=tmp_path/SOURCE
    package.mkdir(parents=True)
    (package/"__init__.py").write_text("from . import extra\n")
    (package/"extra.py").write_text("VALUE=1\n")
    (package/"called.py").write_text("def called():\n    return 7\n")
    (package/"fixture_only.py").write_text("def setup():\n    return 2\n")
    (package/"inline.py").write_text("def single(): return 5\n")
    (package/"collection_call.py").write_text("def collected(): return 3\n")
    (tmp_path/"custom_components/__init__.py").touch()
    test=tmp_path/"tests/test_a.py"
    test.parent.mkdir()
    test.write_text(
        "import pytest\n"
        "from custom_components.daylight_calendar_import.called import called\n"
        "from custom_components.daylight_calendar_import.fixture_only import setup\n"
        "from custom_components.daylight_calendar_import.inline import single\n"
        "from custom_components.daylight_calendar_import.collection_call import collected\n"
        "assert collected() == 3\n"
        "@pytest.fixture\n"
        "def fixture():\n    return setup()\n"
        "def test_a(fixture):\n    assert called()+fixture==9\n    assert single()==5\n"
    )
    # The synthetic package must not inherit unrelated HA pytest fixtures.
    monkeypatch.setenv("PYTEST_DISABLE_PLUGIN_AUTOLOAD", "1")
    monkeypatch.setenv("PYTEST_ADDOPTS", "-p pytest_cov.plugin")
    report=shadow.coverage((str(test),))
    assert SOURCE+"extra.py" in report["import_inclusive_modules"]
    assert SOURCE+"extra.py" not in report["execution_modules"]
    assert set(report["execution_modules"]) == {SOURCE+"called.py",SOURCE+"fixture_only.py",SOURCE+"inline.py"}
    assert "setup" in report["execution_phases"][SOURCE+"fixture_only.py"]
    assert "run" in report["execution_phases"][SOURCE+"inline.py"]
    # Import-only module initialization must stay excluded, but real
    # collection-time calls into production functions must be surfaced.
    assert report["collection_function_modules"] == [SOURCE+"collection_call.py"]
    assert SOURCE+"collection_call.py" not in report["execution_modules"]

@pytest.mark.parametrize("number,changed,modified,expected", [
    (119,{SOURCE+"__init__.py",SOURCE+"storage.py","tests/test_parser.py"},{"tests/test_parser.py"},"needs-base-and-head-coverage"),
    (120,{SOURCE+"storage.py","tests/test_notification_delivery.py"},{"tests/test_notification_delivery.py"},"needs-base-and-head-coverage"),
    (121,{SOURCE+"__init__.py",SOURCE+"const.py",SOURCE+"storage.py","tests/test_targeted_dedupe_reset.py"},set(),"coverage-required"),
    (123,{SOURCE+"storage.py","tests/test_init.py"},{"tests/test_init.py"},"needs-base-and-head-coverage"),
])
def test_historical_regression(number,changed,modified,expected,tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    make(tmp_path,*changed)
    assert shadow.classify(changed,modified,changed-modified)[0] == expected

def test_stacked_base_changes_do_not_pollute_scope(tmp_path,monkeypatch):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.check_output(["git",*args],text=True).strip()
    git("init","-q")
    git("config","user.name","CI")
    git("config","user.email","ci@example.invalid")
    file=SOURCE+"storage.py"
    make(tmp_path,file)
    git("add",".")
    git("commit","-qm","original")
    base=git("rev-parse","HEAD")
    git("checkout","-qb","feature")
    Path(file).write_text("def work():\n    return 2\n")
    git("add",".")
    git("commit","-qm","feature")
    feature=git("rev-parse","HEAD")
    git("checkout","-qb","target",base)
    make(tmp_path,"tests/test_base_only.py")
    git("add",".")
    git("commit","-qm","target advance")
    target=git("rev-parse","HEAD")
    assert shadow.changed(target,feature) == {file}

# Review regressions: fail-safe deletion and runtime-resource handling.

@pytest.mark.parametrize("changed", [
    SOURCE + "missing.py",
    SOURCE + "renamed_away.py",
])
def test_deleted_production_module_escalates_instead_of_skipping(changed, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    mode, modules, reason, _ = shadow.classify({changed}, set(), set())
    assert mode == "comprehensive-sharded"
    assert modules == []
    assert "Deleted/renamed" in reason


def test_git_delete_only_and_rename_production_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    def git(*args):
        return subprocess.check_output(["git", *args], text=True).strip()
    git("init", "-q")
    git("config", "user.name", "CI")
    git("config", "user.email", "ci@example.invalid")
    file = SOURCE + "old.py"
    make(tmp_path, file)
    git("add", ".")
    git("commit", "-qm", "base")
    base = git("rev-parse", "HEAD")

    git("mv", file, SOURCE + "new.py")
    git("commit", "-qm", "rename")
    renamed = git("rev-parse", "HEAD")
    plan = shadow.plan(base, renamed, measure=False)
    assert plan["mode"] == "comprehensive-sharded"
    assert file in plan["changed_files"]

    git("rm", SOURCE + "new.py")
    git("commit", "-qm", "delete")
    deleted = git("rev-parse", "HEAD")
    plan = shadow.plan(renamed, deleted, measure=False)
    assert plan["mode"] == "comprehensive-sharded"
    assert SOURCE + "new.py" in plan["changed_files"]


@pytest.mark.parametrize("asset", [
    SOURCE + "services.yaml",
    SOURCE + "strings.json",
    SOURCE + "translations/en.json",
    SOURCE + "some_config.yaml",
])
def test_runtime_asset_only_is_not_a_mutation_skip(asset):
    assert shadow.classify({asset}, set(), set())[0] == "comprehensive-sharded"


def test_frontend_and_docs_are_mutation_exempt():
    assert shadow.classify({SOURCE + "frontend/index.js", "README.md"}, set(), set())[0] == "skip"


def test_ci_only_exclusions_match_current_required_gate():
    from scripts import pr_mutation
    assert shadow.CI_TESTS == pr_mutation.CI_TESTS


def test_mixed_changes_union_new_test_execution_and_changed_source(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    production = SOURCE + "storage.py"
    added_test = "tests/test_new_mutation.py"
    make(tmp_path, production, added_test)
    source_hit = SOURCE + "dedup.py"
    def changed(_base, _head, kind="ACMRTD"):
        return {production, added_test} if kind == "ACMRTD" else (
            {added_test} if kind == "A" else set()
        )
    monkeypatch.setattr(shadow, "changed", changed)
    monkeypatch.setattr(shadow, "coverage", lambda tests: {
        "import_inclusive_modules": [SOURCE + "storage.py", source_hit, SOURCE + "pdfs.py"],
        "execution_modules": [source_hit],
        "execution_phases": {source_hit: ["run"]},
        "duration_seconds": 0.01,
    })
    result = shadow.plan("a" * 40, "b" * 40)
    assert result["mode"] == "scoped"
    assert result["source_modules"] == [source_hit, production]
    assert SOURCE + "pdfs.py" not in result["source_modules"]


def test_added_tests_without_verified_execution_cannot_silently_narrow(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    production = SOURCE + "storage.py"
    added_test = "tests/test_new_mutation.py"
    make(tmp_path, production, added_test)
    def changed(_base, _head, kind="ACMRTD"):
        return {production, added_test} if kind == "ACMRTD" else (
            {added_test} if kind == "A" else set()
        )
    monkeypatch.setattr(shadow, "changed", changed)
    monkeypatch.setattr(shadow, "coverage", lambda tests: {
        "import_inclusive_modules": [production],
        "execution_modules": [],
        "execution_phases": {},
        "duration_seconds": 0.01,
    })
    assert shadow.plan("a" * 40, "b" * 40)["mode"] == "comprehensive-sharded"


def test_unmeasured_preview_does_not_claim_a_final_mixed_scope(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    production = SOURCE + "storage.py"
    added_test = "tests/test_new_mutation.py"
    make(tmp_path, production, added_test)
    def changed(_base, _head, kind="ACMRTD"):
        return {production, added_test} if kind == "ACMRTD" else (
            {added_test} if kind == "A" else set()
        )
    monkeypatch.setattr(shadow, "changed", changed)
    preview = shadow.plan("a" * 40, "b" * 40, measure=False)
    assert preview["mode"] == "coverage-required"
    assert preview["source_modules"] == [production]


def test_collection_function_detector_ignores_module_level_import_assignments(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source_file = SOURCE + "imported.py"
    make(tmp_path, source_file)
    report = {
        "files": {
            source_file: {
                "contexts": {"1": [""], "2": [""]},
                "executed_lines": [1, 2],
            }
        }
    }
    assert shadow.collection_function_modules(report) == []


def test_shadow_preview_is_explicitly_not_the_authoritative_plan(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(shadow, "changed", lambda *args, **kwargs: {"README.md"})
    preliminary = shadow.plan("a" * 40, "b" * 40, measure=False)
    assert preliminary["report_role"] == "preliminary"
    assert preliminary["authoritative_plan"] == "mutation-impact-plan.json"
    final = shadow.plan("a" * 40, "b" * 40, measure=True)
    assert final["report_role"] == "final"
    assert final["authoritative_plan"] is None
