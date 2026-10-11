"""Fail-closed, PR-local reuse of mutmut 3.8.0's native mutants/ state.

Reuse is limited to the same PR, identical production/config/dependency inputs,
and tests that are byte-identical or have only *new, undecorated top-level*
test functions appended after unchanged existing AST nodes. With new tests,
previously killed mutants remain killed; every non-killed verdict is reset.
Any uncertain change causes a clean run. Never use this for release/nightly gates.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path("mutants")
PROVENANCE = ROOT / ".pr-mutation-provenance.json"
SCOPE = Path(".pr-mutation-scope.json")
VERSION = 1
# Pinned in requirements_test.txt. A format/semantics change needs a new version.
MUTMUT_VERSION = "3.8.0"
KILLED = {1, 3, 37}  # pytest failed (1, 3) or static type checker rejected (37)


def tracked_paths() -> list[Path]:
    paths = subprocess.check_output(["git", "ls-files", "-z"]).split(b"\0")
    return [Path(os.fsdecode(p)) for p in paths if p]


def hash_inputs(paths: list[Path]) -> tuple[str, dict[str, list[str]]]:
    """Hash every tracked non-test input and record the structure of Python tests.

    Including docs/workflows in the hash is deliberately conservative: a false
    cache miss costs time, but a false hit would silently weaken CI quality.
    """
    digest = hashlib.sha256()
    tests: dict[str, list[str]] = {}
    for path in sorted(paths, key=lambda p: p.as_posix()):
        if not path.is_file():
            raise ValueError(f"Missing tracked input: {path}")
        name = path.as_posix()
        body = path.read_bytes()
        if name.startswith("tests/") and name.endswith(".py"):
            tree = ast.parse(body, filename=name)
            tests[name] = [ast.dump(node, include_attributes=False)
                           for node in tree.body]
            continue
        digest.update(len(name.encode()).to_bytes(8, "big"))
        digest.update(name.encode())
        digest.update(hashlib.sha256(body).digest())
    return digest.hexdigest(), tests


def environment_hash() -> str:
    """Reject caches built with another interpreter or installed dependency set."""
    frozen = subprocess.check_output(
        [sys.executable, "-m", "pip", "freeze", "--all"], text=True,
    )
    return hashlib.sha256((sys.version + "\n" + frozen).encode()).hexdigest()


def context(pr: str) -> dict:
    if not pr.isdecimal() or int(pr) < 1:
        raise ValueError("Invalid pull request number")
    scope = json.loads(SCOPE.read_text(encoding="utf-8"))
    if scope["mode"] not in {"scoped", "full"}:
        raise ValueError("No active mutation scope")
    if not isinstance(scope["modules"], list):
        raise ValueError("Invalid module scope")
    inputs, tests = hash_inputs(tracked_paths())
    return {
        "schema": VERSION,
        "mutmut": MUTMUT_VERSION,
        "pr": int(pr),
        "scope": {"mode": scope["mode"], "modules": scope["modules"]},
        "inputs_sha256": inputs,
        "environment_sha256": environment_hash(),
        "tests": tests,
    }


def previously_bound_names(nodes: list[ast.stmt]) -> set[str] | None:
    """Conservatively detect names already exposed by existing test modules.

    Existing pytest callables can arrive via imports or assignments, not just
    definitions. Wildcard imports and dynamic namespace operations can bind
    arbitrary test names, so they force a clean run on test additions.
    Inspecting nested scopes may reject safe reuse but cannot create unsafe hits.
    """
    dynamic_names = {"globals", "locals", "vars", "exec", "eval", "setattr"}
    names: set[str] = set()
    for statement in nodes:
        for node in ast.walk(statement):
            # Python can install collected test names through a module dict
            # without ever binding an ast.Name(Store), e.g. globals()[name].
            # Conservatively fall back to uncached runs for such modules.
            if isinstance(node, ast.Name) and node.id in dynamic_names:
                return None
            if isinstance(node, ast.Attribute) and node.attr in (
                dynamic_names | {"__dict__"}
            ):
                return None
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                # getattr(module, "__dict__") can expose the writable module
                # namespace even without a literal __dict__ attribute node.
                if node.func.id == "getattr" and any(
                    isinstance(arg, ast.Constant) and arg.value == "__dict__"
                    for arg in node.args[1:]
                ):
                    return None
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                names.add(node.name)
            elif isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    if alias.name == "*":
                        return None
                    names.add(alias.asname or alias.name.split(".", 1)[0])
            elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
                names.add(node.id)
            elif isinstance(node, (ast.ExceptHandler, ast.MatchAs, ast.MatchStar)):
                if node.name:
                    names.add(node.name)
    return names


def additive(old: dict[str, list[str]], new: dict[str, list[str]]) -> bool:
    """Allow unchanged tests plus appended, standalone, undecorated test functions.

    No deleted/renamed files; no edits to existing function bodies, imports,
    fixtures, decorators, globals, or collection hooks. New test modules are
    conservatively rejected because imports/collection can change fixtures.
    """
    if old.keys() != new.keys():
        return False
    for name, old_nodes in old.items():
        current = new[name]
        if current[:len(old_nodes)] != old_nodes:
            return False
        if len(current) == len(old_nodes):
            continue
        # Inspect actual AST to validate only the appended nodes.  New
        # definitions must not shadow an existing test or execute expressions
        # during collection (decorators, defaults, runtime annotations).
        nodes = ast.parse(Path(name).read_bytes(), filename=name).body
        used_names = previously_bound_names(nodes[:len(old_nodes)])
        if used_names is None:
            return False  # A wildcard import may bind the proposed name.
        for node in nodes[len(old_nodes):]:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                return False
            if not node.name.startswith("test_") or node.decorator_list:
                return False
            if node.name in used_names or node.args.defaults or any(
                default is not None for default in node.args.kw_defaults
            ) or node.returns is not None or node.type_params:
                return False
            arguments = [
                *node.args.posonlyargs, *node.args.args, *node.args.kwonlyargs,
            ]
            arguments.extend(arg for arg in (node.args.vararg, node.args.kwarg) if arg)
            if any(arg.annotation is not None for arg in arguments):
                return False
            used_names.add(node.name)
    return True


def clear_cache(reason: str) -> None:
    print(f"Mutation cache: cold ({reason})")
    if ROOT.is_symlink():
        ROOT.unlink()
    elif ROOT.exists():
        shutil.rmtree(ROOT)


def reset_non_killed() -> tuple[int, int]:
    """In mutmut 3.8's .meta files, None means 'run this mutant again'."""
    total, reset = 0, 0
    for meta in ROOT.rglob("*.meta"):
        data = json.loads(meta.read_text(encoding="utf-8"))
        verdicts = data.get("exit_code_by_key")
        if not isinstance(verdicts, dict):
            raise ValueError(f"Invalid mutmut metadata: {meta}")
        for key, value in verdicts.items():
            total += 1
            if value is not None and value not in KILLED:
                verdicts[key] = None
                reset += 1
        data["exit_code_by_key"] = verdicts
        meta.write_text(json.dumps(data, indent=4), encoding="utf-8")
    return total, reset


def refresh_tests() -> None:
    """Mutmut's copy_src_dir skips existing paths: overwrite stale copied tests."""
    path = ROOT / "tests"
    if path.is_symlink():
        raise ValueError("Unexpected symlink at mutants/tests")
    if path.exists():
        shutil.rmtree(path)
    shutil.copytree(
        "tests", path,
        ignore=shutil.ignore_patterns("__pycache__", ".pytest_cache", "*.pyc"),
    )


def restore(pr: str) -> None:
    # Restore operations can read corrupt/untrusted old state; fail safely.
    try:
        current = context(pr)
        if ROOT.is_symlink() or not PROVENANCE.is_file():
            clear_cache("missing or invalid provenance")
            return
        old = json.loads(PROVENANCE.read_text(encoding="utf-8"))
        for key in ("schema", "mutmut", "pr", "scope",
                    "inputs_sha256", "environment_sha256"):
            if old.get(key) != current[key]:
                clear_cache(f"{key} changed")
                return
        if not (ROOT / "mutmut-stats.json").is_file():
            clear_cache("missing mutmut statistics")
            return
        if not isinstance(old.get("tests"), dict):
            clear_cache("missing test provenance")
            return
        if old["tests"] != current["tests"]:
            if not additive(old["tests"], current["tests"]):
                clear_cache("existing tests or fixtures changed")
                return
            total, reset = reset_non_killed()
            print(f"Mutation cache: added tests; reconsider {reset}/{total} non-killed mutants")
        else:
            print("Mutation cache: unchanged source and tests; reuse prior verdicts")
        refresh_tests()
        # Prevent an aborted run from being mistaken for a validated cache.
        PROVENANCE.unlink()
    except (OSError, ValueError, KeyError, SyntaxError, TypeError,
            json.JSONDecodeError, subprocess.CalledProcessError) as exc:
        clear_cache(f"unable to validate ({type(exc).__name__})")


def stamp(pr: str) -> None:
    # Called ONLY after mutmut run returned successfully; the score may still fail.
    current = context(pr)
    if not (ROOT / "mutmut-stats.json").is_file():
        raise ValueError("Cannot cache an incomplete mutmut run")
    meta = list(ROOT.rglob("*.meta"))
    if not meta:
        raise ValueError("Cannot cache missing mutation metadata")
    PROVENANCE.write_text(json.dumps(current, sort_keys=True), encoding="utf-8")
    print(f"Mutation cache: saved {len(meta)} source metadata files for PR #{pr}")


def main() -> None:
    cli = argparse.ArgumentParser()
    cli.add_argument("action", choices=["restore", "stamp"])
    cli.add_argument("--pr", required=True)
    args = cli.parse_args()
    if args.action == "restore":
        restore(args.pr)
    else:
        stamp(args.pr)


if __name__ == "__main__":
    main()
