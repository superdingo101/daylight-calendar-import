"""Cold bounded PR execution with exact revisions and same-scope comparison."""
from __future__ import annotations

import argparse
import ast
import json
import os
import re
from pathlib import Path
import subprocess
import sys

# The current driver is copied outside the checkout before testing the base.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from scripts import mutation_shards as shards
from scripts.pr_mutation import configure_only_mutate

PLAN = Path("mutation-impact-plan.json")
EXECUTION = Path("bounded-execution.json")


def execution_plan(base: str, head: str, *, comprehensive: bool = False) -> dict:
    from scripts import mutation_impact
    result = mutation_impact.plan(base, head, measure=not comprehensive)
    if comprehensive:
        result.update(mode="comprehensive-sharded", may_execute_mutmut=False,
                      reason="Explicit comprehensive validation requested")
    if result["mode"] == "scoped":
        for path in result["selected_modules"]:
            tree = ast.parse(Path(path).read_text())
            if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(tree)):
                result.update(mode="comprehensive-sharded", may_execute_mutmut=False,
                              reason="Source without mutable functions requires comprehensive validation")
                break
            old = subprocess.run(["git", "cat-file", "-e", f"{result['merge_base_sha']}:{path}"],
                                 capture_output=True)
            if old.returncode:
                result.update(mode="comprehensive-sharded", may_execute_mutmut=False,
                              reason="New production module requires comprehensive validation")
                break
            body = subprocess.check_output(["git", "show", f"{result['merge_base_sha']}:{path}"], text=True)
            if not any(isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) for n in ast.walk(ast.parse(body))):
                result.update(mode="comprehensive-sharded", may_execute_mutmut=False,
                              reason="New mutable source scope requires comprehensive validation")
                break
    if result["mode"] not in {"skip", "scoped", "comprehensive-sharded"}:
        raise ValueError("Impact measurement did not establish a safe execution tier")
    result.update(informational=False, report_role="execution_plan",
                  required_gate_unchanged=False, comprehensive_sharded_available=True)
    shards.write_json(PLAN, result)
    output = os.environ.get("GITHUB_OUTPUT")
    if output:
        with Path(output).open("a") as stream:
            stream.write(f"mode={result['mode']}\nbase={result['merge_base_sha']}\n")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with Path(summary).open("a") as stream:
            stream.write(f"## PR mutation tier: {result['mode']}\n\n{result['reason']}\n\n")
            stream.write("Scoped scores describe selected modules; comprehensive scores describe the project.\n")
    print(f"Mutation tier: {result['mode']}: {result['reason']}")
    return result


def validate_plan(plan: dict) -> list[str]:
    modules = plan.get("selected_modules")
    if (plan.get("mode") != "scoped" or plan.get("may_execute_mutmut") is not True
            or not isinstance(modules, list) or not modules
            or any(not isinstance(p, str) for p in modules)
            or modules != sorted(set(modules))
            or type(plan.get("max_modules_per_job")) is not int
            or not 1 <= len(modules) <= plan["max_modules_per_job"]):
        raise ValueError("Invalid bounded mutation plan")
    for path in modules:
        if (not path.startswith(shards.SOURCE) or not path.endswith(".py")
                or path.startswith(shards.SOURCE + "frontend/")
                or ".." in Path(path).parts):
            raise ValueError("Invalid bounded production source")
    for field in ("head_sha", "merge_base_sha"):
        if not isinstance(plan.get(field), str) or not re.fullmatch(r"[0-9a-f]{40}", plan[field]):
            raise ValueError("Invalid bounded revision")
    return modules


def prepare(revision: str) -> dict:
    plan = json.loads(PLAN.read_text())
    modules = validate_plan(plan)
    expected = plan["head_sha"] if revision == "head" else plan["merge_base_sha"]
    if shards.git("rev-parse", "HEAD") != expected:
        raise ValueError("Execution checkout differs from the measured revision")
    if not set(modules).issubset(shards.source_paths()):
        raise ValueError("A selected module is missing; comprehensive validation is required")
    if Path(".pr-mutation-pyproject-original").exists():
        raise ValueError("Mutation configuration backup already exists")
    result = {"schema": 1, "revision": revision, "sha": expected, "modules": modules,
              "plan": plan, "plan_sha256": shards.sha256(shards.canonical(plan))}
    configure_only_mutate(modules)
    shards.write_json(EXECUTION, result)
    return result


def record(revision: str) -> dict:
    result = json.loads(EXECUTION.read_text())
    modules = validate_plan(result["plan"])
    if (result["revision"] != revision or result["modules"] != modules
            or shards.git("rev-parse", "HEAD") != result["sha"]):
        raise ValueError("Bounded result revision mismatch")
    counts = shards.stats_checked(json.loads(shards.STATS.read_text()), require_nonempty=True)
    mutants = shards.collect_mutants(modules)
    shards.validate_mutants(mutants, modules, counts)
    result.update(stats=counts, mutants=mutants, complete=True, environment=shards.environment())
    shards.write_json(Path(f"bounded-{revision}.json"), result)
    return result


def score(stats: dict) -> float:
    return 100 * (stats["killed"] + stats["timeout"]) / (stats["total"] - stats["skipped"])


def enforce(base_path: Path, head_path: Path) -> dict:
    base, head = (json.loads(p.read_text()) for p in (base_path, head_path))
    for revision, result in (("base", base), ("head", head)):
        modules = validate_plan(result["plan"])
        expected = result["plan"]["head_sha"] if revision == "head" else result["plan"]["merge_base_sha"]
        if (result.get("schema") != 1 or result.get("complete") is not True
                or result.get("revision") != revision or result.get("sha") != expected
                or result.get("modules") != modules
                or result.get("plan_sha256") != shards.sha256(shards.canonical(result["plan"]))
                or result["plan"] != head["plan"]):
            raise ValueError("Inconsistent scoped mutation evidence")
        shards.validate_environment(result.get("environment"))
        counts = shards.stats_checked(result.get("stats"), require_nonempty=True)
        shards.validate_mutants(result.get("mutants"), modules, counts)
        if any(counts[f] for f in ("no_tests", "suspicious", "segfault")):
            raise ValueError("Scoped mutation evidence contains error outcomes")
    if base["environment"] != head["environment"]:
        raise ValueError("Base/head resolved environments differ")
    minimum = float(json.loads(Path("mutation-baseline.json").read_text())["minimum_score"])
    head_score, base_score = score(head["stats"]), score(base["stats"])
    if head_score + 1e-9 < minimum:
        raise ValueError(f"Scoped score {head_score:.4f}% is below {minimum}%")
    if head_score + 1e-9 < base_score:
        raise ValueError(f"Same-scope score regressed from {base_score:.4f}% to {head_score:.4f}%")
    result = {"schema": 1, "mode": "scoped", "base_score": base_score,
              "head_score": head_score, "source_modules": head["modules"]}
    shards.write_json(Path("bounded-comparison.json"), result)
    print(f"Scoped mutation score {head_score:.4f}% (base {base_score:.4f}%, floor {minimum}%)")
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=["preview", "plan", "prepare", "record", "enforce"])
    parser.add_argument("--base")
    parser.add_argument("--head")
    parser.add_argument("--base-result", type=Path, default=Path("bounded-base.json"))
    parser.add_argument("--head-result", type=Path, default=Path("bounded-head.json"))
    parser.add_argument("--revision", choices=["base", "head"])
    parser.add_argument("--comprehensive", action="store_true")
    args = parser.parse_args()
    try:
        if args.action == "preview":
            from scripts import mutation_impact
            preview = mutation_impact.plan(args.base, args.head, measure=False)
            coverage = preview["mode"] in {"coverage-required", "needs-base-and-head-coverage"} and not args.comprehensive
            if os.environ.get("GITHUB_OUTPUT"):
                with Path(os.environ["GITHUB_OUTPUT"]).open("a") as stream:
                    stream.write(f"needs_coverage={'true' if coverage else 'false'}\n")
        elif args.action == "plan":
            execution_plan(args.base, args.head, comprehensive=args.comprehensive)
        elif args.action == "prepare":
            if not args.revision:
                parser.error("--revision is required")
            prepare(args.revision)
        elif args.action == "record":
            if not args.revision:
                parser.error("--revision is required")
            record(args.revision)
        else:
            enforce(args.base_result, args.head_result)
        return 0
    except (OSError, ValueError, KeyError, TypeError, subprocess.CalledProcessError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
