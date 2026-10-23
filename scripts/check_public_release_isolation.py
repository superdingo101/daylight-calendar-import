"""Block hosted-service surfaces from the public HACS release ZIP.

This conservative text-marker check is a defense in depth, not a substitute
for reviewing the behavior and files proposed for main.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re
import sys
from tempfile import TemporaryDirectory
from zipfile import ZipFile

from scripts.build_hacs_release import INTEGRATION, build

# Deliberately allow the existing *self-hosted* Direct IMAP copy. Do not
# allow hosted-service client code to ship merely because it is dormant.
HOSTED_MARKERS = (
    ("hosted-service marker", re.compile(r"(?<!self-)(?<!self_)hosted", re.IGNORECASE)),
    ("Daylight Cloud name", re.compile(r"daylight[\s_-]*cloud", re.IGNORECASE)),
    (
        "cloud service surface",
        re.compile(
            r"\bcloud[\s_-]*(?:account|api|backend|billing|client|connection|"
            r"enroll(?:ment)?|installation|integration|parser|plan|portal|"
            r"provider|settings|subscription|transport)\b",
            re.IGNORECASE,
        ),
    ),
)

# Images/fonts are permitted assets, but cannot be inspected for visual text
# by this textual check. Other non-UTF-8 files fail closed.
BINARY_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".avif", ".woff", ".woff2", ".ttf", ".otf"}


def scan_archive(archive_path: Path) -> list[str]:
    """Report forbidden paths/content within the artifact users install."""
    violations: list[str] = []
    with ZipFile(archive_path) as archive:
        for entry in archive.infolist():
            name = entry.filename
            if entry.is_dir():
                violations.append(f"{name}: unexpected directory entry")
                continue

            for description, pattern in HOSTED_MARKERS:
                if pattern.search(name):
                    violations.append(f"{name}: forbidden {description} in file path")

            if Path(name).suffix.lower() in BINARY_SUFFIXES:
                continue

            try:
                text = archive.read(entry).decode("utf-8-sig")
            except UnicodeDecodeError:
                violations.append(f"{name}: non-UTF-8 release file cannot be inspected")
                continue

            for line_number, line in enumerate(text.splitlines(), start=1):
                for description, pattern in HOSTED_MARKERS:
                    if pattern.search(line):
                        violations.append(f"{name}:{line_number}: forbidden {description}")
    return violations


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--archive",
        type=Path,
        help="Check an already-built, validated HACS ZIP (as in Publish HACS Release)",
    )
    args = parser.parse_args()

    if args.archive:
        violations = scan_archive(args.archive)
    else:
        root = Path(__file__).resolve().parents[1]
        manifest = json.loads((root / INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
        with TemporaryDirectory() as directory:
            archive_path = Path(directory) / "daylight-calendar-import.zip"
            build(root, archive_path, manifest["version"])
            violations = scan_archive(archive_path)

    if violations:
        print("Public release isolation FAILED:", file=sys.stderr)
        for violation in violations:
            print(f"  {violation}", file=sys.stderr)
        print(
            "Keep hosted integration changes on integration/hosted-v1; "
            "do not include them in a public release.",
            file=sys.stderr,
        )
        return 1
    print("Public release isolation PASS: no recognized hosted-service surfaces in HACS ZIP.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
