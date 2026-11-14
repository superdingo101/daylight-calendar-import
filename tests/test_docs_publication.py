"""Release-documentation publisher regression tests.

All Git operations use disposable local repositories. No GitHub release,
network access, or production docs-live branch can be modified by this suite.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

from scripts.publish_docs_live import (
    PublicationError,
    check_release,
    check_site,
    publish,
    _page_path,
)


def run(*args: str, cwd: Path | None = None) -> str:
    result = subprocess.run(
        ["git", *args], cwd=cwd, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


def release(tag: str = "v0.9.0", *, draft: bool = False,
            prerelease: bool = False, include_zip: bool = True) -> dict:
    return {
        "tag_name": tag,
        "draft": draft,
        "prerelease": prerelease,
        "published_at": "2026-11-09T12:00:00Z",
        "assets": [
            {"name": "daylight-calendar-import.zip", "size": 12600}
        ] if include_zip else [],
    }


class DocsPublicationTest(unittest.TestCase):
    """Exercise exact-tag publication against local Git remotes."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.remote = root / "remote.git"
        self.work = root / "work"
        self.released = root / "released"

        run("init", "--bare", "--initial-branch=main", str(self.remote))
        run("init", "--initial-branch=main", str(self.work))
        run("config", "user.name", "Docs CI Test", cwd=self.work)
        run("config", "user.email", "docs-test@example.invalid", cwd=self.work)
        run("remote", "add", "origin", str(self.remote), cwd=self.work)
        self.write_version("0.9.0")
        self.write_site()
        self.commit("stable docs for version 0.9.0")
        run("tag", "v0.9.0", cwd=self.work)
        run("push", "-u", "origin", "main", "--tags", cwd=self.work)
        run("clone", str(self.remote), str(self.released))
        run("checkout", "--detach", "v0.9.0", cwd=self.released)

    def write_version(self, version: str) -> None:
        path = self.work / "custom_components/daylight_calendar_import"
        path.mkdir(parents=True, exist_ok=True)
        (path / "manifest.json").write_text(
            json.dumps({"version": version}) + "\n", encoding="utf-8",
        )
        (self.work / "pyproject.toml").write_text(
            f'[project]\nversion = "{version}"\n', encoding="utf-8",
        )

    def write_site(self) -> None:
        docs = self.work / "docs"
        (docs / "logo").mkdir(parents=True, exist_ok=True)
        (docs / "docs.json").write_text(
            json.dumps({
                "name": "Daylight Calendar Import",
                "navigation": {
                    "groups": [{"group": "Start", "pages": [
                        "introduction", "installation", "quickstart",
                    ]}],
                },
                "favicon": "/logo/icon.svg",
                "logo": {
                    "light": "/logo/daylight.svg",
                    "dark": "/logo/daylight-dark.svg",
                },
            }),
            encoding="utf-8",
        )
        for name in ("icon", "daylight", "daylight-dark"):
            (docs / "logo" / f"{name}.svg").write_text(
                '<svg xmlns="http://www.w3.org/2000/svg"/>\n',
                encoding="utf-8",
            )
        for route in ("introduction", "installation", "quickstart"):
            (docs / f"{route}.mdx").write_text(
                f"---\ntitle: {route}\n---\n\n# {route}\n",
                encoding="utf-8",
            )

    def commit(self, message: str) -> None:
        run("add", "-A", cwd=self.work)
        run("commit", "-m", message, cwd=self.work)

    def checkout_tag(self, tag: str) -> None:
        run("fetch", "--tags", "origin", cwd=self.released)
        run("checkout", "--detach", tag, cwd=self.released)

    def docs_live_sha(self) -> str | None:
        response = run(
            "ls-remote", "--heads", "origin", "refs/heads/docs-live",
            cwd=self.released,
        )
        return response.split()[0] if response else None

    def test_only_published_stable_releases_with_zip_qualify(self) -> None:
        self.assertEqual(check_release(release()), "v0.9.0")
        for candidate in (
            release("v0.9.0-beta1", prerelease=True),
            release(draft=True),
            release(prerelease=True),
            release(include_zip=False),
            release("v0.9.0-rc1"),
            {**release(), "published_at": None},
            {**release(), "assets": [{"name": "daylight-calendar-import.zip", "size": 0}]},
        ):
            with self.subTest(candidate=candidate), self.assertRaises(PublicationError):
                check_release(candidate)

    def test_docs_validation_rejects_missing_page_assets_and_wrong_version(self) -> None:
        self.assertEqual(len(check_site(self.released, "v0.9.0")), 3)
        (self.released / "docs/installation.mdx").unlink()
        with self.assertRaisesRegex(PublicationError, "nonexistent docs page"):
            check_site(self.released, "v0.9.0")
        (self.released / "docs/installation.mdx").write_text(
            "---\ntitle: Installation\n---\n# install\n", encoding="utf-8",
        )
        (self.released / "docs/logo/daylight-dark.svg").unlink()
        with self.assertRaisesRegex(PublicationError, "dark logo"):
            check_site(self.released, "v0.9.0")
        (self.released / "docs/logo/daylight-dark.svg").write_text("<svg/>")
        with self.assertRaisesRegex(PublicationError, "do not match"):
            check_site(self.released, "v0.9.1")

    def test_actual_versioned_release_navigation_publishes_safely(self) -> None:
        """Validate the authored docs.json and its actual page/link files."""
        source = Path(__file__).resolve().parents[1] / "docs"
        self.assertTrue((source / "releases/v0.9.0.md").is_file())
        shutil.copytree(source, self.released / "docs", dirs_exist_ok=True)
        config = json.loads((source / "docs.json").read_text(encoding="utf-8"))
        routes = [
            page
            for group in config["navigation"]["groups"]
            for page in group["pages"]
        ]
        self.assertIn("releases/v0.9.0", routes)
        self.assertEqual(len(check_site(self.released, "v0.9.0")), len(routes))
        self.assertIn("Validated", publish(self.released, release(), check_only=True))
        self.assertIsNone(self.docs_live_sha())

    def test_versioned_page_routes_do_not_allow_dot_segments_or_files(self) -> None:
        page_dir = self.released / "docs/releases"
        page_dir.mkdir(parents=True, exist_ok=True)
        (page_dir / "v0.9.0.md").write_text(
            "---\\ntitle: v0.9.0\\n---\\n# v0.9.0\\n", encoding="utf-8",
        )
        self.assertEqual(
            _page_path(self.released / "docs", "releases/v0.9.0"),
            page_dir / "v0.9.0.md",
        )
        for bad_route in (
            "../secrets", "releases/../secrets", "releases/./v0.9.0",
            "releases/v0.9.0/extra", "releases/v0.9.0.md",
            "releases/v0.9", "releases/v0.09.0", "releases/v0.9.0-beta2",
            "releases//v0.9.0", "/releases/v0.9.0",
            "https://example.com", "releases/.env",
        ):
            with self.subTest(route=bad_route), self.assertRaisesRegex(
                PublicationError, "Invalid Mintlify page route",
            ):
                _page_path(self.released / "docs", bad_route)

    def test_pinned_calendar_card_branding_passes_release_validation(self) -> None:
        config_path = self.work / "docs/docs.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        config["favicon"] = "https://docs.daylightcalendar.com/images/977.ico"
        config["logo"] = {
            "light": "https://docs.daylightcalendar.com/images/976.png",
            "dark": "https://docs.daylightcalendar.com/images/976.png",
        }
        config_path.write_text(json.dumps(config), encoding="utf-8")
        self.write_version("0.9.1")
        self.commit("ship stable docs with shared Card branding")
        run("tag", "v0.9.1", cwd=self.work)
        run("push", "origin", "main", "--tags", cwd=self.work)
        self.checkout_tag("v0.9.1")

        self.assertEqual(len(check_site(self.released, "v0.9.1")), 3)
        self.assertIn("Published v0.9.1", publish(self.released, release("v0.9.1")))
        self.assertEqual(self.docs_live_sha(), run(
            "rev-parse", "v0.9.1^{commit}", cwd=self.released,
        ))

    def test_unapproved_external_branding_is_rejected(self) -> None:
        config_path = self.released / "docs/docs.json"
        original = json.loads(config_path.read_text(encoding="utf-8"))
        allowed_logo = "https://docs.daylightcalendar.com/images/976.png"
        allowed_favicon = "https://docs.daylightcalendar.com/images/977.ico"
        invalid_variants = (
            {"favicon": "https://example.com/favicon.ico"},
            {"favicon": "https://docs.daylightcalendar.com.evil.test/images/977.ico"},
            {"favicon": "https://docs.daylightcalendar.com/images/other.ico"},
            {"favicon": {"light": allowed_favicon, "dark": allowed_favicon}},
            {"logo": {"light": "https://example.com/logo.png", "dark": allowed_logo}},
            {"logo": {"light": allowed_logo, "dark": allowed_favicon}},
        )
        for changes in invalid_variants:
            config = {**original, **changes}
            with self.subTest(changes=changes):
                config_path.write_text(json.dumps(config), encoding="utf-8")
                with self.assertRaisesRegex(PublicationError, "not approved"):
                    check_site(self.released, "v0.9.0")
        self.assertIsNone(self.docs_live_sha())

    def test_valid_local_markdown_image_does_not_fail_page_validation(self) -> None:
        (self.released / "docs/introduction.mdx").write_text(
            "---\ntitle: Introduction\n---\n"
            "![Daylight logo](/logo/daylight.svg)\n"
            "[Quickstart](/quickstart)\n",
            encoding="utf-8",
        )
        self.assertEqual(len(check_site(self.released, "v0.9.0")), 3)
        self.assertIn("Published v0.9.0", publish(self.released, release()))
        self.assertIsNotNone(self.docs_live_sha())

    def test_invalid_internal_link_fails_before_publication(self) -> None:
        (self.released / "docs/introduction.mdx").write_text(
            "---\ntitle: Introduction\n---\n[Missing](/unreleased-feature)\n",
            encoding="utf-8",
        )
        with self.assertRaisesRegex(PublicationError, "nonexistent docs page"):
            publish(self.released, release())
        self.assertIsNone(self.docs_live_sha())

    def test_unreleased_main_docs_changes_cannot_leak_to_docs_live(self) -> None:
        tagged_sha = run("rev-parse", "v0.9.0^{commit}", cwd=self.work)
        ready = publish(self.released, release(), check_only=True)
        self.assertIn("Validated", ready)
        self.assertIsNone(self.docs_live_sha())

        (self.work / "docs/introduction.mdx").write_text(
            "---\ntitle: Unreleased feature\n---\n# Not yet in HACS\n",
            encoding="utf-8",
        )
        self.commit("document unreleased feature on main")
        run("push", "origin", "main", cwd=self.work)
        self.assertNotEqual(run("rev-parse", "HEAD", cwd=self.work), tagged_sha)

        published = publish(self.released, release())
        self.assertIn("Published v0.9.0", published)
        self.assertEqual(self.docs_live_sha(), tagged_sha)
        self.assertIn("Already published", publish(self.released, release()))

    def test_new_stable_release_fast_forwards_old_release_but_cannot_roll_back(self) -> None:
        publish(self.released, release())
        old_sha = self.docs_live_sha()
        self.write_version("0.9.1")
        self.commit("ship new stable release")
        run("tag", "v0.9.1", cwd=self.work)
        run("push", "origin", "main", "--tags", cwd=self.work)
        self.checkout_tag("v0.9.1")

        self.assertIn("Published v0.9.1", publish(self.released, release("v0.9.1")))
        new_sha = self.docs_live_sha()
        self.assertNotEqual(old_sha, new_sha)
        self.assertEqual(new_sha, run("rev-parse", "v0.9.1^{commit}", cwd=self.released))
        self.checkout_tag("v0.9.0")
        with self.assertRaisesRegex(PublicationError, "rollback|divergent"):
            publish(self.released, release())
        self.assertEqual(new_sha, self.docs_live_sha())

    def test_invalid_new_release_preserves_live_docs(self) -> None:
        publish(self.released, release())
        old_sha = self.docs_live_sha()
        self.write_version("0.9.1")
        (self.work / "docs/quickstart.mdx").unlink()
        self.commit("bad release candidate documentation")
        run("tag", "v0.9.1", cwd=self.work)
        run("push", "origin", "main", "--tags", cwd=self.work)
        self.checkout_tag("v0.9.1")

        with self.assertRaisesRegex(PublicationError, "nonexistent docs page"):
            publish(self.released, release("v0.9.1"))
        self.assertEqual(self.docs_live_sha(), old_sha)

    def test_tag_outside_public_main_is_refused(self) -> None:
        run("checkout", "-b", "unreleased", cwd=self.work)
        self.write_version("0.9.2")
        self.commit("isolated non-main tag")
        run("tag", "v0.9.2", cwd=self.work)
        run("push", "origin", "v0.9.2", cwd=self.work)
        self.checkout_tag("v0.9.2")
        with self.assertRaisesRegex(PublicationError, "not an ancestor"):
            publish(self.released, release("v0.9.2"))
        self.assertIsNone(self.docs_live_sha())

    def docs_rehearsal_sha(self) -> str | None:
        response = run(
            "ls-remote", "--heads", "origin", "refs/heads/docs-rehearsal",
            cwd=self.released,
        )
        return response.split()[0] if response else None

    def ship_beta(self, version: str) -> None:
        """Advance main and check out an actual beta-tagged fixture release."""
        self.write_version(version)
        self.commit(f"ship beta {version}")
        run("tag", f"v{version}", cwd=self.work)
        run("push", "origin", "main", "--tags", cwd=self.work)
        self.checkout_tag(f"v{version}")

    def test_rehearsal_rejects_stable_rc_drafts_and_missing_zip(self) -> None:
        for candidate in (
            release(),
            release("v0.9.0-rc1", prerelease=True),
            release("v0.9.0-beta2"),
            release("v0.9.0-beta2", prerelease=True, draft=True),
            release("v0.9.0-beta2", prerelease=True, include_zip=False),
            {**release("v0.9.0-beta2", prerelease=True), "published_at": None},
        ):
            with self.subTest(candidate=candidate), self.assertRaises(PublicationError):
                check_release(candidate, rehearsal=True)

    def test_beta_rehearsal_keeps_existing_stable_docs_unchanged(self) -> None:
        publish(self.released, release())
        stable_sha = self.docs_live_sha()
        self.assertIsNotNone(stable_sha)
        self.ship_beta("0.9.0-beta2")
        beta = release("v0.9.0-beta2", prerelease=True)
        with self.assertRaises(PublicationError):
            publish(self.released, beta)
        self.assertIsNone(self.docs_rehearsal_sha())
        self.assertIn("Validated", publish(self.released, beta, rehearsal=True, check_only=True))
        self.assertIsNone(self.docs_rehearsal_sha())
        self.assertIn("Published v0.9.0-beta2", publish(self.released, beta, rehearsal=True))
        self.assertEqual(self.docs_live_sha(), stable_sha)
        self.assertEqual(
            self.docs_rehearsal_sha(),
            run("rev-parse", "v0.9.0-beta2^{commit}", cwd=self.released),
        )
        self.assertIn("Already published", publish(self.released, beta, rehearsal=True))

    def test_beta_rehearsal_rejects_rollback_and_preserves_last_good(self) -> None:
        self.ship_beta("0.9.0-beta2")
        beta2 = release("v0.9.0-beta2", prerelease=True)
        publish(self.released, beta2, rehearsal=True)
        beta2_sha = self.docs_rehearsal_sha()
        self.write_version("0.9.0-beta3")
        (self.work / "docs/quickstart.mdx").unlink()
        self.commit("beta3 candidate with invalid released docs")
        run("tag", "v0.9.0-beta3", cwd=self.work)
        run("push", "origin", "main", "--tags", cwd=self.work)
        self.checkout_tag("v0.9.0-beta3")
        beta3 = release("v0.9.0-beta3", prerelease=True)
        with self.assertRaisesRegex(PublicationError, "nonexistent docs page"):
            publish(self.released, beta3, rehearsal=True)
        self.assertEqual(self.docs_rehearsal_sha(), beta2_sha)

        (self.work / "docs/quickstart.mdx").write_text(
            "---\ntitle: quickstart\n---\n# quickstart\n", encoding="utf-8",
        )
        self.write_version("0.9.0-beta4")
        self.commit("repair docs for beta4")
        run("tag", "v0.9.0-beta4", cwd=self.work)
        run("push", "origin", "main", "--tags", cwd=self.work)
        self.checkout_tag("v0.9.0-beta4")
        self.assertIn("Published v0.9.0-beta4", publish(
            self.released, release("v0.9.0-beta4", prerelease=True), rehearsal=True,
        ))
        beta4_sha = self.docs_rehearsal_sha()
        self.checkout_tag("v0.9.0-beta2")
        with self.assertRaisesRegex(PublicationError, "rollback|divergent"):
            publish(self.released, beta2, rehearsal=True)
        self.assertEqual(self.docs_rehearsal_sha(), beta4_sha)
        self.assertIsNone(self.docs_live_sha())

    def test_rehearsal_mode_rejects_arbitrary_target(self) -> None:
        from scripts.publish_docs_live import inspect_ref

        with self.assertRaisesRegex(PublicationError, "Unsupported documentation publication target"):
            inspect_ref(self.released, "v0.9.0", target="main")

    def test_exact_tag_is_required_even_if_checked_out_content_looks_valid(self) -> None:
        (self.work / "docs/introduction.mdx").write_text(
            "---\ntitle: Development only\n---\n# Not released\n", encoding="utf-8",
        )
        self.commit("main advances after latest stable tag")
        run("push", "origin", "main", cwd=self.work)
        run("fetch", "origin", "main", cwd=self.released)
        run("checkout", "-B", "main", "origin/main", cwd=self.released)
        # Version metadata still looks correct, but this is not the tag SHA.
        with self.assertRaisesRegex(PublicationError, "not the published release tag"):
            publish(self.released, release())
        self.assertIsNone(self.docs_live_sha())


if __name__ == "__main__":
    unittest.main()
