"""Regression tests for reusing clean mutmut baselines across ordinary PRs."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import mutation_cache


def _git(*args: str) -> str:
    return subprocess.check_output(["git", *args], text=True).strip()


def _write(path: str, content: str) -> None:
    file = Path(path)
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_text(content, encoding="utf-8")


def _baseline(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    _git("init")
    _git("config", "user.email", "cache@example.invalid")
    _git("config", "user.name", "Cache Test")
    initial = {
        "custom_components/daylight_calendar_import/old.py":
            "LIMIT = 3\ndef old():\n    return LIMIT\n",
        "custom_components/daylight_calendar_import/frontend/panel.js":
            "console.log('old')\n",
        "tests/test_old.py": "def test_old():\n    assert True\n",
        "tests/conftest.py": "# shared fixtures\n",
        "tests/fixtures/data.json": '{"state":"old"}\n',
        "tests/test_mutation_ci_strategy.py": "# CI-only test helper\n",
        "scripts/mutation_cache.py": "# original cache helper\n",
        "requirements_test.txt": "pytest\n",
        ".github/workflows/mutation.yml": (
            "name: Incremental mutation testing\n"
            "jobs:\n  mutation:\n    steps:\n"
            "      - uses: actions/checkout@v5\n"
            "      - name: Restore mutation cache\n"
            "        uses: actions/cache/restore@v4\n"
            "      - name: Run incremental mutations\n"
            "        run: mutmut run\n"
        ),
    }
    for path, text in initial.items():
        _write(path, text)
    _git("add", ".")
    _git("commit", "-m", "clean baseline")
    baseline = _git("rev-parse", "HEAD")

    _write("mutants/custom_components/daylight_calendar_import/old.py.meta", "{}")
    _write("mutants/mutmut-stats.json", json.dumps({"git_commit": baseline}))
    _write(
        "mutants/mutation-cache-provenance.json",
        json.dumps({
            "schema": mutation_cache.CACHE_SCHEMA,
            "fingerprint": mutation_cache.fingerprint(),
            # Legacy baseline: no source_commit yet.
        }),
    )
    assert mutation_cache.validate()
    return initial, baseline


def test_reuses_legacy_main_baseline_for_new_feature_modules_and_tests(monkeypatch, tmp_path):
    initial, baseline = _baseline(monkeypatch, tmp_path)
    _write(
        "custom_components/daylight_calendar_import/old.py",
        "LIMIT = 3\ndef old():\n    return LIMIT + 1\n",
    )
    _write(
        "custom_components/daylight_calendar_import/notifications.py",
        "def notify():\n    return True\n",
    )
    _write("tests/test_notifications.py", "def test_notification():\n    assert True\n")
    _write("custom_components/daylight_calendar_import/frontend/panel.js", "console.log('new')\n")
    _write("scripts/mutation_cache.py", "# updated cache validator\n")
    _write("tests/test_mutation_ci_strategy.py", "# updated cache tests\n")
    # Cache state under mutants/ is intentionally untracked in the real
    # repository; do not stage that temporary state in this fixture.
    _git("add", "custom_components/daylight_calendar_import/notifications.py",
         "tests/test_notifications.py")
    assert mutation_cache.validate()
    assert mutation_cache.fingerprint() != mutation_cache.fingerprint(baseline)

    # Cached results remain reusable after stamping a successfully tested PR.
    source_commit = _git("rev-parse", "HEAD")
    mutation_cache.CACHE_META.write_text(json.dumps({
        "schema": mutation_cache.CACHE_SCHEMA,
        "fingerprint": mutation_cache.fingerprint(),
        "source_commit": source_commit,
    }))
    assert mutation_cache.validate()


@pytest.mark.parametrize("path,content", [
    ("tests/test_old.py", "def test_old():\n    assert False\n"),
    ("tests/conftest.py", "# changed autouse fixtures\n"),
    ("tests/fixtures/data.json", '{"state":"changed"}\n'),
    ("requirements_test.txt", "pytest==9.0\n"),
    ("custom_components/daylight_calendar_import/old.py",
     "LIMIT = 4\ndef old():\n    return LIMIT\n"),
])
def test_invalidates_existing_tests_fixtures_dependencies_and_module_state(
    monkeypatch, tmp_path, path, content,
):
    _baseline(monkeypatch, tmp_path)
    _write(path, content)
    assert not mutation_cache.validate()


@pytest.mark.parametrize("path,content", [
    ("tests/conftest.py", "def pytest_configure(config): pass\n"),
    ("custom_components/daylight_calendar_import/__init__.py", "# new package init\n"),
    ("requirements_more.txt", "new_dependency\n"),
])
def test_rejects_new_shared_inputs(monkeypatch, tmp_path, path, content):
    _baseline(monkeypatch, tmp_path)
    _write(path, content)
    _git("add", path)
    assert not mutation_cache.validate()


def test_rejects_deleted_existing_test(monkeypatch, tmp_path):
    _baseline(monkeypatch, tmp_path)
    Path("tests/test_old.py").unlink()
    _git("add", "-u")
    assert not mutation_cache.validate()


def test_rejects_forged_legacy_fingerprint(monkeypatch, tmp_path):
    _baseline(monkeypatch, tmp_path)
    saved = json.loads(mutation_cache.CACHE_META.read_text())
    saved["fingerprint"] = "0" * 64
    mutation_cache.CACHE_META.write_text(json.dumps(saved))
    _write("tests/test_new.py", "def test_new(): pass\n")
    _git("add", ".")
    assert not mutation_cache.validate()


def test_missing_or_unknown_baseline_commit_fails_closed(monkeypatch, tmp_path):
    _baseline(monkeypatch, tmp_path)
    _write("tests/test_new.py", "def test_new(): pass\n")
    _git("add", ".")
    mutation_cache.STATS.write_text("{}")
    assert not mutation_cache.validate()
    mutation_cache.STATS.write_text(json.dumps({"git_commit": "a" * 40}))
    assert not mutation_cache.validate()


def test_allows_cache_workflow_plumbing_changes_but_not_mutation_command(monkeypatch, tmp_path):
    _baseline(monkeypatch, tmp_path)
    before = Path(".github/workflows/mutation.yml").read_text(encoding="utf-8")
    after = before.replace(
        "      - uses: actions/checkout@v5\n",
        "      - uses: actions/checkout@v5\n"
        "        with:\n          fetch-depth: 0\n",
    ).replace(
        "      - name: Restore mutation cache\n",
        "      - name: Restore PR mutation cache\n",
    )
    _write(".github/workflows/mutation.yml", after)
    assert mutation_cache.validate()

    # Adjusting the mutmut invocation itself is *not* cache plumbing.
    _write(".github/workflows/mutation.yml", after.replace("mutmut run", "mutmut run --max-children=1"))
    assert not mutation_cache.validate()


def test_rejects_mismatched_resolved_dependency_versions(monkeypatch, tmp_path):
    _baseline(monkeypatch, tmp_path)
    _write("tests/test_new.py", "def test_new(): pass\n")
    _git("add", ".")
    original_distributions = mutation_cache.metadata.distributions
    monkeypatch.setattr(mutation_cache.metadata, "distributions", lambda: ())
    assert not mutation_cache.validate()
    monkeypatch.setattr(mutation_cache.metadata, "distributions", original_distributions)

def test_new_tests_invalidate_only_stale_non_killed_verdicts(monkeypatch, tmp_path):
    _, _ = _baseline(monkeypatch, tmp_path)
    meta_path = Path("mutants/custom_components/daylight_calendar_import/old.py.meta")
    meta_path.write_text(json.dumps({
        "exit_code_by_key": {
            "was_surviving": 0, "already_killed": 1, "internal_kill": 3,
            "had_no_tests": 33, "was_timeout": 36,
            "type_checked": 37, "skipped": 34,
        },
        "hash_by_function_name": {},
        "durations_by_key": {},
        "estimated_durations_by_key": {},
        "type_check_error_by_key": {},
    }))
    _write("tests/test_new.py", "def test_new(): assert True\n")
    _git("add", "tests/test_new.py")
    assert mutation_cache.cache_validation() == (True, ["tests/test_new.py"])
    monkeypatch.setattr("sys.argv", ["mutation_cache.py", "validate"])
    assert mutation_cache.main() == 0
    verdicts = json.loads(meta_path.read_text())["exit_code_by_key"]
    assert verdicts["was_surviving"] is None
    assert verdicts["had_no_tests"] is None
    assert verdicts["was_timeout"] is None
    assert verdicts["already_killed"] == 1
    assert verdicts["internal_kill"] == 3
    assert verdicts["type_checked"] == 37
    assert verdicts["skipped"] == 34


def test_pr_manifest_survives_unreachable_synthetic_merge_commit(monkeypatch, tmp_path):
    _baseline(monkeypatch, tmp_path)
    files = mutation_cache._cache_files()
    env = mutation_cache._environment_fingerprint()
    mutation_cache.CACHE_META.write_text(json.dumps({
        "schema": mutation_cache.CACHE_SCHEMA,
        "fingerprint": mutation_cache.fingerprint(),
        "source_commit": "a" * 40,  # Old PR merge ref is no longer reachable.
        "files": files,
        "environment": env,
        "signature": mutation_cache._snapshot_signature(files, env),
    }))
    _write("custom_components/daylight_calendar_import/old.py",
           "LIMIT = 3\ndef old():\n    return LIMIT + 1\n")
    assert mutation_cache.cache_validation() == (True, [])
    _write("tests/test_new.py", "def test_new(): assert True\n")
    _git("add", "tests/test_new.py")
    assert mutation_cache.cache_validation() == (True, ["tests/test_new.py"])
    _write("tests/test_old.py", "def test_old(): assert False\n")
    assert not mutation_cache.validate()


def test_manifest_environment_and_signature_fail_closed(monkeypatch, tmp_path):
    _baseline(monkeypatch, tmp_path)
    files = mutation_cache._cache_files()
    env = mutation_cache._environment_fingerprint()
    saved = {
        "schema": mutation_cache.CACHE_SCHEMA,
        "fingerprint": mutation_cache.fingerprint(),
        "source_commit": "a" * 40,
        "files": files,
        "environment": env,
        "signature": mutation_cache._snapshot_signature(files, env),
    }
    _write("tests/test_new.py", "def test_new(): assert True\n")
    _git("add", "tests/test_new.py")
    mutation_cache.CACHE_META.write_text(json.dumps(saved))
    assert mutation_cache.validate()
    for field, replacement in [
        ("environment", "mismatched"), ("signature", "forged"),
        ("files", {"tests/test_old.py": "not-a-sha"}),
    ]:
        changed = {**saved, field: replacement}
        mutation_cache.CACHE_META.write_text(json.dumps(changed))
        assert not mutation_cache.validate()


def test_stamp_records_self_contained_manifest(monkeypatch, tmp_path):
    _baseline(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.argv", ["mutation_cache.py", "stamp"])
    assert mutation_cache.main() == 0
    saved = json.loads(mutation_cache.CACHE_META.read_text())
    assert saved["files"] == mutation_cache._cache_files()
    assert saved["environment"] == mutation_cache._environment_fingerprint()
    assert mutation_cache.validate()
