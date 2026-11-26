"""Exercise the actual HA atomic writers without pytest storage mocks.

Run with python -m scripts.check_hosted_storage_compatibility in an isolated
HA environment. No network or actual account credentials are used.
"""

import asyncio
import json
import stat
import tempfile
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from homeassistant.const import __version__
from homeassistant.core import HomeAssistant
from homeassistant.helpers.storage import Store, get_internal_store_manager

from custom_components.daylight_calendar_import.hosted_credentials import (
    HostedCredentialStorageError,
    HostedCredentialStore,
)
from custom_components.daylight_calendar_import.storage import (
    STORAGE_KEY,
    STORAGE_VERSION,
    _PendingStore,
)


def fail_write(*args):
    raise OSError("synthetic disk failure")


def failing_writer(store):
    """Inject failure at the actual version-specific executor write boundary."""
    io = getattr(store, "_io", None)
    if io is not None:
        return patch.object(io, "_write", side_effect=fail_write)
    return patch.object(store, "_write_prepared_data", side_effect=fail_write)


async def check():
    with tempfile.TemporaryDirectory() as directory:
        Path(directory, ".storage").mkdir()
        hass = HomeAssistant(directory)
        manager = get_internal_store_manager(hass)
        await manager.async_initialize()

        credentials = HostedCredentialStore(hass, "acceptance_probe")
        initial = await credentials.async_load()
        key = credentials._store.key
        assert manager.async_fetch(key) == (False, None)
        committed = await credentials.async_checkpoint(
            initial, replace(initial, processing_consent=True, pairing_verifier="v" * 43),
        )
        assert committed.revision == 1
        assert manager.async_fetch(key) is None
        assert await HostedCredentialStore(hass, "acceptance_probe").async_load() == committed
        path = Path(credentials._store.path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert json.loads(path.read_text())["data"]["pairing_verifier"] == "v" * 43
        previous = path.read_bytes()
        with failing_writer(credentials._store):
            try:
                await credentials.async_checkpoint(committed, committed)
            except HostedCredentialStorageError:
                pass
            else:
                raise AssertionError("Credential write failure was swallowed")
        assert path.read_bytes() == previous
        assert await HostedCredentialStore(hass, "acceptance_probe").async_load() == committed

        pending = _PendingStore(hass, STORAGE_VERSION, STORAGE_KEY, private=True)
        assert await pending.async_load() is None
        assert manager.async_fetch(STORAGE_KEY) == (False, None)
        data = {"items": [], "seen_source_fingerprints": ["v1:source:" + "a" * 64]}
        await pending.async_save_immediate(data)
        assert manager.async_fetch(STORAGE_KEY) is None
        assert await _PendingStore(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_load() == data
        assert await Store(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_load() == data
        path = Path(pending.path)
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        previous = path.read_bytes()
        with failing_writer(pending):
            try:
                await pending.async_save_immediate({"items": []})
            except OSError as exc:
                assert str(exc) == "synthetic disk failure"
            else:
                raise AssertionError("Pending write failure was swallowed")
        assert path.read_bytes() == previous
        assert await _PendingStore(hass, STORAGE_VERSION, STORAGE_KEY, private=True).async_load() == data
        print(f"HA {__version__}: credential and pending atomic persistence, cache and failure checks PASS")


if __name__ == "__main__":
    asyncio.run(check())
