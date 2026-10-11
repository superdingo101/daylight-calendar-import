"""Regression tests for fail-closed reuse of PR-local mutmut state."""
from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import pr_mutation_cache as cache


def test_appended_test_functions_are_additive_only(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    tests = Path("tests")
    tests.mkdir()
    file = tests / "test_app.py"
    before = "import pytest\n\ndef test_existing():\n    assert True\n"
    file.write_text(before)
    old = cache.hash_inputs([file])[1]
    file.write_text(before + "\nasync def test_new():\n    assert True\n")
    new = cache.hash_inputs([file])[1]
    assert cache.additive(old, new)
    file.write_text(before + "\n@pytest.mark.parametrize('a', [1])\ndef test_new(a):\n    assert a\n")
    assert not cache.additive(old, cache.hash_inputs([file])[1])


@pytest.mark.parametrize("change", [
    "import pytest\n\ndef test_existing():\n    assert False\n",
    "import pytest\n\nx = 1\ndef test_existing():\n    assert True\n",
    "import pytest\n\ndef test_existing():\n    assert True\n\n@pytest.fixture\ndef fixture():\n    return 1\n",
])
def test_changes_to_existing_tests_or_fixtures_are_not_additive(tmp_path, monkeypatch, change):
    monkeypatch.chdir(tmp_path)
    path = Path("tests/test_app.py")
    path.parent.mkdir()
    path.write_text("import pytest\n\ndef test_existing():\n    assert True\n")
    old = cache.hash_inputs([path])[1]
    path.write_text(change)
    assert not cache.additive(old, cache.hash_inputs([path])[1])



@pytest.mark.parametrize("addition", [
    "\ndef test_existing():\n    assert True\n",
    "\ndef test_new(value=side_effect()):\n    assert value\n",
    "\ndef test_new(*, value=side_effect()):\n    assert value\n",
    "\ndef test_new(value: side_effect()):\n    assert value\n",
])
def test_appended_tests_with_collection_side_effects_or_shadowing_are_not_additive(
    tmp_path, monkeypatch, addition,
):
    monkeypatch.chdir(tmp_path)
    path = Path("tests/test_app.py")
    path.parent.mkdir()
    original = "def test_existing():\n    assert True\n"
    path.write_text(original)
    previous = cache.hash_inputs([path])[1]
    path.write_text(original + addition)
    assert not cache.additive(previous, cache.hash_inputs([path])[1])


def test_added_or_deleted_test_files_cannot_keep_old_verdicts():
    assert not cache.additive({"tests/test_one.py": []}, {
        "tests/test_one.py": [], "tests/test_two.py": [],
    })
    assert not cache.additive({"tests/test_one.py": [], "tests/test_two.py": []}, {
        "tests/test_one.py": [],
    })


def make_context(tests=None, **changes):
    return {
        "schema": cache.VERSION, "mutmut": cache.MUTMUT_VERSION,
        "pr": 113, "scope": {"mode": "full", "modules": []},
        "inputs_sha256": "prod", "environment_sha256": "deps",
        "tests": tests if tests is not None else {"tests/test_app.py": []},
        **changes,
    }


def setup_state(tmp_path, monkeypatch, old, new):
    monkeypatch.chdir(tmp_path)
    t = Path("tests/test_app.py")
    t.parent.mkdir()
    t.write_text("def test_existing():\n    assert True\n\ndef test_added():\n    assert True\n")
    cache.ROOT.mkdir()
    (cache.ROOT / "tests").mkdir()
    (cache.ROOT / "tests/test_app.py").write_text("STALE OLD TEST CONTENT")
    (cache.ROOT / "mutmut-stats.json").write_text("{}")
    meta = cache.ROOT / "example.py.meta"
    meta.write_text(json.dumps({
        "exit_code_by_key": {
            "killed": 1, "killed_internal": 3, "type_check": 37,
            "survived": 0, "no_tests": 33, "timeout": 36, "pending": None,
        },
        "hash_by_function_name": {},
    }))
    cache.PROVENANCE.write_text(json.dumps(old))
    monkeypatch.setattr(cache, "context", lambda pr: new)
    return meta


def test_additive_tests_retest_all_non_killed_and_refresh_copied_tests(tmp_path, monkeypatch):
    old = make_context({"tests/test_app.py": ["old"]})
    new = make_context({"tests/test_app.py": ["old", "new"]})
    meta = setup_state(tmp_path, monkeypatch, old, new)
    monkeypatch.setattr(cache, "additive", lambda a, b: True)
    cache.restore("113")
    values = json.loads(meta.read_text())["exit_code_by_key"]
    assert values == {
        "killed": 1, "killed_internal": 3, "type_check": 37,
        "survived": None, "no_tests": None, "timeout": None, "pending": None,
    }
    assert (cache.ROOT / "tests/test_app.py").read_text() == Path("tests/test_app.py").read_text()
    assert not cache.PROVENANCE.exists()


def test_identical_tests_keep_prior_results(tmp_path, monkeypatch):
    same = make_context()
    meta = setup_state(tmp_path, monkeypatch, same, same)
    cache.restore("113")
    assert json.loads(meta.read_text())["exit_code_by_key"]["survived"] == 0
    assert not cache.PROVENANCE.exists()


@pytest.mark.parametrize("new", [
    make_context(inputs_sha256="changed-source"),
    make_context(environment_sha256="changed-dependency"),
    make_context(scope={"mode": "scoped", "modules": ["app.py"]}),
    make_context(pr=114),
    make_context(tests={"tests/test_app.py": ["changed-existing-function"]}),
])
def test_incompatible_provenance_never_reuses_mutant_verdicts(tmp_path, monkeypatch, new):
    # The previous test body must exist for a replacement to be non-additive.
    old = make_context(tests={"tests/test_app.py": ["previous-existing-test"]})
    setup_state(tmp_path, monkeypatch, old, new)
    cache.restore("113")
    assert not cache.ROOT.exists()


def test_cache_stamp_requires_complete_mutmut_state(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(cache, "context", lambda pr: make_context())
    cache.ROOT.mkdir()
    with pytest.raises(ValueError, match="incomplete"):
        cache.stamp("113")
    (cache.ROOT / "mutmut-stats.json").write_text("{}")
    with pytest.raises(ValueError, match="metadata"):
        cache.stamp("113")
    (cache.ROOT / "component.py.meta").write_text("{}")
    cache.stamp("113")
    assert json.loads(cache.PROVENANCE.read_text())["pr"] == 113


def test_production_hash_changes_even_when_file_size_is_constant(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src = Path("custom_components/example.py")
    src.parent.mkdir()
    src.write_text("a = 1\n")
    original = cache.hash_inputs([src])[0]
    src.write_text("a = 2\n")
    assert cache.hash_inputs([src])[0] != original



@pytest.mark.parametrize("existing", [
    "from helpers import test_shadow\n",
    "from helpers import some_test as test_shadow\n",
    "import helpers as test_shadow\n",
    "test_shadow = lambda: True\n",
    "test_shadow: object = lambda: True\n",
    "for test_shadow in [lambda: True]:\n    pass\n",
    "if True:\n    from helpers import test_shadow\n",
    "try:\n    from helpers import test_shadow\nexcept ImportError:\n    pass\n",
])
def test_appended_function_cannot_shadow_previous_module_binding(
    tmp_path, monkeypatch, existing,
):
    monkeypatch.chdir(tmp_path)
    path = Path("tests/test_app.py")
    path.parent.mkdir()
    path.write_text(existing)
    before = cache.hash_inputs([path])[1]
    path.write_text(existing + "\ndef test_shadow():\n    assert True\n")
    assert not cache.additive(before, cache.hash_inputs([path])[1])


def test_appended_function_cannot_follow_wildcard_import(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = Path("tests/test_app.py")
    path.parent.mkdir()
    path.write_text("from helpers import *\n")
    before = cache.hash_inputs([path])[1]
    path.write_text("from helpers import *\n\ndef test_new():\n    assert True\n")
    assert not cache.additive(before, cache.hash_inputs([path])[1])


def test_unrelated_named_import_does_not_block_appended_test(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    path = Path("tests/test_app.py")
    path.parent.mkdir()
    path.write_text("import pytest\nfrom helpers import test_other\n")
    before = cache.hash_inputs([path])[1]
    path.write_text("import pytest\nfrom helpers import test_other\n\ndef test_new():\n    assert True\n")
    assert cache.additive(before, cache.hash_inputs([path])[1])



@pytest.mark.parametrize("existing", [
    'globals()["test_shadow"] = lambda: True\n',
    'globals().update(test_shadow=lambda: True)\n',
    'vars()["test_shadow"] = lambda: True\n',
    'locals()["test_shadow"] = lambda: True\n',
    'setattr(sys.modules[__name__], "test_shadow", lambda: True)\n',
    'exec("test_shadow = lambda: True")\n',
    'eval("globals()")["test_shadow"] = lambda: True\n',
    'import builtins\nbuiltins.globals()["test_shadow"] = lambda: True\n',
    'from builtins import globals as namespace\nnamespace()["test_shadow"] = lambda: True\n',
    'sys.modules[__name__].__dict__["test_shadow"] = lambda: True\n',
    'getattr(sys.modules[__name__], "__dict__")["test_shadow"] = lambda: True\n',
])
def test_appended_function_cannot_shadow_dynamic_module_binding(
    tmp_path, monkeypatch, existing,
):
    """Unknown dynamic exports force cold instead of retaining old kills."""
    monkeypatch.chdir(tmp_path)
    file = Path("tests/test_dynamic.py")
    file.parent.mkdir()
    file.write_text(existing)
    before = cache.hash_inputs([file])[1]
    file.write_text(existing + "\ndef test_shadow():\n    assert True\n")
    assert not cache.additive(before, cache.hash_inputs([file])[1])


def test_new_test_in_a_different_file_does_not_reuse_dynamic_binder(
    tmp_path, monkeypatch,
):
    """A dynamic binder is conservative only in the test module being extended."""
    monkeypatch.chdir(tmp_path)
    first = Path("tests/test_dynamic.py")
    first.parent.mkdir()
    first.write_text('globals()["test_shadow"] = lambda: True\n')
    second = Path("tests/test_simple.py")
    second.write_text("def test_original():\n    assert True\n")
    before = cache.hash_inputs([first, second])[1]
    second.write_text(second.read_text() + "\ndef test_added():\n    assert True\n")
    assert cache.additive(before, cache.hash_inputs([first, second])[1])



def test_isolated_new_test_file_is_additive_and_retests_survivors(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    initial = Path("tests/test_existing.py")
    initial.parent.mkdir()
    initial.write_text("def test_original():\n    assert True\n")
    previous_tests = cache.hash_inputs([initial])[1]
    extra = Path("tests/test_more.py")
    extra.write_text(
        '"""Purely additive test module."""\n'
        'from datetime import UTC\n'
        'import pytest\n'
        'from test_existing import test_original\n'
        'from custom_components.daylight_calendar_import.models import EventDraft\n'
        '\n'
        'def test_extra():\n'
        '    assert test_original() is None\n'
    )
    latest_tests = cache.hash_inputs([initial, extra])[1]
    assert cache.additive(previous_tests, latest_tests)

    old = make_context(tests=previous_tests)
    new = make_context(tests=latest_tests)
    metadata = setup_state(tmp_path, monkeypatch, old, new)
    # setup_state creates its own test_app.py but the provenance and saved state
    # are intentionally those of the two test modules created above.
    cache.restore("113")
    verdicts = json.loads(metadata.read_text())["exit_code_by_key"]
    assert verdicts["killed"] == 1
    assert verdicts["killed_internal"] == 3
    assert verdicts["type_check"] == 37
    assert verdicts["survived"] is None
    assert verdicts["no_tests"] is None
    assert (cache.ROOT / "tests/test_more.py").exists()
    assert not cache.PROVENANCE.exists()


@pytest.mark.parametrize("body", [
    'import pytest\n@pytest.fixture\ndef shared():\n    return 1\n',
    'from pytest import fixture\n@fixture\ndef shared():\n    return 1\n',
    'import pytest\n@pytest.mark.parametrize("x", [1])\ndef test_new(x):\n    assert x\n',
    'import pytest\npytest_plugins = ["new_plugin"]\ndef test_new():\n    assert True\n',
    'import pytest\nregister_global_hooks()\ndef test_new():\n    assert True\n',
    'import external_new_module\ndef test_new():\n    assert True\n',
    'from arbitrary_source import *\ndef test_new():\n    assert True\n',
    'from test_new_helper import value\ndef test_new():\n    assert value\n',
    'def test_new(a=side_effect()):\n    assert a\n',
    'def test_new(a: some_effect()):\n    assert a\n',
    'class TestGlobal:\n    def test_nested(self):\n        assert True\n',
    'async def test_new():\n    assert True\n\ndef not_a_test():\n    pass\n',
])
def test_new_test_module_collection_side_effects_force_cold(
    tmp_path, monkeypatch, body,
):
    monkeypatch.chdir(tmp_path)
    original = Path("tests/test_old.py")
    original.parent.mkdir()
    original.write_text("def test_old(): assert True\n")
    old = cache.hash_inputs([original])[1]
    newfile = Path("tests/test_added.py")
    newfile.write_text(body)
    assert not cache.additive(old, cache.hash_inputs([original, newfile])[1])


def test_deleting_existing_test_file_remains_incompatible(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = Path("tests/test_old.py")
    original.parent.mkdir()
    original.write_text("def test_old(): assert True\n")
    old = cache.hash_inputs([original])[1]
    assert not cache.additive(old, {})


def test_new_test_helper_file_is_not_safe_addition(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = Path("tests/test_old.py")
    original.parent.mkdir()
    original.write_text("def test_old(): assert True\n")
    old = cache.hash_inputs([original])[1]
    helper = Path("tests/helper.py")
    helper.write_text("def test_new(): assert True\n")
    assert not cache.additive(old, cache.hash_inputs([original, helper])[1])


def test_new_test_file_must_not_shadow_imports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    original = Path("tests/test_old.py")
    original.parent.mkdir()
    original.write_text("def test_old(): assert True\n")
    old = cache.hash_inputs([original])[1]
    newfile = Path("tests/test_added.py")
    newfile.write_text("from datetime import UTC\n\ndef test_new(): assert True\n")
    assert cache.additive(old, cache.hash_inputs([original, newfile])[1])
    newfile.write_text("from datetime import test_new\n\ndef test_new(): assert True\n")
    assert not cache.additive(old, cache.hash_inputs([original, newfile])[1])
