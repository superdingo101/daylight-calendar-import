"""Local-only namespace receipts inspect real supported storage, never a UI flag."""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from custom_components.daylight_calendar_import import hosted_namespace as module
from custom_components.daylight_calendar_import.storage import (
    PendingImportStore,
    _PendingStore,
)
from tests.test_hosted_lifecycle import _REAL_LOAD, _REAL_WRITE
from tests.test_storage import draft


@pytest.fixture
async def pending(hass, tmp_path, monkeypatch):
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr(_PendingStore, "_async_load", _REAL_LOAD)
    monkeypatch.setattr(_PendingStore, "_async_write_data", _REAL_WRITE)
    store = PendingImportStore(hass)
    await store.async_load()
    store.accepting_services = True
    return store


async def test_loaded_empty_current_contract_receipt_makes_no_file_or_mutation(pending):
    path = Path(pending._store.path)
    assert not path.exists()
    assert await module.LocalOnlyNamespaceReconciler(pending).async_receipt(None) == module.LOCAL_ONLY_CONTRACT
    assert not path.exists()


async def test_real_nonempty_byo_queue_is_preserved_and_not_mistaken_for_hosted_journal(pending):
    await pending.async_add(source_text="School newsletter", events=[draft()], source_kind="email")
    path = Path(pending._store.path)
    original = path.read_bytes()
    assert await module.LocalOnlyNamespaceReconciler(pending).async_receipt(("account_1234567890", "installation_1234567890")) == "pending-v2-local-only"
    assert path.read_bytes() == original
    assert len(pending._items) == 1


@pytest.mark.parametrize("field,value", [
    ("_storage_blocked", True), ("accepting_services", False), ("accepting_services", 1),
])
async def test_uncertain_or_paused_owner_denies_without_read(pending, monkeypatch, field, value):
    setattr(pending, field, value)
    def forbidden(*_): raise AssertionError("Do not read on an invalid owner")
    monkeypatch.setattr(module, "_read_contract", forbidden)
    with pytest.raises(module.NamespaceReconciliationError, match="preserve existing journals"):
        await module.LocalOnlyNamespaceReconciler(pending).async_receipt(None)


async def test_queued_private_write_denies_receipt(pending):
    pending._store._data = {"pending": "write"}
    with pytest.raises(module.NamespaceReconciliationError):
        await module.LocalOnlyNamespaceReconciler(pending).async_receipt(None)
    pending._store._data = None


@pytest.mark.parametrize("namespace", [[], ("x", "y"), ("account_1234567890",), (1, "installation_1234567890")])
async def test_receipt_never_accepts_malformed_prior_namespace(pending, namespace):
    with pytest.raises(module.NamespaceReconciliationError):
        await module.LocalOnlyNamespaceReconciler(pending).async_receipt(namespace)


async def test_unrecognized_store_or_future_contract_is_default_deny(pending, monkeypatch):
    with pytest.raises(module.NamespaceReconciliationError):
        await module.LocalOnlyNamespaceReconciler(SimpleNamespace()).async_receipt(None)
    actual = pending._store
    pending._store = object()
    with pytest.raises(module.NamespaceReconciliationError):
        await module.LocalOnlyNamespaceReconciler(pending).async_receipt(None)
    pending._store = actual
    monkeypatch.setattr(module.storage, "STORAGE_VERSION", 3)
    with pytest.raises(module.NamespaceReconciliationError):
        await module.LocalOnlyNamespaceReconciler(pending).async_receipt(None)
    monkeypatch.setattr(module.storage, "STORAGE_VERSION", 2)
    monkeypatch.setattr(module, "SourceKind", {"manual_text", "email", "image", "pdf", "hosted"})
    with pytest.raises(module.NamespaceReconciliationError):
        await module.LocalOnlyNamespaceReconciler(pending).async_receipt(None)


@pytest.mark.parametrize("change", ["future_version", "future_minor", "extra_envelope", "hosted_journal", "hosted_source", "item_metadata", "event_metadata", "draft_metadata", "activity_metadata", "transition_metadata", "notification_metadata", "missing", "corrupt"])
async def test_future_or_unknown_durable_provenance_cannot_be_silently_erased(pending, change):
    await pending.async_add(source_text="School newsletter", events=[draft()])
    path = Path(pending._store.path)
    envelope = json.loads(path.read_text())
    data = envelope["data"]
    if change == "future_version": envelope["version"] = 3
    elif change == "future_minor": envelope["minor_version"] = 2
    elif change == "extra_envelope": envelope["hosted_journal"] = []
    elif change == "hosted_journal": data["hosted_ack_journal"] = [{"acknowledged": True}]
    elif change == "hosted_source": data["items"][0]["source_kind"] = "hosted"
    elif change == "item_metadata": data["items"][0]["installation_id"] = "installation_1234567890"
    elif change == "event_metadata": data["items"][0]["events"][0]["delivery_id"] = "delivery_1234567890"
    elif change == "draft_metadata": data["items"][0]["events"][0]["draft"]["hosted"] = True
    elif change == "activity_metadata": data["activity"][0]["acknowledged"] = True
    elif change == "transition_metadata": data["activity"][0]["transitions"][0]["ack_id"] = "private"
    elif change == "notification_metadata":
        data["notifications"] = [{"import_id": "import_1234567890", "transition_type": "review_ready", "occurred_at": "2026-10-24T00:00:00Z", "event_id": None, "hosted": True}]
    if change == "missing": path.unlink()
    elif change == "corrupt": path.write_text('{"version":2,"version":2}')
    else: path.write_text(json.dumps(envelope))
    original = path.read_bytes() if path.exists() else None
    with pytest.raises(module.NamespaceReconciliationError):
        await module.LocalOnlyNamespaceReconciler(pending).async_receipt(None)
    assert (path.read_bytes() if path.exists() else None) == original


async def test_loaded_unknown_source_or_transition_is_not_hidden_by_missing_file(pending):
    await pending.async_add(source_text="Unknown provenance", events=[draft()], source_kind="hosted")
    with pytest.raises(module.NamespaceReconciliationError):
        await module.LocalOnlyNamespaceReconciler(pending).async_receipt(None)


async def test_owner_lock_serializes_receipt_with_local_transactions(pending):
    await pending._lock.acquire()
    task = asyncio.create_task(module.LocalOnlyNamespaceReconciler(pending).async_receipt(None))
    await asyncio.sleep(0)
    assert not task.done()
    pending._lock.release()
    assert await task == module.LOCAL_ONLY_CONTRACT


async def test_unknown_activity_source_is_not_a_local_only_receipt(pending):
    await pending.async_add(source_text="School newsletter", events=[draft()])
    pending._activity[0]["source_kind"] = "hosted"
    with pytest.raises(module.NamespaceReconciliationError):
        await module.LocalOnlyNamespaceReconciler(pending).async_receipt(None)


async def test_pause_during_disk_validation_cannot_return_a_receipt(pending, monkeypatch):
    original = pending._store.hass.async_add_executor_job
    async def pause_after_read(*args):
        result = await original(*args)
        pending.accepting_services = False
        return result
    monkeypatch.setattr(pending._store.hass, "async_add_executor_job", pause_after_read)
    with pytest.raises(module.NamespaceReconciliationError):
        await module.LocalOnlyNamespaceReconciler(pending).async_receipt(None)
