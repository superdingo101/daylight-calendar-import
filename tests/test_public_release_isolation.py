"""Regression checks for the public HACS artifact isolation guard."""

from __future__ import annotations

from pathlib import Path
import tempfile
import unittest
from zipfile import ZipFile

from scripts.check_public_release_isolation import scan_archive


class PublicReleaseIsolationTests(unittest.TestCase):
    def scan(self, members: dict[str, bytes | str]) -> list[str]:
        with tempfile.TemporaryDirectory() as directory:
            archive = Path(directory) / "daylight-calendar-import.zip"
            with ZipFile(archive, "w") as output:
                for path, content in members.items():
                    output.writestr(path, content)
            return scan_archive(archive)

    def test_public_integration_allowed(self) -> None:
        self.assertEqual(
            self.scan(
                {
                    "__init__.py": "from .parser import parse_events\n",
                    "translations/en.json": '{"description": "Self-hosted Direct IMAP inbox"}',
                    "frontend/settings.js": 'const title = "Local import settings";',
                }
            ),
            [],
        )

    def test_dormant_hosted_module_rejected_by_filename(self) -> None:
        violations = self.scan({"hosted_transport.py": "# No runtime import yet\n"})
        self.assertTrue(any("file path" in violation for violation in violations))

    def test_hosted_ui_text_rejected(self) -> None:
        violations = self.scan({"frontend/settings.js": 'const title = "Hosted connection";'})
        self.assertTrue(any("settings.js:1" in violation for violation in violations))

    def test_cloud_client_identifier_rejected(self) -> None:
        violations = self.scan({"frontend/panel.js": "const cloud_client = {};"})
        self.assertTrue(any("cloud service surface" in violation for violation in violations))

    def test_cloud_brand_rejected(self) -> None:
        violations = self.scan({"strings.json": '{"name": "Daylight Cloud"}'})
        self.assertTrue(any("Daylight Cloud name" in violation for violation in violations))

    def test_unrelated_icloud_word_allowed(self) -> None:
        self.assertEqual(self.scan({"translations/en.json": '{"label": "iCloud calendar"}'}), [])

    def test_text_that_cannot_be_inspected_fails_closed(self) -> None:
        violations = self.scan({"frontend/settings.js": b"\xff\xfe"})
        self.assertTrue(any("non-UTF-8" in violation for violation in violations))

    def test_binary_icon_allowed(self) -> None:
        self.assertEqual(self.scan({"brand/icon.png": b"\x89PNG\r\n\x1a\n"}), [])

    def test_directory_entry_rejected(self) -> None:
        violations = self.scan({"empty/": b""})
        self.assertTrue(any("unexpected directory" in violation for violation in violations))


if __name__ == "__main__":
    unittest.main()
