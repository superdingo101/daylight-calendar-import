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
import shutil
import platform
import re
import subprocess
import sys
import tokenize
import tomllib
import yaml
from pathlib import Path

CACHE_SCHEMA = 1
CACHE_META = Path("mutants/mutation-cache-provenance.json")
STATS = Path("mutants/mutmut-stats.json")
SOURCE = "custom_components/daylight_calendar_import/"
FRONTEND = SOURCE + "frontend/"
# Repository guidance and CI-only helper tests cannot affect behavior
# of the production integration during the mutmut runner.
CACHE_HELPERS = {
    "AGENTS.md",
    "scripts/mutation_cache.py",
    "tests/test_mutation_ci_strategy.py",
    "tests/test_mutation_cache_reuse.py",
}
COMMIT_SHA = re.compile(r"[0-9a-f]{40}\Z")
VERSION_ONLY_FILES = {
    "custom_components/daylight_calendar_import/manifest.json",
    "pyproject.toml",
}


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
    "Restore rebuilt PR mutation baseline",
    "Validate rebuilt PR mutation baseline",
    "Restore main mutation baseline",
    "Validate main mutation baseline",
    "Require reusable mutation baseline",
}


def _same_test_execution_workflow(old: bytes, new: bytes) -> bool:
    """Accept cache plumbing updates, never changes to tests/mutmut invocation."""
    try:
        original, proposed = [yaml.safe_load(contents) for contents in (old, new)]
        for workflow in (original, proposed):
            workflow["jobs"]["mutation"].pop("timeout-minutes", None)
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



def _environment_fingerprint() -> str:
    digest = hashlib.sha256()
    digest.update(
        f"mutation-cache-v{CACHE_SCHEMA}|python={platform.python_version()}|"
        f"os={platform.system()}|mutmut=3.8.0".encode()
    )
    installed = sorted(
        (dist.metadata["Name"].lower(), dist.version)
        for dist in metadata.distributions()
        if dist.metadata.get("Name")
    )
    digest.update(json.dumps(installed, separators=(",", ":")).encode())
    return digest.hexdigest()


def _version_independent_content(path: Path, raw: bytes) -> bytes:
    """Ignore version *labels*, never dependencies or test configuration.

    Release preparation updates the integration/packaging version together.
    Neither value changes the code exercised by the mutation test runner.
    Parse both formats so every other field must match semantically; malformed
    or unsupported metadata is compared byte-for-byte and fails closed.
    """
    name = path.as_posix()
    if name not in VERSION_ONLY_FILES:
        return raw
    try:
        if name == "pyproject.toml":
            doc = tomllib.loads(raw.decode("utf-8"))
            project = doc.get("project")
            if not isinstance(project, dict) or not isinstance(project.get("version"), str):
                return raw
            project.pop("version")
        else:
            doc = json.loads(raw)
            if not isinstance(doc, dict) or not isinstance(doc.get("version"), str):
                return raw
            doc.pop("version")
        return json.dumps(doc, sort_keys=True, separators=(",", ":")).encode()
    except (UnicodeError, ValueError, TypeError):
        return raw


def _comparison_content(path: Path, raw: bytes) -> bytes:
    """Hash the behavior relevant to mutation testing, not cache plumbing."""
    if path.as_posix() in VERSION_ONLY_FILES:
        return _version_independent_content(path, raw)
    if path.as_posix() == ".github/workflows/mutation.yml":
        try:
            workflow = yaml.safe_load(raw)
            workflow["jobs"]["mutation"].pop("timeout-minutes", None)
            steps = workflow["jobs"]["mutation"]["steps"]
            workflow["jobs"]["mutation"]["steps"] = [
                step for step in steps if step.get("name") not in _CACHE_ONLY_STEPS
            ]
            for step in workflow["jobs"]["mutation"]["steps"]:
                if step.get("uses", "").startswith("actions/checkout@"):
                    options = step.get("with", {})
                    options.pop("fetch-depth", None)
                    if not options:
                        step.pop("with", None)
            return json.dumps(workflow, sort_keys=True).encode()
        except (AttributeError, KeyError, TypeError, ValueError, yaml.YAMLError):
            return raw
    return _snapshot_content(path, raw)


def _cache_files() -> dict[str, str]:
    files = {}
    for path in tracked_inputs():
        if _frontend_asset(path) or path.as_posix() in CACHE_HELPERS:
            continue
        files[path.as_posix()] = hashlib.sha256(
            _comparison_content(path, path.read_bytes())
        ).hexdigest()
    return files


def _snapshot_signature(files: dict[str, str], environment: str) -> str:
    return hashlib.sha256(
        json.dumps({"files": files, "environment": environment}, sort_keys=True).encode()
    ).hexdigest()


def _compare_files(before: set[Path], same: callable) -> tuple[bool, list[str], list[str]]:
    after = set(tracked_inputs())
    added_tests = []
    added_sources = []
    for path in before | after:
        name = path.as_posix()
        if _frontend_asset(path) or name in CACHE_HELPERS:
            continue
        if path not in before:
            if not _independent_addition(path):
                print(f"Mutation cache rejected: new shared input {name}")
                return False, [], []
            if name.startswith("tests/"):
                added_tests.append(name)
            elif name.startswith(SOURCE):
                added_sources.append(name)
            continue
        if path not in after:
            print(f"Mutation cache rejected: removed cached input {name}")
            return False, [], []
        if not same(path):
            print(f"Mutation cache rejected: changed behavior-dependent input {name}")
            return False, [], []
    return True, sorted(added_tests), sorted(added_sources)


def _legacy_delta(commit: str) -> tuple[bool, list[str], list[str]]:
    def same(path: Path) -> bool:
        old = _historical_content(commit, path)
        now = path.read_bytes()
        if old == now:
            return True
        if path.as_posix() in VERSION_ONLY_FILES:
            return _version_independent_content(path, old) == _version_independent_content(path, now)
        if path.as_posix() == ".github/workflows/mutation.yml":
            return _same_test_execution_workflow(old, now)
        return (
            path.as_posix().startswith(SOURCE)
            and path.suffix == ".py"
            and _snapshot_content(path, old) == _snapshot_content(path, now)
        )

    return _compare_files(set(tracked_inputs(commit)), same)


def _manifest_delta(saved: dict) -> tuple[bool, list[str], list[str]]:
    files = saved.get("files")
    environment = saved.get("environment")
    if not isinstance(files, dict) or not isinstance(environment, str):
        return False, [], []
    if (
        saved.get("signature") != _snapshot_signature(files, environment)
        or environment != _environment_fingerprint()
        or any(
            not isinstance(path, str) or not isinstance(sha, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sha)
            for path, sha in files.items()
        )
    ):
        return False, [], []

    def same(path: Path) -> bool:
        name = path.as_posix()
        current = path.read_bytes()
        normalized_sha = hashlib.sha256(_comparison_content(path, current)).hexdigest()
        if normalized_sha == files[name]:
            return True
        # Previously stamped PR caches hashed these files raw. Accept a
        # byte-identical old file, or a version-only change if the original
        # Git snapshot is still accessible.
        if name in VERSION_ONLY_FILES:
            if hashlib.sha256(current).hexdigest() == files[name]:
                return True
            commit = saved.get("source_commit", "")
            if isinstance(commit, str) and COMMIT_SHA.fullmatch(commit):
                try:
                    old = _historical_content(commit, path)
                except subprocess.CalledProcessError:
                    return False
                return (
                    hashlib.sha256(old).hexdigest() == files[name]
                    and _version_independent_content(path, old)
                    == _version_independent_content(path, current)
                )
        return False

    return _compare_files(set(map(Path, files)), same)


def cache_validation() -> tuple[bool, list[str], list[str]]:
    try:
        saved = json.loads(CACHE_META.read_text(encoding="utf-8"))
        if not (
            isinstance(saved, dict) and saved.get("schema") == CACHE_SCHEMA
            and isinstance(saved.get("fingerprint"), str)
            and STATS.is_file() and any(Path("mutants").rglob("*.meta"))
        ):
            print("Mutation cache rejected: missing metadata, stats or mutant verdicts")
            return False, [], []
        if saved["fingerprint"] == fingerprint():
            return True, [], []
        if "files" in saved:
            return _manifest_delta(saved)

        # First shared main cache, created before file-level manifests existed.
        # Verify its exact original full-repository fingerprint before reuse.
        stats = json.loads(STATS.read_text(encoding="utf-8"))
        commit = saved.get("source_commit") or stats.get("git_commit")
        if not isinstance(commit, str) or not COMMIT_SHA.fullmatch(commit):
            print("Mutation cache rejected: legacy baseline has no valid commit SHA")
            return False, [], []
        if saved["fingerprint"] != fingerprint(commit):
            print("Mutation cache rejected: legacy source snapshot/environment fingerprint differs")
            return False, [], []
        return _legacy_delta(commit)
    except (OSError, ValueError, KeyError, subprocess.CalledProcessError) as error:
        print(f"Mutation cache rejected: cannot inspect provenance ({type(error).__name__})")
        return False, [], []


def validate() -> bool:
    return cache_validation()[0]


def _invalidate_old_non_kills() -> int:
    """Retest decided non-kills whenever the test-to-function mapping grows.

    New tests and new source modules can make previously surviving or
    uncovered mutants killable. Preserve existing kills, but clear all old
    non-killed verdicts before mutmut re-evaluates the changed associations.
    """
    reset = 0
    for path in Path("mutants").rglob("*.meta"):
        data = json.loads(path.read_text(encoding="utf-8"))
        verdicts = data["exit_code_by_key"]
        for name, code in verdicts.items():
            if code is not None and code not in {1, 3, 34, 37}:
                verdicts[name] = None
                reset += 1
        path.write_text(json.dumps(data, indent=4), encoding="utf-8")
    return reset


def _invalidate_stale_stats_for_new_sources(new_sources: list[str]) -> None:
    """New production functions need test mappings from existing tests.

    mutmut 3.8.0's incremental statistics collection discovers new tests only.
    Removing the stats cache forces a fresh full association collection while
    retaining existing per-mutant metadata and verdicts for reuse.
    """
    if new_sources:
        STATS.unlink(missing_ok=True)
        print(f"{len(new_sources)} new production modules; rebuilding test associations")


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in {"validate", "stamp"}:
        print("Usage: python scripts/mutation_cache.py [validate|stamp]", file=sys.stderr)
        return 2
    if sys.argv[1] == "validate":
        valid, new_tests, new_sources = cache_validation()
        if not valid:
            print("Mutation cache missing or stale; discarding cached state")
            shutil.rmtree("mutants", ignore_errors=True)
        else:
            print("Valid mutation cache restored; reusing applicable results")
            # New source modules can change associations for *existing* tests,
            # so their refreshed mappings must also revisit decided non-kills.
            if new_tests or new_sources:
                reset = _invalidate_old_non_kills()
                print(
                    f"{len(new_tests)} new test files, {len(new_sources)} new source modules; "
                    f"reset {reset} cached non-killed verdicts"
                )
            _invalidate_stale_stats_for_new_sources(new_sources)
        return 0

    if not STATS.is_file() or not any(Path("mutants").rglob("*.meta")):
        print("Cannot stamp missing mutation state", file=sys.stderr)
        return 1
    CACHE_META.parent.mkdir(exist_ok=True, parents=True)
    files = _cache_files()
    environment = _environment_fingerprint()
    CACHE_META.write_text(
        json.dumps({
            "schema": CACHE_SCHEMA,
            "fingerprint": fingerprint(),
            "source_commit": subprocess.check_output(
                ["git", "rev-parse", "HEAD"], text=True,
            ).strip(),
            "environment": environment,
            "files": files,
            "signature": _snapshot_signature(files, environment),
        }, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
