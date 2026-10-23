"""Prevent direct inclusion of hosted integration branch commits in public history.

Git ancestry is a provenance check, not semantic feature classification. A
squash, cherry-pick, copied patch or rewritten commit gets a different SHA and
still needs ordinary review before merging into public main.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


DEFAULT_HOSTED_REF = "refs/remotes/origin/integration/hosted-v1"
DEFAULT_MAIN_REF = "refs/remotes/origin/main"


def exclusive_commits(root: Path, head: str, base: str) -> set[str]:
    """Return commits reachable from head but not from base, failing closed."""
    output = subprocess.run(
        ["git", "rev-list", head, f"^{base}"],
        cwd=root,
        capture_output=True,
        text=True,
        check=True,
    )
    return set(output.stdout.splitlines())


def hosted_commits_in_candidate(
    root: Path, *, candidate: str, base: str, hosted: str
) -> set[str]:
    """Detect exact commits shared by candidate delta and hosted branch delta."""
    return exclusive_commits(root, candidate, base) & exclusive_commits(root, hosted, base)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--candidate", default="HEAD")
    parser.add_argument("--base", default=DEFAULT_MAIN_REF)
    parser.add_argument("--hosted", default=DEFAULT_HOSTED_REF)
    args = parser.parse_args()

    try:
        overlap = hosted_commits_in_candidate(
            args.repo,
            candidate=args.candidate,
            base=args.base,
            hosted=args.hosted,
        )
    except subprocess.CalledProcessError as exc:
        print("Public release isolation FAILED: required Git history unavailable.", file=sys.stderr)
        print(exc.stderr.strip(), file=sys.stderr)
        return 1

    if overlap:
        print("Public release isolation FAILED: hosted integration commits in public candidate:", file=sys.stderr)
        for sha in sorted(overlap):
            print(f"  {sha}", file=sys.stderr)
        print(
            "Target hosted integration work to integration/hosted-v1, not main. "
            "This cannot detect squashed, copied, or rewritten hosted changes.",
            file=sys.stderr,
        )
        return 1

    print("Public release isolation PASS: no shared hosted-only commit ancestry.")
    print("Review still required: this is not semantic code or release-content verification.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
