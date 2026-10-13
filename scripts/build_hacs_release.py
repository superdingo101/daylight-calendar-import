"""Build and verify the HACS integration ZIP from tracked integration files."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

INTEGRATION = Path("custom_components/daylight_calendar_import")
ASSET_NAME = "daylight-calendar-import.zip"
REQUIRED = {"__init__.py", "manifest.json"}


def tracked_files(root: Path) -> dict[str, Path]:
    """Only ship files that belong to the checked-in integration tree."""
    result = subprocess.run(
        ["git", "ls-files", "-z", "--", INTEGRATION.as_posix()],
        cwd=root,
        check=True,
        capture_output=True,
    )
    files: dict[str, Path] = {}
    for encoded_path in filter(None, result.stdout.split(b"\0")):
        source = root / os.fsdecode(encoded_path)
        relative = source.relative_to(root / INTEGRATION).as_posix()
        if source.is_symlink() or not source.is_file():
            raise ValueError(f"Integration contains a symlink or missing file: {source}")
        if relative in files or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts:
            raise ValueError(f"Unsafe or duplicate archive member: {relative}")
        files[relative] = source
    if not REQUIRED.issubset(files):
        raise ValueError(f"Missing required integration files: {sorted(REQUIRED - files.keys())}")
    return files


def check_metadata(root: Path, version: str) -> None:
    hacs = json.loads((root / "hacs.json").read_text(encoding="utf-8"))
    manifest = json.loads((root / INTEGRATION / "manifest.json").read_text(encoding="utf-8"))
    if hacs.get("zip_release") is not True or hacs.get("filename") != ASSET_NAME:
        raise ValueError("hacs.json does not select the release ZIP")
    if hacs.get("hide_default_branch") is not True:
        raise ValueError("HACS must hide the default branch, which has no release ZIP")
    if manifest.get("domain") != INTEGRATION.name or manifest.get("version") != version:
        raise ValueError("Integration domain or manifest version does not match the release")


def verify_archive(archive_path: Path, files: dict[str, Path]) -> None:
    """Check that HACS extraction produces exactly the tracked integration tree."""
    with ZipFile(archive_path) as archive:
        infos = archive.infolist()
        names = [info.filename for info in infos]
        if sorted(names) != sorted(files) or len(names) != len(set(names)):
            raise ValueError("Release ZIP does not contain exactly the tracked integration files")
        for info in infos:
            path = PurePosixPath(info.filename)
            if path.is_absolute() or ".." in path.parts or info.is_dir():
                raise ValueError(f"Unsafe ZIP member: {info.filename}")
            if archive.read(info) != files[info.filename].read_bytes():
                raise ValueError(f"ZIP content mismatch: {info.filename}")
        if archive.testzip() is not None:
            raise ValueError("Release ZIP checksum validation failed")


def build(root: Path, destination: Path, version: str) -> None:
    root = root.resolve()
    check_metadata(root, version)
    files = tracked_files(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(destination, "w") as archive:
        for name, source in sorted(files.items()):
            # Fixed timestamps and permissions keep release packages reproducible.
            info = ZipInfo(name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, source.read_bytes())
    verify_archive(destination, files)
    print(f"Validated {destination} ({len(files)} integration files, version {version})")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="Manifest version without v prefix")
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, default=Path(ASSET_NAME))
    args = parser.parse_args()
    build(args.root, args.output, args.version)


if __name__ == "__main__":
    main()
