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
    assert calls[0][-3:] == [BASE, HEAD, "--"]


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
    assert pr_mutation.select(BASE, HEAD) == ("scoped", [name], "Changed or test-covered production Python modules")


def test_changed_test_maps_coverage(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    t = Path("tests/test_journal.py")
    t.parent.mkdir()
    t.touch()
    def changed(base, head, kind="ACMRTD"):
        return set() if kind == "D" else {str(t)}
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


def test_deleted_only_ci_contract_tests_do_not_force_full_mutation(monkeypatch):
    deleted = {"tests/test_mutation_cache_reuse.py", "tests/test_pr_mutation_rebuild_workflow.py"}
    monkeypatch.setattr(pr_mutation, "changed_paths", lambda *a, **kw: deleted)
    assert pr_mutation.select(BASE, HEAD)[0] == "skip"


def test_deleted_application_tests_still_force_full_mutation(monkeypatch):
    monkeypatch.setattr(pr_mutation, "changed_paths", lambda *a, **kw: {"tests/test_storage.py"})
    assert pr_mutation.select(BASE, HEAD)[0] == "full"
