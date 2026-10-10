"""Fingerprint inputs that mutmut 3.8.0 does not invalidate by source-function hash.

Cached verdicts are reusable only when the test environment and fixtures match.
Production function bodies are handled by mutmut's own per-function hashes;
module-level imports/constants and class attributes are tracked separately.
"""

from __future__ import annotations

import ast
import hashlib
import io
from importlib import metadata
import json
import platform
import re
import subprocess
import sys
import tokenize
import yaml
from pathlib import Path

CACHE_SCHEMA = 1
CACHE_META = Path("mutants/mutation-cache-provenance.json")
STATS = Path("mutants/mutmut-stats.json")
SOURCE = "custom_components/daylight_calendar_import/"
FRONTEND = SOURCE + "frontend/"
# CI-only helpers and their tests do not affect mutation verdicts for the
# production package; pytest still exercises them on every mutmut run.
CACHE_HELPERS = {"scripts/mutation_cache.py", "tests/test_mutation_ci_strategy.py"}
COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")


def tracked_inputs(commit: str | None = None) -> list[Path]:
    command = (
        ["git", "ls-files", "-z"] if commit is None
        else ["git", "ls-tree", "-r", "--name-only", "-z", commit]
    )
    paths = subprocess.check_output(command).decode().split("\0")
    return [
        Path(path) for path in paths
        if path
        and path not in {"README.md", "LICENSE"}
        and not path.startswith((".git/", "docs/"))
    ]


class _HideFunctionBodies(ast.NodeTransformer):
    """Keep imports, constants, decorators and signatures; ignore function bodies."""

    def visit_FunctionDef(self, node: ast.FunctionDef) -> ast.AST:
        node.body = [ast.Pass()]
        return node

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> ast.AST:
        node.body = [ast.Pass()]
        return node


def _input_content(path: Path) -> bytes:
    if path.as_posix().startswith(SOURCE) and path.suffix == ".py" and path.is_file():
        # Mutmut already hashes individual functions. Imports, module-level
        # expressions, and class constants still affect tests, and need their
        # own conservative invalidation signal.
        try:
            with tokenize.open(path) as source:
                module = ast.parse(source.read())
            outline = _HideFunctionBodies().visit(module)
            return ast.dump(outline, include_attributes=False).encode("utf-8")
        except (SyntaxError, UnicodeError, OSError, LookupError):
            return path.read_bytes()
    return path.read_bytes() if path.is_file() else b"<missing>"


def _historical_content(commit: str, path: Path) -> bytes:
    return subprocess.check_output(["git", "show", f"{commit}:{path.as_posix()}"])


def _outline(source: bytes) -> bytes:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(source).readline)
        module = ast.parse(source.decode(encoding))
        return ast.dump(_HideFunctionBodies().visit(module), include_attributes=False).encode()
    except (SyntaxError, UnicodeError, LookupError):
        return source


def _snapshot_content(path: Path, data: bytes) -> bytes:
    return _outline(data) if path.as_posix().startswith(SOURCE) and path.suffix == ".py" else data


def fingerprint(commit: str | None = None) -> str:
    digest = hashlib.sha256()
    digest.update(
        f"mutation-cache-v{CACHE_SCHEMA}|python={platform.python_version()}|"
        f"os={platform.system()}|mutmut=3.8.0".encode()
    )
    # Ranged dependencies in requirements_test.txt can resolve to newer
    # packages without the requirements file changing. Their exact installed
    # versions are part of the mutation test environment.
    installed = sorted(
        (distribution.metadata["Name"].lower(), distribution.version)
        for distribution in metadata.distributions()
        if distribution.metadata.get("Name")
    )
    digest.update(json.dumps(installed, separators=(",", ":")).encode())
    for path in sorted(tracked_inputs(commit)):
        digest.update(b"\0")
        digest.update(path.as_posix().encode())
        digest.update(b"\0")
        digest.update(
            _input_content(path) if commit is None
            else _snapshot_content(path, _historical_content(commit, path))
        )
    return digest.hexdigest()


def _independent_addition(path: Path) -> bool:
    name = path.as_posix()
    return (
        (name.startswith("tests/") and path.name.startswith("test_") and path.suffix == ".py")
        or (name.startswith(SOURCE) and path.suffix == ".py" and path.name != "__init__.py")
    )


def _frontend_asset(path: Path) -> bool:
    return path.as_posix().startswith(FRONTEND) and path.suffix in {
        ".js", ".mjs", ".css", ".html", ".map",
    }


_CACHE_ONLY_STEPS = {
    "Restore mutation cache",
    "Validate mutation cache provenance",
    "Restore PR mutation cache",
    "Validate PR mutation cache",
    "Restore main mutation baseline",
    "Validate main mutation baseline",
}


def _same_test_execution_workflow(old: bytes, new: bytes) -> bool:
    """Accept cache plumbing updates, never changes to tests/mutmut invocation."""
    try:
        original, proposed = [yaml.safe_load(contents) for contents in (old, new)]
        for workflow in (original, proposed):
            steps = workflow["jobs"]["mutation"]["steps"]
            workflow["jobs"]["mutation"]["steps"] = [
                step for step in steps if step.get("name") not in _CACHE_ONLY_STEPS
            ]
            for step in workflow["jobs"]["mutation"]["steps"]:
                if step.get("uses", "").startswith("actions/checkout@"):
                    settings = step.get("with", {})
                    settings.pop("fetch-depth", None)
                    if not settings:
                        step.pop("with", None)
        return original == proposed
    except (AttributeError, TypeError, KeyError, ValueError, yaml.YAMLError):
        return False


def _safe_delta(commit: str) -> bool:
    before = set(tracked_inputs(commit))
    after = set(tracked_inputs())
    for path in before | after:
        name = path.as_posix()
        if _frontend_asset(path) or name in CACHE_HELPERS:
            continue
        if path not in before:
            if not _independent_addition(path):
                return False
            continue
        if path not in after:
            return False
        old = _historical_content(commit, path)
        now = path.read_bytes()
        if old == now:
            continue
        if name == ".github/workflows/mutation.yml":
            if _same_test_execution_workflow(old, now):
                continue
            return False
        # Mutmut already invalidates source function-body changes by hash.
        if name.startswith(SOURCE) and path.suffix == ".py":
            if _snapshot_content(path, old) == _snapshot_content(path, now):
                continue
        return False
    return True


def validate() -> bool:
    try:
        saved = json.loads(CACHE_META.read_text(encoding="utf-8"))
        if not (
            isinstance(saved, dict) and saved.get("schema") == CACHE_SCHEMA
            and isinstance(saved.get("fingerprint"), str)
            and STATS.is_file() and any(Path("mutants").rglob("*.meta"))
        ):
            return False
        if saved["fingerprint"] == fingerprint():
            return True

        # Existing nightly baselines only contain a whole-tree fingerprint.
        # Recover the exact original Git snapshot and *verify* its fingerprint
        # before allowing strictly scoped additions. This preserves the
        # baseline built before this change without accepting stale metadata.
        stats = json.loads(STATS.read_text(encoding="utf-8"))
        commit = saved.get("source_commit") or stats.get("git_commit")
        if not isinstance(commit, str) or not COMMIT_SHA.fullmatch(commit):
            return False
        if saved["fingerprint"] != fingerprint(commit):
            return False
        return _safe_delta(commit)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError):
        return False


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"validate", "stamp"}:
        print("Usage: python scripts/mutation_cache.py [validate|stamp]", file=sys.stderr)
        return 2
    if sys.argv[1] == "validate":
        if not validate():
            print("Mutation cache missing or stale; discarding cached state")
            import shutil
            shutil.rmtree("mutants", ignore_errors=True)
        else:
            print("Valid mutation cache restored; reusing applicable results")
        return 0

    if not STATS.is_file() or not any(Path("mutants").rglob("*.meta")):
        print("Cannot stamp missing mutation state", file=sys.stderr)
        return 1
    CACHE_META.parent.mkdir(exist_ok=True, parents=True)
    CACHE_META.write_text(
        json.dumps({
            "schema": CACHE_SCHEMA,
            "fingerprint": fingerprint(),
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True,
            ).strip(),
        }, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
