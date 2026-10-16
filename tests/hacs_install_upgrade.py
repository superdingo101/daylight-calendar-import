"""Smoke-test real release payloads in an isolated Home Assistant config tree.

This exercises HACS's integration-directory replacement boundary, not HACS's
network/download UI. Historical trees come from immutable release tags.
"""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile

import pytest

from scripts.build_hacs_release import ASSET_NAME, INTEGRATION, build, tracked_files

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("previous", [None, "v0.5.3", "v0.7.1"])
def test_release_zip_fresh_install_and_historical_upgrade(tmp_path, previous):
    config = tmp_path / "config"
    installed = config / INTEGRATION
    installed.mkdir(parents=True)
    if previous:
        names = subprocess.check_output(
            ["git", "ls-tree", "-r", "--name-only", previous, "--", str(INTEGRATION)],
            cwd=ROOT, text=True,
        ).splitlines()
        assert names, f"Historical release {previous} must be available"
        for name in names:
            target = config / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(subprocess.check_output(["git", "show", f"{previous}:{name}"], cwd=ROOT))
        assert json.loads((installed / "manifest.json").read_text())["version"] == {
            # The v0.5.3 tag still carried manifest version 0.5.0. Preserve
            # that historical packaging detail in the upgrade smoke test.
            "v0.5.3": "0.5.0", "v0.7.1": "0.7.1",
        }[previous]
        # Replacement must discard files removed between integration releases.
        (installed / "obsolete_beta_module.py").write_text("raise RuntimeError('obsolete')")

    protected = {
        ".storage/core.config_entries": b'{"entries": [{"domain": "daylight_calendar_import"}]}',
        ".storage/daylight_calendar_import.pending_imports": b'{"version": 1, "data": {"items": []}}',
        "configuration.yaml": b"default_config:\n",
        "custom_components/other_integration/__init__.py": b"# unrelated integration\n",
    }
    for name, content in protected.items():
        target = config / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)

    version = json.loads((ROOT / INTEGRATION / "manifest.json").read_text())["version"]
    archive_path = tmp_path / ASSET_NAME
    build(ROOT, archive_path, version)
    # HACS replaces only its integration directory when installing a ZIP release.
    shutil.rmtree(installed)
    with ZipFile(archive_path) as archive:
        archive.extractall(installed)
    files = tracked_files(ROOT)
    assert {p.relative_to(installed).as_posix() for p in installed.rglob("*") if p.is_file()} == set(files)
    for name, source in files.items():
        assert (installed / name).read_bytes() == source.read_bytes()
    for name, content in protected.items():
        assert (config / name).read_bytes() == content

    # A fresh interpreter avoids the test suite's already-imported source tree.
    # Importing the shipped config flow must register the integration with HA.
    code = """
import json
from pathlib import Path
from homeassistant.config_entries import HANDLERS
import custom_components.daylight_calendar_import as integration
import custom_components.daylight_calendar_import.config_flow as flow
root = Path.cwd() / 'custom_components/daylight_calendar_import'
assert Path(integration.__file__).resolve() == root / '__init__.py'
assert Path(flow.__file__).resolve() == root / 'config_flow.py'
manifest = json.loads((root / 'manifest.json').read_text())
assert manifest['config_flow'] is True
assert HANDLERS.get(manifest['domain']) is not None
assert callable(integration.async_setup_entry)
assert callable(integration.async_unload_entry)
assert manifest['version'] == __import__('sys').argv[1]
"""
    subprocess.run(
        [sys.executable, "-c", code, version], cwd=config,
        env={**os.environ, "PYTHONPATH": str(config)}, check=True,
        capture_output=True, text=True,
    )
