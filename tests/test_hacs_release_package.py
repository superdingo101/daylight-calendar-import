"""No-Home-Assistant-dependency packaging regression tests."""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from zipfile import ZipFile

from scripts.build_hacs_release import ASSET_NAME, INTEGRATION, build, verify_archive, tracked_files


class HacsPackageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        self.source = self.root / INTEGRATION
        (self.source / "translations").mkdir(parents=True)
        (self.source / "__init__.py").write_text("# sample integration\n")
        (self.source / "config_flow.py").write_text("# setup\n")
        (self.source / "translations" / "en.json").write_text('{"title": "Test"}\n')
        (self.source / "manifest.json").write_text(
            json.dumps({"domain": "daylight_calendar_import", "version": "0.7.1"})
        )
        (self.root / "hacs.json").write_text(
            json.dumps({"zip_release": True, "filename": ASSET_NAME, "hide_default_branch": True})
        )
        subprocess.run(["git", "add", "-A"], cwd=self.root, check=True)
        self.output = self.root / "dist" / ASSET_NAME

    def test_extracts_exact_integration_tree_without_extra_directory(self) -> None:
        build(self.root, self.output, "0.7.1")
        with ZipFile(self.output) as archive:
            self.assertEqual(
                set(archive.namelist()),
                {"__init__.py", "config_flow.py", "manifest.json", "translations/en.json"},
            )
            target = self.root / "installed" / "custom_components" / "daylight_calendar_import"
            archive.extractall(target)
        for name, source in tracked_files(self.root).items():
            self.assertEqual((target / name).read_bytes(), source.read_bytes())

    def test_untracked_files_are_not_distributed(self) -> None:
        (self.source / "credentials.txt").write_text("not shipped")
        build(self.root, self.output, "0.7.1")
        with ZipFile(self.output) as archive:
            self.assertNotIn("credentials.txt", archive.namelist())

    def test_broken_version_refused(self) -> None:
        with self.assertRaisesRegex(ValueError, "version"):
            build(self.root, self.output, "0.7.2")

    def test_missing_zip_hacs_setting_refused(self) -> None:
        (self.root / "hacs.json").write_text(json.dumps({"zip_release": False}))
        with self.assertRaisesRegex(ValueError, "hacs.json"):
            build(self.root, self.output, "0.7.1")

    def test_default_branch_cannot_be_offered_for_zip_mode(self) -> None:
        (self.root / "hacs.json").write_text(
            json.dumps({"zip_release": True, "filename": ASSET_NAME})
        )
        with self.assertRaisesRegex(ValueError, "default branch"):
            build(self.root, self.output, "0.7.1")

    def test_missing_manifest_is_refused(self) -> None:
        subprocess.run(["git", "rm", "-q", "-f", str(INTEGRATION / "manifest.json")], cwd=self.root, check=True)
        with self.assertRaises(FileNotFoundError):
            build(self.root, self.output, "0.7.1")

    def test_symlink_is_refused(self) -> None:
        (self.source / "external.py").symlink_to(self.source / "__init__.py")
        subprocess.run(["git", "add", str(INTEGRATION / "external.py")], cwd=self.root, check=True)
        with self.assertRaisesRegex(ValueError, "symlink"):
            build(self.root, self.output, "0.7.1")

    def test_archive_tampering_is_detected(self) -> None:
        build(self.root, self.output, "0.7.1")
        with ZipFile(self.output, "a") as archive:
            archive.writestr("unexpected.py", "oops")
        with self.assertRaisesRegex(ValueError, "exactly"):
            verify_archive(self.output, tracked_files(self.root))

    def test_build_is_reproducible(self) -> None:
        build(self.root, self.output, "0.7.1")
        first = self.output.read_bytes()
        build(self.root, self.output, "0.7.1")
        self.assertEqual(first, self.output.read_bytes())


if __name__ == "__main__":
    unittest.main()
