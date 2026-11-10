"""Publish Mintlify documentation from the exact, validated GitHub release.

Stable releases advance docs-live. Published beta prereleases can advance only
an isolated docs-rehearsal branch when --rehearsal is explicitly selected.
Neither mode uses force pushes or development-branch snapshots.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
from pathlib import Path
from typing import Any

TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")
BETA_TAG_RE = re.compile(r"^v(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)-beta(0|[1-9]\d*)$")
PAGE_RE = re.compile(r"^[a-zA-Z0-9/_-]+$")
ASSET = "daylight-calendar-import.zip"
TARGET = "docs-live"
REHEARSAL_TARGET = "docs-rehearsal"


class PublicationError(ValueError):
    """A safe release publication preflight has failed."""


def _git(repo: Path, *args: str, expected_failure: bool = False) -> str:
    process = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    if process.returncode and not expected_failure:
        raise PublicationError(f"Git command failed: git {' '.join(args)}: {process.stderr.strip()}")
    return process.stdout.strip() if process.returncode == 0 else ""


def _ancestor(repo: Path, first: str, second: str) -> bool:
    process = subprocess.run(
        ["git", "-C", str(repo), "merge-base", "--is-ancestor", first, second],
        capture_output=True,
        check=False,
    )
    if process.returncode not in (0, 1):
        raise PublicationError("Could not verify Git ancestry")
    return process.returncode == 0


def check_release(release: dict[str, Any], *, rehearsal: bool = False) -> str:
    """Require a real published release of the correct type, with a HACS ZIP."""
    tag = release.get("tag_name")
    required = BETA_TAG_RE if rehearsal else TAG_RE
    if not isinstance(tag, str) or not required.fullmatch(tag):
        kind = "beta prerelease" if rehearsal else "stable"
        raise PublicationError(f"Only {kind} semantic-version release tags qualify")
    if (release.get("draft") is not False
            or release.get("prerelease") is not rehearsal
            or not release.get("published_at")):
        raise PublicationError("The GitHub release is not published with the required prerelease status")
    assets = release.get("assets")
    if not isinstance(assets, list) or not any(
        isinstance(asset, dict) and asset.get("name") == ASSET
        and isinstance(asset.get("size"), int) and asset["size"] > 0
        for asset in assets
    ):
        raise PublicationError("The published release has no nonempty HACS ZIP asset")
    return tag


def _navigation_pages(value: Any) -> list[str]:
    """Collect referenced pages while ignoring external navigation links."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [page for item in value for page in _navigation_pages(item)]
    if isinstance(value, dict):
        return [
            page for key in ("pages", "groups", "tabs", "anchors", "versions", "languages", "dropdowns")
            if key in value for page in _navigation_pages(value[key])
        ]
    return []


def _page_path(docs: Path, route: str) -> Path:
    if not PAGE_RE.fullmatch(route) or ".." in route.split("/"):
        raise PublicationError(f"Invalid Mintlify page route: {route!r}")
    for ext in (".mdx", ".md"):
        path = docs / (route + ext)
        if path.is_file():
            return path
    raise PublicationError(f"Navigation references a nonexistent docs page: {route}")


def check_site(root: Path, tag: str) -> list[Path]:
    """Check release/version alignment and publishable Mintlify navigation."""
    docs = root / "docs"
    config_path = docs / "docs.json"
    manifest_path = root / "custom_components/daylight_calendar_import/manifest.json"
    pyproject_path = root / "pyproject.toml"
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        pyproject = pyproject_path.read_text(encoding="utf-8")
    except (OSError, ValueError) as error:
        raise PublicationError("The release lacks valid docs.json or integration metadata") from error

    version = tag.removeprefix("v")
    pyproject_version = re.search(r'(?m)^version\s*=\s*"([^"]+)"\s*$', pyproject)
    if manifest.get("version") != version or not pyproject_version or pyproject_version.group(1) != version:
        raise PublicationError("Release tag, manifest version, and pyproject version do not match")
    if not isinstance(config, dict) or config.get("name") != "Daylight Calendar Import":
        raise PublicationError("Mintlify site configuration is missing or for the wrong product")
    nav = config.get("navigation")
    pages = _navigation_pages(nav)
    if not pages or len(pages) != len(set(pages)):
        raise PublicationError("Mintlify navigation must be nonempty and have unique page routes")
    if not {"introduction", "installation", "quickstart"}.issubset(pages):
        raise PublicationError("Mintlify is missing its introduction, installation, or quickstart")

    for key, asset in [
        ("favicon", config.get("favicon")),
        ("light logo", (config.get("logo") or {}).get("light")),
        ("dark logo", (config.get("logo") or {}).get("dark")),
    ]:
        if not isinstance(asset, str) or not asset.startswith("/") or not (docs / asset.lstrip("/")).is_file():
            raise PublicationError(f"Mintlify {key} asset is missing")

    files = [_page_path(docs, route) for route in pages]
    for page in files:
        source = page.read_text(encoding="utf-8")
        if not re.match(r"\A---\s*\n.*?\n---\s*\n", source, flags=re.DOTALL):
            raise PublicationError(f"Missing MDX frontmatter: {page.relative_to(root)}")
        # Check page links, not Markdown image asset links. Mint validates local
        # image resources and more complex/relative links separately.
        links = re.findall(r"(?<!!)\[[^\]]*\]\((/[^)#?]*)(?:[#?][^)]*)?\)", source)
        links += re.findall(r'\bhref=["\'](/[^"\'#?]*)', source)
        for link in links:
            if link != "/":
                _page_path(docs, link.lstrip("/"))
    return files


def inspect_ref(repo: Path, tag: str, *, target: str = TARGET) -> tuple[str, str | None]:
    """Verify the exact tag and non-rollback lineage of one approved branch."""
    if target not in (TARGET, REHEARSAL_TARGET):
        raise PublicationError("Unsupported documentation publication target")
    current = _git(repo, "rev-parse", "HEAD")
    tagged = _git(repo, "rev-parse", f"refs/tags/{tag}^{{commit}}")
    if current != tagged:
        raise PublicationError("The checked-out source is not the published release tag")
    _git(repo, "fetch", "--no-tags", "origin", "+refs/heads/main:refs/remotes/origin/main")
    if not _ancestor(repo, current, "refs/remotes/origin/main"):
        raise PublicationError("The release tag is not an ancestor of public main")

    remote = _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{target}")
    previous = remote.split()[0] if remote else None
    if previous and previous != current:
        _git(repo, "fetch", "--no-tags", "origin", f"+refs/heads/{target}:refs/remotes/origin/{target}")
        if not _ancestor(repo, previous, current):
            raise PublicationError(f"Refusing stale release, rollback, or divergent {target} history")
    return current, previous


def publish(repo: Path, release: dict[str, Any], *, check_only: bool = False,
            rehearsal: bool = False) -> str:
    """Preflight first; advance only the branch permitted for this release type."""
    tag = check_release(release, rehearsal=rehearsal)
    checked = check_site(repo, tag)
    target = REHEARSAL_TARGET if rehearsal else TARGET
    sha, previous = inspect_ref(repo, tag, target=target)
    if previous == sha:
        return f"Already published {tag} ({sha}); {len(checked)} pages verified"
    if check_only:
        return f"Validated {tag} ({sha}); {len(checked)} pages ready for publication"
    # No force push: Git itself additionally rejects concurrent non-fast-forward
    # updates even after the ancestry preflight.
    _git(repo, "push", "origin", f"{sha}:refs/heads/{target}")
    remote = _git(repo, "ls-remote", "--heads", "origin", f"refs/heads/{target}")
    if not remote or remote.split()[0] != sha:
        raise PublicationError(f"Publication returned without the intended {target} HEAD")
    return f"Published {tag} ({sha}) to {target}; {len(checked)} pages verified"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True, help="Checkout of exact released tag")
    parser.add_argument("--release-json", type=Path, required=True, help="Fresh GitHub API release response")
    parser.add_argument("--publish", action="store_true", help="Advance the allowed branch after preflight")
    parser.add_argument("--rehearsal", action="store_true", help="Accept only beta releases and target docs-rehearsal")
    args = parser.parse_args()
    try:
        release = json.loads(args.release_json.read_text(encoding="utf-8"))
        print(publish(args.repo.resolve(), release, check_only=not args.publish,
                      rehearsal=args.rehearsal))
    except (OSError, ValueError, PublicationError) as error:
        parser.exit(1, f"Docs publication blocked: {error}\n")


if __name__ == "__main__":
    main()
