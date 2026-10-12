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
    assert mode == "scoped" and modules == [name]

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
    (tmp_path/"custom_components/__init__.py").touch()
    test=tmp_path/"tests/test_a.py"
    test.parent.mkdir()
    test.write_text(
        "import pytest\n"
        "from custom_components.daylight_calendar_import.called import called\n"
        "from custom_components.daylight_calendar_import.fixture_only import setup\n"
        "@pytest.fixture\n"
        "def fixture():\n    return setup()\n"
        "def test_a(fixture):\n    assert called()+fixture==9\n"
    )
    report=shadow.coverage((str(test),))
    assert SOURCE+"extra.py" in report["import_inclusive_modules"]
    assert SOURCE+"extra.py" not in report["execution_modules"]
    assert set(report["execution_modules"]) == {SOURCE+"called.py",SOURCE+"fixture_only.py"}
    assert "setup" in report["execution_phases"][SOURCE+"fixture_only.py"]

@pytest.mark.parametrize("number,changed,modified,expected", [
    (119,{SOURCE+"__init__.py",SOURCE+"storage.py","tests/test_parser.py"},{"tests/test_parser.py"},"needs-base-and-head-coverage"),
    (120,{SOURCE+"storage.py","tests/test_notification_delivery.py"},{"tests/test_notification_delivery.py"},"needs-base-and-head-coverage"),
    (121,{SOURCE+"__init__.py",SOURCE+"const.py",SOURCE+"storage.py","tests/test_targeted_dedupe_reset.py"},set(),"scoped"),
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
