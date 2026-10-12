"""Candidate bounded mutation PR work policy; never supersedes the required gate.

Diffs use the PR's own merge-base, including stacked PRs. Modified/deleted
tests are measured on both revisions. Missing evidence never triggers an
unsharded full mutation run or an unearned passing score.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

# The planner runs from the PR head even when its coverage probe runs inside
# a temporary checkout of the merge base.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import mutation_shadow as shadow

OUT = Path("mutation-impact-plan.json")
DEFAULT_MAX_MODULES = 4
DEFAULT_BUDGET_MINUTES = 20


def merge_base(base: str, head: str) -> str:
    return subprocess.check_output(
        ["git", "merge-base", base, head], text=True,
    ).strip()


def _run_probe(tests: list[str], *, cwd: Path | None = None) -> dict:
    if cwd is None:
        return shadow.coverage(tests)
    # Use the planner from the PR head against the tests and source from the
    # old checkout. Running tests in the head would lose removed coverage.
    with tempfile.TemporaryDirectory(prefix="mutation-base-probe-") as tmp:
        out = Path(tmp) / "coverage.json"
        subprocess.run(
            [sys.executable, str(Path(__file__).resolve()), "probe",
             "--out", str(out), "--tests", *tests],
            cwd=cwd, check=True,
        )
        return json.loads(out.read_text(encoding="utf-8"))


def base_test_coverage(base: str, head: str, tests: list[str]) -> dict:
    """Run old tests in an isolated checkout at the three-dot merge base."""
    with tempfile.TemporaryDirectory(prefix="mutation-impact-base-") as tmp:
        checkout = Path(tmp) / "checkout"
        commit = merge_base(base, head)
        subprocess.run(
            ["git", "worktree", "add", "--detach", str(checkout), commit],
            check=True, stdout=subprocess.DEVNULL,
        )
        try:
            if any(not (checkout / p).is_file() for p in tests):
                raise ValueError("Changed existing test absent from merge base")
            return _run_probe(tests, cwd=checkout)
        finally:
            subprocess.run(
                ["git", "worktree", "remove", "--force", str(checkout)],
                check=True, stdout=subprocess.DEVNULL,
            )


def _shared_test_risk_in_source(body: str, path: str) -> str | None:
    """Detect test modules that may export behavior to unselected tests.

    Only modules consisting of imports and standalone test functions can be
    treated as isolated. Fixtures, helper callables, class definitions and
    module-level executable statements/assignments may affect other modules.
    The scan checks both the old and new text, so deleting an export is caught.
    """
    tree = ast.parse(body, filename=path)
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            continue
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if not node.name.startswith("test_"):
                return f"Exportable fixture/helper {node.name} in {path}"
            for decorator in node.decorator_list:
                # Even a test_* callable can be repurposed as a fixture via a
                # decorator, and decorators themselves can register providers.
                text = ast.unparse(decorator)
                if "fixture" in text or "pytest_plugins" in text:
                    return f"Potentially shared test decorator in {path}"
            continue
        # Classes, assignments, registrations, pytest hooks, mutations and
        # other module-level statements may expose behavior outside this file.
        return f"Module-level exported/shared state in {path}"
    return None


def shared_test_risk(base: str, head: str, paths: list[str]) -> str | None:
    """Assess both source revisions without executing or trusting imports."""
    commit = merge_base(base, head)
    for path in paths:
        old_body = subprocess.check_output(
            ["git", "show", f"{commit}:{path}"], text=True,
        )
        risk = _shared_test_risk_in_source(old_body, path)
        if risk:
            return f"Old revision: {risk}"
        if Path(path).is_file():
            risk = _shared_test_risk_in_source(
                Path(path).read_text(encoding="utf-8"), path,
            )
            if risk:
                return f"Current revision: {risk}"
    return None


def _escalate(result: dict, reason: str) -> dict:
    result.update(
        mode="comprehensive-sharded",
        reason=reason,
        may_execute_mutmut=False,
    )
    return result


def plan(
    base: str, head: str, *,
    max_modules: int = DEFAULT_MAX_MODULES,
    budget: int = DEFAULT_BUDGET_MINUTES,
    measure: bool = True,
) -> dict:
    if max_modules < 1 or budget < 1:
        raise ValueError("Module cap and PR budget must be positive")
    preview = shadow.plan(base, head, measure=False)
    result = {
        "schema": 1,
        "informational": True,
        "required_gate_unchanged": True,
        "base_sha": base,
        "head_sha": head,
        "merge_base_sha": merge_base(base, head),
        "mode": preview["mode"],
        "reason": preview["reason"],
        "selected_modules": list(preview["source_modules"]),
        "changed_files": list(preview["changed_files"]),
        "existing_test_edits": list(preview["existing_test_edits"]),
        "head_execution_modules": [],
        "base_execution_modules": [],
        "head_collection_function_modules": [],
        "base_collection_function_modules": [],
        "max_modules_per_job": max_modules,
        "pr_job_budget_minutes": budget,
        "may_execute_mutmut": False,
        "same_scope_baseline_verified": False,
        "comprehensive_sharded_available": False,
        "full_project_score_unchanged": True,
    }
    mode = preview["mode"]
    if mode in {"skip", "comprehensive-sharded"}:
        return result

    # If the known directly edited production scope already exceeds the
    # budget, no old/head pytest probe could make it smaller.
    if len(result["selected_modules"]) > max_modules:
        return _escalate(
            result, f"{len(result['selected_modules'])} directly changed modules "
                    f"exceed the {max_modules}-module PR work budget"
        )

    if not measure:
        if mode in {"coverage-required", "needs-base-and-head-coverage"}:
            result["reason"] += "; execution impact pending measurement"
            return result
    elif mode in {"coverage-required", "needs-base-and-head-coverage"}:
        try:
            paths = shadow.changed(base, head)
            added = shadow.changed(base, head, "A")
            edited = shadow.changed(base, head, "MRTD")
            affected = {p for p in paths if shadow.app_test(p)}
            new_tests = sorted(
                p for p in added & affected if Path(p).is_file() and p.endswith(".py")
            )
            old_tests = sorted(edited & affected)
            if old_tests:
                shared_risk = shared_test_risk(base, head, old_tests)
                if shared_risk:
                    return _escalate(
                        result, "Edited test may affect other test modules: "
                                + shared_risk,
                    )
            head_tests = sorted(set(new_tests) | {
                p for p in old_tests if Path(p).is_file()
            })
            if head_tests:
                head_coverage = _run_probe(head_tests)
                result["head_execution_modules"] = head_coverage["execution_modules"]
                result["head_collection_function_modules"] = head_coverage[
                    "collection_function_modules"
                ]
            if old_tests:
                old_coverage = base_test_coverage(base, head, old_tests)
                result["base_execution_modules"] = old_coverage["execution_modules"]
                result["base_collection_function_modules"] = old_coverage[
                    "collection_function_modules"
                ]
            # Dynamic pytest contexts cannot attribute a function called
            # during module collection to one test. These are genuine
            # function-body executions, not mere package import lines.
            # Fail closed rather than returning an incomplete scoped plan.
            collection = set(result["head_collection_function_modules"]) | set(
                result["base_collection_function_modules"]
            )
            if collection:
                return _escalate(
                    result, "Production function executed during test "
                            "collection: " + ", ".join(sorted(collection))
                )
            evidence = set(result["head_execution_modules"]) | set(
                result["base_execution_modules"]
            )
            if not evidence:
                return _escalate(
                    result, "Cannot verify function execution on either revision"
                )
            selected = set(preview["source_modules"]) | evidence
            if not all(shadow.source(p) and Path(p).is_file() for p in selected):
                return _escalate(
                    result, "Previous test coverage includes missing production source"
                )
            result["selected_modules"] = sorted(selected)
            result["mode"] = "scoped"
            result["reason"] = "Changed source + function execution from both revisions"
        except (
            OSError, ValueError, KeyError, SyntaxError, TypeError,
            subprocess.CalledProcessError, json.JSONDecodeError,
        ) as exc:
            return _escalate(
                result, f"Unable to prove safe impact: {type(exc).__name__}: {exc}"
            )

    # A cold/missing cache must never widen the scope to all production files.
    # This is module-count eligibility, *not* proof of a bounded runtime.
    if len(result["selected_modules"]) > max_modules:
        return _escalate(
            result, f"{len(result['selected_modules'])} modules exceed "
                    f"the {max_modules}-module PR work budget"
        )
    if result["mode"] == "scoped" and result["selected_modules"]:
        result["may_execute_mutmut"] = True
    return result


def main() -> int:
    cli = argparse.ArgumentParser()
    actions = cli.add_subparsers(dest="action", required=True)
    prepare = actions.add_parser("prepare")
    prepare.add_argument("--base", required=True)
    prepare.add_argument("--head", required=True)
    prepare.add_argument("--max-modules", type=int, default=DEFAULT_MAX_MODULES)
    prepare.add_argument("--budget-minutes", type=int, default=DEFAULT_BUDGET_MINUTES)
    prepare.add_argument("--no-coverage", action="store_true")
    probe = actions.add_parser("probe")
    probe.add_argument("--out", required=True)
    probe.add_argument("--tests", nargs="+", required=True)
    args = cli.parse_args()

    if args.action == "probe":
        Path(args.out).write_text(json.dumps(shadow.coverage(args.tests)) + "\n")
        return 0

    started = time.monotonic()
    try:
        result = plan(
            args.base, args.head,
            max_modules=args.max_modules, budget=args.budget_minutes,
            measure=not args.no_coverage,
        )
        rc = 0
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as exc:
        result = {
            "schema": 1, "informational": True,
            "mode": "error", "reason": f"{type(exc).__name__}: {exc}",
            "may_execute_mutmut": False,
        }
        rc = 1

    result["planning_seconds"] = round(time.monotonic() - started, 3)
    OUT.write_text(json.dumps(result, indent=2) + "\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as stream:
            stream.write(
                "### Bounded PR mutation impact (informational)\n\n"
                f"- Mode: **{result['mode']}**\n"
                f"- Reason: {result['reason']}\n"
                f"- Eligible for a bounded mutmut job: "
                f"**{result['may_execute_mutmut']}**\n"
                "- Required Mutation score: **unchanged**\n"
            )
    print(f"Bounded impact mode: {result['mode']}: {result['reason']}")
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
