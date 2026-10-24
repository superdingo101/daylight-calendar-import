"""Entry identity and independence through the real private disk store."""

import asyncio
from dataclasses import replace

import pytest
from homeassistant.helpers.storage import Store

from custom_components.daylight_calendar_import.hosted_credentials import (
    HostedCredentialStore,
    _ImmediatePrivateStore,
)

_REAL_LOAD = Store._async_load
_REAL_WRITE = Store._async_write_data


@pytest.fixture
def disk_store(hass, tmp_path, monkeypatch):
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_load", _REAL_LOAD)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_write_data", _REAL_WRITE)
    return lambda entry: HostedCredentialStore(hass, entry)


@pytest.mark.parametrize("entry", ["01KABCDEFGHJKMNPQRSTVWXYZ12", "A", "z" * 128])
async def test_supported_entry_ids_survive_disk_restart(disk_store, entry):
    """HA's uppercase ULIDs and accepted boundary lengths remain usable."""
    store = disk_store(entry)
    initial = await store.async_load()
    saved = await store.async_checkpoint(
        initial, replace(initial, processing_consent=True),
    )
    assert saved.revision == 1
    assert await disk_store(entry).async_load() == saved


async def test_credential_store_preserves_unrelated_hass_data(hass, disk_store):
    """Private shared state must not borrow an unrelated integration's slot."""
    sentinel = object()
    hass.data[None] = sentinel
    store = disk_store("independent-entry")
    initial = await store.async_load()
    await store.async_checkpoint(initial, replace(initial, processing_consent=True))
    assert hass.data[None] is sentinel


async def test_slow_entry_does_not_block_another_entry(disk_store, monkeypatch):
    """An uncertain disk writer serializes its own entry, not every HA entry."""
    first = disk_store("first-entry")
    other = disk_store("other-entry")
    initial = await first.async_load()
    other_initial = await other.async_load()
    started = asyncio.Event()
    release = asyncio.Event()
    original_save = first._store.async_save

    async def delayed_save(data):
        started.set()
        await release.wait()
        await original_save(data)

    monkeypatch.setattr(first._store, "async_save", delayed_save)
    pending = asyncio.create_task(first.async_checkpoint(
        initial, replace(initial, processing_consent=True),
    ))
    await started.wait()
    try:
        async with asyncio.timeout(1):
            saved = await other.async_checkpoint(
                other_initial, replace(other_initial, processing_consent=True),
            )
        assert saved.revision == 1
        assert not pending.done()
    finally:
        release.set()
        await pending
        if other._pending_write is not None:
            await asyncio.gather(other._pending_write, return_exceptions=True)
