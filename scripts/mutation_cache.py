"""Fingerprint inputs that mutmut 3.8.0 does not invalidate by source-function hash.

Cached verdicts are reusable only when the test environment and fixtures match.
Production function bodies are handled by mutmut's own per-function hashes;
module-level imports/constants and class attributes are tracked separately.
"""

from __future__ import annotations

import ast
import hashlib
import json
import platform
import subprocess
import sys
import tokenize
from pathlib import Path

CACHE_SCHEMA = 1
CACHE_META = Path("mutants/mutation-cache-provenance.json")
STATS = Path("mutants/mutmut-stats.json")
SOURCE = "custom_components/daylight_calendar_import/"


def tracked_inputs() -> list[Path]:
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
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


def fingerprint() -> str:
    digest = hashlib.sha256()
    digest.update(f"mutation-cache-v{CACHE_SCHEMA}|python={sys.version_info[:2]}|os={platform.system()}|mutmut=3.8.0".encode())
    for path in sorted(tracked_inputs()):
        digest.update(b"\0")
        digest.update(path.as_posix().encode())
        digest.update(b"\0")
        digest.update(_input_content(path))
    return digest.hexdigest()


def validate() -> bool:
    try:
        saved = json.loads(CACHE_META.read_text(encoding="utf-8"))
        return (
            saved == {"schema": CACHE_SCHEMA, "fingerprint": fingerprint()}
            and STATS.is_file()
            and any(Path("mutants").rglob("*.meta"))
        )
    except (OSError, ValueError, subprocess.CalledProcessError):
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
        json.dumps({"schema": CACHE_SCHEMA, "fingerprint": fingerprint()}, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
