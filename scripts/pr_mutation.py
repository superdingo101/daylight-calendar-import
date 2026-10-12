"""Select application modules for PR mutation checks without baseline cache rebuilds.

A PR is compared with *its own* target branch, including stacked PRs.
Mutmut still discovers relevant tests across the entire suite.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tomllib

SOURCE = "custom_components/daylight_calendar_import/"
FRONTEND = SOURCE + "frontend/"
SCOPE = Path(".pr-mutation-scope.json")
BACKUP = Path(".pr-mutation-pyproject-original")
CONFIG = Path("pyproject.toml")
CI_TESTS = {
    "tests/test_mutation_ci_strategy.py",
    "tests/test_export_mutation_survivors.py",
    "tests/test_pr_mutation_scope.py",
    "tests/test_pr_mutation_cache.py",
    # Shadow instrumentation tests exercise CI policy, not application code.
    "tests/test_mutation_shadow.py",
    "tests/test_mutation_impact.py",
    "tests/test_mutation_shards.py",
    "tests/test_mutation_pr_gate.py",
    # Removed obsolete cache-specific test modules are CI-only too.
    "tests/test_mutation_cache_reuse.py",
    "tests/test_pr_mutation_rebuild_workflow.py",
}
SHA = re.compile(r"[0-9a-f]{40}\Z")


def changed_paths(base: str, head: str, kind: str = "ACMRTD") -> set[str]:
    """Use NUL names, preserve renamed old paths and ignore base-only changes."""
    if not SHA.fullmatch(base) or not SHA.fullmatch(head):
        raise ValueError("Expected 40-character base and head Git SHAs")
    output = subprocess.check_output([
        "git", "diff", "--no-renames", "--name-only", "-z", f"--diff-filter={kind}",
        f"{base}...{head}", "--",
    ])
    return {os.fsdecode(p) for p in output.split(b"\0") if p}


def test_coverage(paths: list[str]) -> set[str]:
    """Select production files exercised by changed tests using actual coverage."""
    subprocess.run([sys.executable, "-m", "coverage", "run", "-m", "pytest",
                    "-q", *paths], check=True)
    report = Path(".pr-mutation-coverage.json")
    try:
        subprocess.run([sys.executable, "-m", "coverage", "json",
                        "--fail-under=0", "-o", str(report)], check=True)
        results = json.loads(report.read_text(encoding="utf-8"))
        return {
            file for file, entry in results["files"].items()
            if file.startswith(SOURCE) and not file.startswith(FRONTEND)
            and file.endswith(".py") and entry.get("executed_lines")
            and Path(file).is_file()
        }
    finally:
        report.unlink(missing_ok=True)


def select(base: str, head: str) -> tuple[str, list[str], str]:
    files = changed_paths(base, head)
    if not files:
        return "skip", [], "No changed files"

    global_files = {"pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg",
                    "conftest.py", "tests/conftest.py", SOURCE + "manifest.json"}
    if any(
        file in global_files or file.startswith(("requirements", "tests/fixtures/",
                                                  "tests/data/"))
        or (file.startswith("tests/") and file.endswith("/conftest.py"))
        for file in files
    ):
        return "full", [], "Shared test, dependency or integration settings changed"
    # Modified/deleted existing tests can STOP covering a module. Current-head
    # coverage cannot reconstruct what they previously exercised, so only a
    # full run is safe. New tests have no old behavior to lose.
    modified_tests = changed_paths(base, head, "MRTD")
    if any(file.startswith("tests/") and file not in CI_TESTS
           for file in modified_tests):
        return "full", [], "An existing application test changed or was removed"

    # Shared helpers and fixtures affect unrelated tests, even if their own
    # names do not begin with test_. Do not silently classify them as CI-only.
    application_tests = {
        file for file in files if file.startswith("tests/")
        and file not in CI_TESTS
    }
    if any(not Path(file).name.startswith("test_") or not file.endswith(".py")
           for file in application_tests):
        return "full", [], "Shared test helpers or fixtures changed"

    selected = {
        file for file in files if file.startswith(SOURCE)
        and not file.startswith(FRONTEND) and file.endswith(".py")
        and Path(file).is_file()
    }
    added_tests = sorted(
        file for file in application_tests
        if file.endswith(".py") and Path(file).is_file()
    )
    if added_tests:
        # Always union test coverage with changed source, even in mixed PRs.
        # Otherwise a new test could expose mutations in another module.
        covered = test_coverage(added_tests)
        if not covered and not selected:
            return "full", [], "Added tests have no identifiable production coverage"
        selected.update(covered)
    if selected:
        return "scoped", sorted(selected), "Changed production modules and modules covered by added tests"
    return "skip", [], "No affected Python modules (docs, frontend or CI-only)"


def configure_only_mutate(paths: list[str]) -> None:
    original = CONFIG.read_bytes()
    body = original.decode("utf-8")
    parsed = tomllib.loads(body)
    mutation = parsed.get("tool", {}).get("mutmut")
    if not isinstance(mutation, dict) or "only_mutate" in mutation:
        raise ValueError("Expected [tool.mutmut] with no only_mutate setting")
    header = re.search(r"(?m)^\[tool\.mutmut\][ \t]*$", body)
    if not header:
        raise ValueError("Missing [tool.mutmut] section")
    next_header = re.search(r"(?m)^\[[^\n]+\][ \t]*$", body[header.end():])
    index = header.end() + next_header.start() if next_header else len(body)
    changed = (body[:index].rstrip() + "\nonly_mutate = "
               + json.dumps(paths) + "\n\n" + body[index:].lstrip("\n"))
    tomllib.loads(changed)
    BACKUP.write_bytes(original)
    CONFIG.write_text(changed, encoding="utf-8")


def restore() -> None:
    if BACKUP.is_file():
        CONFIG.write_bytes(BACKUP.read_bytes())
        BACKUP.unlink()


def prepare(base: str, head: str) -> None:
    mode, modules, reason = select(base, head)
    if mode == "scoped":
        configure_only_mutate(modules)
    SCOPE.write_text(json.dumps({"mode": mode, "modules": modules, "reason": reason,
                                 "base": base, "head": head}, indent=2) + "\n")
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"mode={mode}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as stream:
            stream.write(f"### PR mutation selection: {mode}\n\n{reason}\n\n")
            for module in modules:
                stream.write(f"- {module}\n")
    print(f"Mutation mode: {mode}. {reason}")
    for module in modules:
        print(f"  {module}")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["prepare", "restore"])
    parser.add_argument("--base")
    parser.add_argument("--head")
    args = parser.parse_args()
    if args.action == "restore":
        restore()
        return 0
    if not args.base or not args.head:
        parser.error("--base and --head required")
    try:
        prepare(args.base, args.head)
        return 0
    except (OSError, KeyError, ValueError, subprocess.CalledProcessError) as error:
        print(f"Cannot safely select mutation scope: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
