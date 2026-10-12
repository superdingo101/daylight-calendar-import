"""Non-gating mutation impact planner. Never authorizes a skipped quality gate."""
from __future__ import annotations
import argparse
import ast
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import time

SOURCE = "custom_components/daylight_calendar_import/"
# Keep this policy in sync with scripts.pr_mutation.CI_TESTS until the old gate is retired.
CI_TESTS = {
    "tests/test_mutation_ci_strategy.py",
    "tests/test_export_mutation_survivors.py",
    "tests/test_pr_mutation_scope.py",
    "tests/test_pr_mutation_cache.py",
    "tests/test_mutation_shadow.py",
    "tests/test_mutation_impact.py",
    "tests/test_mutation_cache_reuse.py",
    "tests/test_pr_mutation_rebuild_workflow.py",
}
GLOBAL = {"pyproject.toml", "pytest.ini", "tox.ini", "setup.cfg", "conftest.py", "tests/conftest.py", SOURCE + "manifest.json"}
OUT = Path("mutation-shadow-plan.json")

def changed(base, head, kind="ACMRTD"):
    if not all(re.fullmatch(r"[a-f0-9]{40}", sha) for sha in (base, head)):
        raise ValueError("Expected full SHAs")
    raw = subprocess.check_output(["git", "diff", "--no-renames", "--name-only", "-z", "--diff-filter="+kind, base+"..."+head, "--"])
    return {os.fsdecode(p) for p in raw.split(b"\0") if p}

def source(path):
    return path.startswith(SOURCE) and not path.startswith(SOURCE+"frontend/") and path.endswith(".py")

def app_test(path):
    return path.startswith("tests/") and path not in CI_TESTS

def classify(paths, modified, added):
    """Plan conservatively: unknown impact is never a clean skip."""
    modules = sorted(p for p in paths if source(p) and Path(p).is_file())
    tests = {p for p in paths if app_test(p)}
    measurable = sorted(p for p in tests if Path(p).is_file() and Path(p).name.startswith("test_") and p.endswith(".py"))
    added_tests = sorted(added & set(measurable))

    global_change = any(
        p in GLOBAL or p.startswith(("requirements", "tests/fixtures/", "tests/data/"))
        or (p.startswith("tests/") and p.endswith("/conftest.py"))
        for p in paths
    )
    if global_change:
        return "comprehensive-sharded", modules, "Shared dependency, fixture or configuration changed", measurable

    # --no-renames reports the old name of a rename as a deletion. Losing a
    # production module can affect other modules even though its file no longer
    # exists, so do not drop it and erroneously report a safe skip.
    deleted_source = sorted(p for p in paths if source(p) and not Path(p).is_file())
    if deleted_source:
        return "comprehensive-sharded", modules, "Deleted/renamed-away production module: impact unknown", measurable

    # Runtime resources can change program behavior without changing .py
    # files. Only frontend assets have been deliberately exempted.
    runtime_resources = sorted(
        p for p in paths if p.startswith(SOURCE) and not p.startswith(SOURCE + "frontend/")
        and not source(p)
    )
    if runtime_resources:
        return "comprehensive-sharded", modules, "Integration runtime resource changed", measurable

    if modified & tests:
        return "needs-base-and-head-coverage", modules, "Edited/deleted test requires both base and head coverage", measurable
    if any(not p.endswith(".py") or not Path(p).name.startswith("test_") for p in tests):
        return "comprehensive-sharded", modules, "Shared test helper changed", measurable

    # A test added alongside changed production might kill existing mutants
    # in other, unchanged modules. We must union its executed-function modules
    # with changed production before declaring the scoped plan complete.
    if added_tests:
        return "coverage-required", modules, "Added tests require fixture-aware execution coverage", added_tests
    if modules:
        return "scoped", modules, "Changed production modules; no newly added application tests", []
    if tests:
        return "comprehensive-sharded", [], "Unknown application test impact", measurable

    return "skip", [], "No mutation-relevant changes", []

def body_executed(path, lines):
    if not lines:
        return False
    tree = ast.parse(Path(path).read_bytes(), filename=path)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for statement in node.body:
            start = getattr(statement, "lineno", 0)
            end = getattr(statement, "end_lineno", start)
            if any(start <= n <= end for n in lines):
                return True
    return False

def report_modules(report):
    inclusive, executed, phases = [], [], {}
    for name, info in report.get("files", {}).items():
        if not source(name) or not Path(name).is_file():
            continue
        if info.get("executed_lines"):
            inclusive.append(name)
        by_phase = {"setup": set(), "run": set(), "teardown": set()}
        for line, contexts in info.get("contexts", {}).items():
            for phase in by_phase:
                if any(str(context).endswith("|"+phase) for context in contexts):
                    by_phase[phase].add(int(line))
        hits = sorted(phase for phase, lines in by_phase.items() if body_executed(name, lines))
        if hits:
            executed.append(name)
            phases[name] = hits
    return sorted(inclusive), sorted(executed), phases

def coverage(tests):
    start = time.monotonic()
    report = Path(".mutation-shadow-coverage.json")
    try:
        # Dynamic pytest contexts require coverage\u0027s C tracer, not sys.monitoring.
        env = {**os.environ, "COVERAGE_CORE": "ctrace"}
        subprocess.run([sys.executable, "-m", "pytest", "-q", "--cov=custom_components.daylight_calendar_import", "--cov-context=test", "--cov-report=", "--cov-fail-under=0", *tests], check=True, env=env)
        subprocess.run([sys.executable, "-m", "coverage", "json", "--show-contexts", "--fail-under=0", "-o", str(report)], check=True)
        inclusive, executed, phases = report_modules(json.loads(report.read_text()))
        return {"import_inclusive_modules": inclusive, "execution_modules": executed, "execution_phases": phases, "duration_seconds": round(time.monotonic()-start, 3)}
    finally:
        report.unlink(missing_ok=True)

def plan(base, head, measure=True):
    paths = changed(base, head)
    modified = changed(base, head, "MRTD")
    added = changed(base, head, "A")
    mode, modules, reason, tests = classify(paths, modified, added)
    result = {"schema": 1, "shadow": True, "required_gate_unchanged": True,
              "base_sha": base, "head_sha": head, "changed_files": sorted(paths),
              "existing_test_edits": sorted(modified & {p for p in paths if app_test(p)}),
              "mode": mode, "reason": reason, "source_modules": modules,
              "test_files_measured": tests, "coverage": None,
              "estimated_mutants": None, "estimated_runtime_seconds": None}
    if measure and tests:
        result["coverage"] = coverage(tests)
        if mode == "coverage-required":
            found = result["coverage"]["execution_modules"]
            if found:
                # Do not omit modules whose existing mutants may now be killed
                # by new tests, even in mixed production + test changes.
                result["mode"] = "scoped"
                result["source_modules"] = sorted(set(modules) | set(found))
                result["reason"] = "Changed modules plus function execution by added tests"
            else:
                result["mode"] = "comprehensive-sharded"
                result["reason"] = "No verifiable execution from new tests: escalate"
    return result

def main():
    cli = argparse.ArgumentParser()
    cli.add_argument("--base", required=True)
    cli.add_argument("--head", required=True)
    cli.add_argument("--no-coverage", action="store_true")
    args = cli.parse_args()
    start = time.monotonic()
    try:
        result = plan(args.base, args.head, not args.no_coverage)
        code = 0
    except (OSError, ValueError, KeyError, SyntaxError, subprocess.CalledProcessError) as exc:
        result = {"schema": 1, "shadow": True, "mode": "error", "base_sha": args.base, "head_sha": args.head, "reason": str(exc)}
        code = 1
    result["planning_duration_seconds"] = round(time.monotonic()-start, 3)
    OUT.write_text(json.dumps(result, indent=2)+"\n")
    print(f"Shadow plan: {result['mode']}: {result['reason']}")
    summary = os.getenv("GITHUB_STEP_SUMMARY")
    # The lightweight preview is superseded by the measured plan when tests
    # require dependency installation. Avoid duplicate workflow summaries.
    if summary and not (args.no_coverage and result.get("test_files_measured")):
        with Path(summary).open("a") as out:
            out.write("### Mutation scope shadow (not a quality gate)\n\n")
            out.write(f"- Proposed mode: {result['mode']}\n- Reason: {result['reason']}\n")
            out.write("- Existing required Mutation score: unchanged\n")
            if result.get("coverage"):
                out.write(f"- Import-inclusive modules: {len(result['coverage']['import_inclusive_modules'])}\n")
                out.write(f"- Fixture/call execution modules: {len(result['coverage']['execution_modules'])}\n")
    return code

if __name__ == "__main__":
    raise SystemExit(main())
