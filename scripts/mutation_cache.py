"""Fingerprint inputs that mutmut 3.8.0 does not invalidate by source-function hash.

Cached verdicts are reusable only when the test environment and fixtures match.
Production Python changes are handled by mutmut's own per-function hashes.
"""

from __future__ import annotations

import hashlib
import json
import platform
import subprocess
import sys
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
        and not (path.startswith(SOURCE) and path.endswith(".py"))
        and not path.endswith((".md", ".rst"))
        and not path.startswith((".git/", "docs/"))
    ]


def fingerprint() -> str:
    digest = hashlib.sha256()
    digest.update(f"mutation-cache-v{CACHE_SCHEMA}|python={sys.version_info[:2]}|os={platform.system()}|mutmut=3.8.0".encode())
    for path in sorted(tracked_inputs()):
        digest.update(b"\0")
        digest.update(path.as_posix().encode())
        digest.update(b"\0")
        digest.update(path.read_bytes() if path.is_file() else b"<missing>")
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
