"""Corrupt bytes and rows never erase uncertainty, retries or dedup evidence."""

import asyncio
import json
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.storage import Store
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daylight_calendar_import.const import DOMAIN, CONF_AI_TASK_ENTITY, CONF_CALENDAR_ENTITY
from custom_components.daylight_calendar_import.dedup import event_fingerprint, source_fingerprint
from custom_components.daylight_calendar_import.storage import (
    PendingImport, PendingImportStore, PendingImportStorageError, STORAGE_KEY, STORAGE_VERSION, _PendingStore,
)
from test_storage import FakeStoreBackend, make_store, draft
from test_notification_journal import record as notification


@pytest.fixture(autouse=True)
def isolated_disk_storage(hass, tmp_path):
    """Actual-byte tests must not share the HA fixture's package config path."""
    hass.config.config_dir = str(tmp_path)


def valid_data():
    pending = PendingImport.create(
        source_text="private-source", events=[draft()],
        source_fingerprint=source_fingerprint("private-source"),
    )
    item = pending.as_dict()
    item["events"][0].update(status="write_uncertain", write_attempt="attempt-1")
    return {"items": [item], "activity": [{"id": item["id"], "status": "calendar_write_uncertain",
        "created_count": 0, "rejected_count": 0, "transitions": []}],
        "notifications": [notification().as_dict()],
        "seen_source_fingerprints": [source_fingerprint("handled-source")],
        "seen_event_fingerprints": [event_fingerprint(draft())], "source_claims": {}}


def set_field(data, path, value):
    parent = data
    for key in path[:-1]:
        parent = parent[key]
    parent[path[-1]] = value


@pytest.mark.parametrize("path,value", [
    (("items",), {}), (("activity",), {}), (("notifications",), {}),
    (("seen_source_fingerprints",), "hash"), (("seen_event_fingerprints", 0), None),
    (("source_claims",), []), (("source_claims",), {"live": None}),
    (("items", 0, "id"), ""), (("items", 0, "created_at"), 12),
    (("items", 0, "source_kind"), ""), (("items", 0, "source_text"), []),
    (("items", 0, "events"), []), (("items", 0, "warnings"), "warning"),
    (("items", 0, "warnings"), [12]), (("items", 0, "duplicate_events"), True),
    (("items", 0, "duplicate_events"), -1), (("items", 0, "source_title"), {}),
    (("items", 0, "events", 0, "id"), ""), (("items", 0, "events", 0, "draft"), None),
    (("items", 0, "events", 0, "draft"), []),
    (("items", 0, "events", 0, "draft", "confidence"), 10 ** 1000), (("items", 0, "events", 0, "status"), "created"),
    (("items", 0, "events", 0, "routing_unresolved"), "false"),
    (("items", 0, "events", 0, "calendar_entity"), ""),
    (("items", 0, "events", 0, "write_attempt"), 12),
    (("activity", 0, "id"), ""), (("activity", 0, "status"), None),
    (("activity", 0, "transitions"), {}), (("activity", 0, "transitions"), [None]),
    (("activity", 0, "created_count"), True), (("activity", 0, "rejected_count"), -1),
    (("notifications", 0, "status"), "unknown"),
])
async def test_bad_row_preserves_prior_snapshot_and_blocks_all_writes(monkeypatch, path, value):
    good = valid_data()
    backend = FakeStoreBackend(good)
    store = make_store(monkeypatch, backend)
    await store.async_load()
    prior = (store.list(), store.list_activity(), store.list_notifications(),
             store._seen_source_fingerprints, store._seen_event_fingerprints, dict(store._source_claims))
    broken = deepcopy(good)
    set_field(broken, path, value)
    backend.load_result = broken
    before = len(backend.saved)
    with pytest.raises(PendingImportStorageError, match="storage needs repair") as error:
        await store.async_load()
    assert "private-source" not in str(error.value)
    assert not store.accepting_services
    assert prior == (store.list(), store.list_activity(), store.list_notifications(),
                     store._seen_source_fingerprints, store._seen_event_fingerprints, dict(store._source_claims))
    with pytest.raises(PendingImportStorageError):
        await store.async_add(source_text="new", events=[replace(draft(), title="Different meeting")])
    assert len(backend.saved) == before
    assert backend.load_result == broken
    backend.load_result = good
    await store.async_load()
    assert not store._storage_blocked
    assert await store.async_enqueue_notification(replace(notification(), import_id="new-identity"))


@pytest.mark.parametrize("duplicate", ["item", "event", "activity", "notification"])
async def test_duplicate_identity_fails_closed_instead_of_collapsing_work(monkeypatch, duplicate):
    data = valid_data()
    rows = {"item": data["items"], "event": data["items"][0]["events"],
            "activity": data["activity"], "notification": data["notifications"]}[duplicate]
    rows.append(deepcopy(rows[0]))
    backend = FakeStoreBackend(data)
    store = make_store(monkeypatch, backend)
    with pytest.raises(PendingImportStorageError):
        await store.async_load()
    assert store.list() == () and store.list_notifications() == ()
    assert backend.saved == []


@pytest.mark.parametrize("data", [[], {"items": [], "source_archive": "recoverable private bytes"}])
async def test_unknown_envelope_preserved_without_partial_salvage(monkeypatch, data):
    backend = FakeStoreBackend(data)
    store = make_store(monkeypatch, backend)
    with pytest.raises(PendingImportStorageError):
        await store.async_load()
    assert backend.saved == [] and backend.load_result == data


async def test_missing_store_does_not_reset_previously_loaded_work(monkeypatch):
    backend = FakeStoreBackend(valid_data())
    store = make_store(monkeypatch, backend)
    await store.async_load()
    pending = store.list()
    backend.load_result = None
    with pytest.raises(PendingImportStorageError):
        await store.async_load()
    assert store.list() == pending


async def test_optional_advisory_corruption_and_legacy_tuple_collections_remain_recoverable(monkeypatch):
    data = valid_data()
    data["items"][0]["events"][0]["date_time_assumptions"] = {"broken": True}
    data["items"][0]["warnings"] = ("legacy warning",)
    data["activity"][0]["transitions"] = ()
    data["items"] = tuple(data["items"])
    backend = FakeStoreBackend(data)
    store = make_store(monkeypatch, backend)
    await store.async_load()
    assert store.list()[0].events[0].date_time_assumptions == ()
    assert store.list()[0].events[0].status == "write_uncertain"
    assert store.list()[0].events[0].write_attempt == "attempt-1"
    assert store.list_notifications()


def actual_disk_loader(monkeypatch):
    # The HA test fixture normally intercepts disk reads and writes. Bypass only
    # its load shim for our dedicated Store, retaining HA's public load guard.
    monkeypatch.setattr(_PendingStore, "_async_load", _PendingStore._async_load_data)


async def write_original(hass, data):
    path = Path(hass.config.path(".storage", STORAGE_KEY))
    def write():
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    await hass.async_add_executor_job(write)
    return path


def envelope(data, **kwargs):
    return {"version": STORAGE_VERSION, "minor_version": 1, "key": STORAGE_KEY, "data": data, **kwargs}


@pytest.mark.parametrize("content", [
    b'{"data":"private-source",', b"\xff", b"{}", b"[]",
    b'{"version":2,"data":{"items":[],"items":[]}}',
    b'{"version":2,"data":{"items":[],"private-value":NaN}}',
    b'{"version":2,"data":{"items":[],"private-value":"\\ud800"}}',
    b"[" * 1100 + b"0" + b"]" * 1100,
    json.dumps(envelope({"items": []}, version=True)).encode(),
    json.dumps(envelope({"items": []}, version=3)).encode(),
    json.dumps(envelope({"items": []}, minor_version=True)).encode(),
    json.dumps(envelope({"items": []}, minor_version=2)).encode(),
    json.dumps(envelope({"items": []}, key="other-storage")).encode(),
])
async def test_original_corrupt_bytes_are_not_renamed_reset_or_logged(hass, monkeypatch, content, caplog):
    actual_disk_loader(monkeypatch)
    path = await write_original(hass, content)
    store = PendingImportStore(hass)
    with pytest.raises(PendingImportStorageError):
        await store.async_load()
    assert await hass.async_add_executor_job(path.read_bytes) == content
    assert list(path.parent.glob(f"{STORAGE_KEY}.corrupt.*")) == []
    with pytest.raises(PendingImportStorageError):
        await store.async_add(source_text="do not overwrite", events=[draft()])
    assert "private-source" not in caplog.text and "private-value" not in caplog.text


async def test_original_valid_load_then_disk_corruption_has_no_stale_cache(hass, monkeypatch):
    actual_disk_loader(monkeypatch)
    original = json.dumps(envelope(valid_data())).encode()
    path = await write_original(hass, original)
    store = PendingImportStore(hass)
    await store.async_load()
    prior = store.list()
    assert store._store._data is None
    await hass.async_add_executor_job(path.write_bytes, b'{"broken":')
    with pytest.raises(PendingImportStorageError):
        await store.async_load()
    assert store.list() == prior and store._store._data is None
    with pytest.raises(PendingImportStorageError):
        await store.async_resolve_uncertain(prior[0].id, prior[0].events[0].id, "not_created")
    assert await hass.async_add_executor_job(path.read_bytes) == b'{"broken":'
    await hass.async_add_executor_job(path.write_bytes, original)
    await store.async_load()
    assert store.list() == prior and not store._storage_blocked


async def test_earlier_ha_corrupt_backup_prevents_fresh_empty_replacement(hass, monkeypatch):
    actual_disk_loader(monkeypatch)
    path = Path(hass.config.path(".storage", STORAGE_KEY))
    backup = Path(f"{path}.corrupt.old")
    original = await write_original(hass, b"preserved private source")
    await hass.async_add_executor_job(original.rename, backup)
    store = PendingImportStore(hass)
    with pytest.raises(PendingImportStorageError):
        await store.async_load()
    assert not path.exists()
    assert await hass.async_add_executor_job(backup.read_bytes) == b"preserved private source"


async def test_v1_invalid_late_collection_does_not_migrate_or_overwrite_original(hass, monkeypatch):
    actual_disk_loader(monkeypatch)
    legacy = valid_data()
    legacy["items"][0]["events"] = [draft().as_dict()]
    legacy["items"][0]["approval_in_flight"] = True
    legacy["seen_event_fingerprints"] = {"broken": "recoverable"}
    content = json.dumps(envelope(legacy, version=1)).encode()
    path = await write_original(hass, content)
    with pytest.raises(PendingImportStorageError):
        await PendingImportStore(hass).async_load()
    assert await hass.async_add_executor_job(path.read_bytes) == content


async def test_corruption_blocks_real_ha_setup_and_clears_repair_after_restore(hass, monkeypatch):
    actual_disk_loader(monkeypatch)
    path = await write_original(hass, b'{"private-source":')
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_AI_TASK_ENTITY: "ai_task.test",
        CONF_CALENDAR_ENTITY: "calendar.home"})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.SETUP_ERROR
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "storage_recovery")
    assert issue is not None
    assert issue.translation_placeholders == {"documentation_url":
        "https://github.com/superdingo101/daylight-calendar-import/blob/main/docs/storage-recovery.md"}
    assert entry.entry_id not in hass.data.get(DOMAIN, {})
    assert not hass.services.has_service(DOMAIN, "approve_pending_event")
    await hass.async_add_executor_job(path.write_bytes, json.dumps(envelope(valid_data())).encode())
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert ir.async_get(hass).async_get_issue(DOMAIN, "storage_recovery") is None
    assert hass.data[DOMAIN][entry.entry_id].list()[0].events[0].status == "write_uncertain"


async def test_fresh_actual_store_without_original_or_corrupt_backup_starts_empty(hass, monkeypatch):
    actual_disk_loader(monkeypatch)
    store = PendingImportStore(hass)
    await store.async_load()
    assert store.list() == () and store.list_notifications() == ()
    assert not store._storage_blocked


async def test_unexpected_empty_base_loader_cannot_replace_valid_original(hass, monkeypatch):
    actual_disk_loader(monkeypatch)
    content = json.dumps(envelope(valid_data())).encode()
    path = await write_original(hass, content)
    monkeypatch.setattr(Store, "_async_load_data", AsyncMock(return_value=None))
    store = PendingImportStore(hass)
    with pytest.raises(PendingImportStorageError):
        await store.async_load()
    assert store._store._data is None
    assert await hass.async_add_executor_job(path.read_bytes) == content


async def test_load_cancellation_preserves_valid_snapshot_without_claiming_corruption(monkeypatch):
    backend = FakeStoreBackend(valid_data())
    store = make_store(monkeypatch, backend)
    await store.async_load()
    pending = store.list()
    backend.async_load = AsyncMock(side_effect=asyncio.CancelledError)
    with pytest.raises(asyncio.CancelledError):
        await store.async_load()
    assert store.list() == pending and not store._storage_blocked


async def test_v1_migration_queued_during_shutdown_is_not_cleared(hass, monkeypatch):
    from homeassistant.core import CoreState
    actual_disk_loader(monkeypatch)
    legacy = valid_data()
    legacy["items"][0]["events"] = [draft().as_dict()]
    legacy["items"][0]["approval_in_flight"] = True
    content = json.dumps(envelope(legacy, version=1)).encode()
    path = await write_original(hass, content)
    store = PendingImportStore(hass)
    hass.state = CoreState.stopping
    try:
        await store.async_load()
        pending = store.list()[0]
        assert pending.events[0].status == "write_uncertain"
        queued = store._store._data
        assert queued["version"] == STORAGE_VERSION
        assert queued["data"]["items"][0]["events"][0]["id"] == pending.events[0].id
        assert await hass.async_add_executor_job(path.read_bytes) == content
    finally:
        hass.state = CoreState.running


@pytest.mark.parametrize("path,value,message", [
    (("source_archive",), "private bytes", "unsupported pending storage envelope"),
    (("items",), {}, "invalid storage collection"),
    (("items", 0, "id"), "", "invalid storage identity"),
    (("items", 0, "warnings"), "malformed", "invalid import warnings"),
    (("items", 0, "source_text"), [], "invalid pending import"),
    (("items", 0, "duplicate_events"), True, "invalid source metadata"),
    (("items", 0, "source_title"), {}, "invalid source metadata"),
    (("items", 0, "events", 0, "routing_unresolved"), "false", "invalid routing checkpoint"),
    (("activity", 0, "transitions"), {}, "invalid lifecycle history"),
    (("activity", 0, "created_count"), -1, "invalid lifecycle count"),
    (("source_claims",), [], "invalid source claims"),
    (("activity", 0, "status"), "processing ", "invalid lifecycle status"),
])
def test_storage_decoder_rejects_invalid_data_with_precise_category(path, value, message):
    """Recovery checks should distinguish corrupt fields without leaking their data."""
    from custom_components.daylight_calendar_import.storage import _decode_storage_data
    data = deepcopy(valid_data())
    set_field(data, path, value)
    with pytest.raises(ValueError) as error:
        _decode_storage_data(data)
    assert str(error.value) == message


@pytest.mark.parametrize("collection", ["items", "activity", "notifications"])
def test_storage_decoder_rejects_duplicate_identities_with_precise_error(collection):
    from custom_components.daylight_calendar_import.storage import _decode_storage_data
    data = valid_data()
    data[collection].append(deepcopy(data[collection][0]))
    with pytest.raises(ValueError, match="^duplicate storage identity$"):
        _decode_storage_data(data)


@pytest.mark.parametrize("minor", [None, 0, 2, False, "1"])
def test_envelope_validation_checks_explicit_minor_version(minor):
    from custom_components.daylight_calendar_import.storage import _validate_pending_envelope
    data = envelope(valid_data(), minor_version=minor)
    with pytest.raises(PendingImportStorageError, match="^Daylight import storage needs repair"):
        _validate_pending_envelope(data)


def test_envelope_default_minor_version_is_backwards_compatible():
    from custom_components.daylight_calendar_import.storage import _validate_pending_envelope
    data = envelope(valid_data())
    del data["minor_version"]
    assert _validate_pending_envelope(data) == data


@pytest.mark.parametrize("value", ["other", None, False])
def test_envelope_validates_explicit_storage_key(value):
    from custom_components.daylight_calendar_import.storage import _validate_pending_envelope
    data = envelope(valid_data(), key=value)
    with pytest.raises(PendingImportStorageError, match="^Daylight import storage needs repair"):
        _validate_pending_envelope(data)


def test_envelope_missing_key_remains_legacy_compatible():
    from custom_components.daylight_calendar_import.storage import _validate_pending_envelope
    data = envelope(valid_data())
    del data["key"]
    assert _validate_pending_envelope(data) == data


def test_missing_required_items_cannot_be_loaded_as_an_empty_queue():
    from custom_components.daylight_calendar_import.storage import _decode_storage_data
    data = valid_data()
    del data["items"]
    with pytest.raises(KeyError, match="items"):
        _decode_storage_data(data)


def test_storage_decoder_preserves_valid_transition_and_count_defaults():
    from custom_components.daylight_calendar_import.storage import _decode_storage_data
    data = valid_data()
    data["activity"][0]["transitions"] = ({"type": "processing", "at": "2026-01-01T00:00:00+00:00"},)
    data["activity"][0].pop("created_count")
    data["activity"][0].pop("rejected_count")
    decoded = _decode_storage_data(data)
    assert decoded[1][0]["transitions"] == [{"type": "processing", "at": "2026-01-01T00:00:00+00:00"}]
    assert "created_count" not in decoded[1][0]
    assert "rejected_count" not in decoded[1][0]


async def test_malformed_activity_status_preserves_old_snapshot_and_blocks_retention(monkeypatch):
    good = valid_data()
    backend = FakeStoreBackend(good)
    store = make_store(monkeypatch, backend)
    await store.async_load()
    prior = store.list_activity()
    broken = deepcopy(good)
    broken["activity"][0]["status"] = "processing "
    backend.load_result = broken
    before = len(backend.saved)
    with pytest.raises(PendingImportStorageError):
        await store.async_load()
    assert store.list_activity() == prior
    assert store._storage_blocked
    assert len(backend.saved) == before
    assert backend.load_result == broken


@pytest.mark.parametrize("status", [
    "discovered", "processing", "failed", "duplicate", "no_events",
    "review_ready", "source_reprocessed", "event_rejected", "mixed",
    "calendar_write_started", "calendar_write_uncertain",
    "calendar_created", "calendar_create_failed",
])
def test_supported_lifecycle_statuses_remain_loadable(status):
    from custom_components.daylight_calendar_import.storage import _decode_storage_data
    data = valid_data()
    data["activity"][0]["status"] = status
    assert _decode_storage_data(data)[1][0]["status"] == status


async def test_duplicate_original_json_keys_rejected_without_exposing_bytes(hass, monkeypatch):
    actual_disk_loader(monkeypatch)
    content = b'{"version":2,"minor_version":1,"key":"daylight_calendar_import.pending_imports","data":{"items":[],"items":[]}}'
    path = await write_original(hass, content)
    store = PendingImportStore(hass)
    with pytest.raises(PendingImportStorageError, match="^Daylight import storage needs repair"):
        await store.async_load()
    assert await hass.async_add_executor_job(path.read_bytes) == content


def test_original_duplicate_json_key_reports_precise_decoder_failure(tmp_path):
    from custom_components.daylight_calendar_import.storage import _read_pending_storage
    path = tmp_path / STORAGE_KEY
    path.write_bytes(b'{"items":[],"items":[]}')
    with pytest.raises(ValueError, match="^duplicate storage key$"):
        _read_pending_storage(str(path))


def test_corrupt_backup_without_original_reports_safe_recovery_error(tmp_path):
    from custom_components.daylight_calendar_import.storage import _read_pending_storage
    original = tmp_path / STORAGE_KEY
    (tmp_path / (STORAGE_KEY + ".corrupt.old")).write_bytes(b"private bytes")
    with pytest.raises(PendingImportStorageError) as error:
        _read_pending_storage(str(original))
    assert str(error.value).startswith("Daylight import storage needs repair")
    assert "private bytes" not in str(error.value)


def test_missing_original_without_corrupt_backup_is_new_install(tmp_path):
    from custom_components.daylight_calendar_import.storage import _read_pending_storage
    assert _read_pending_storage(str(tmp_path / STORAGE_KEY)) is None


@pytest.mark.parametrize("collection", ["seen_source_fingerprints", "seen_event_fingerprints"])
def test_invalid_fingerprint_identity_has_precise_error(collection):
    from custom_components.daylight_calendar_import.storage import _decode_storage_data
    data = valid_data()
    data[collection] = [""]
    with pytest.raises(ValueError, match="^invalid storage identity$"):
        _decode_storage_data(data)


@pytest.mark.parametrize("collection", ["seen_source_fingerprints", "seen_event_fingerprints"])
def test_valid_fingerprints_survive_decoding(collection):
    from custom_components.daylight_calendar_import.storage import _decode_storage_data
    data = valid_data()
    assert tuple(data[collection]) in _decode_storage_data(data)


def test_valid_optional_source_metadata_preserved_on_load():
    from custom_components.daylight_calendar_import.storage import _decode_storage_data
    data = valid_data()
    data["items"][0]["source_title"] = "Recovered document"
    data["items"][0]["source_sender"] = "sender@example.test"
    data["items"][0]["source_fingerprint"] = source_fingerprint("exact-source")
    restored = next(iter(_decode_storage_data(data)[0].values()))
    assert (restored.source_title, restored.source_sender, restored.source_fingerprint) == (
        "Recovered document", "sender@example.test", source_fingerprint("exact-source")
    )


@pytest.mark.parametrize("path,valid_kind", [
    (("items", 0, "source_fingerprint"), "source"),
    (("seen_source_fingerprints", 0), "source"),
    (("seen_event_fingerprints", 0), "event"),
    (("source_claims", "live"), "source"),
])
@pytest.mark.parametrize("invalid", [
    "invalid-hash",
    "v1:event:" + "a" * 64,
    "v2:source:" + "a" * 64,
    "v1:source:" + "a" * 63,
    "v1:source:" + "A" * 64,
    "v1:source:" + "g" * 64,
])
def test_corrupt_fingerprint_formats_are_rejected(path, valid_kind, invalid):
    """Every durable dedupe location must reject hashes it cannot recognize."""
    from custom_components.daylight_calendar_import.storage import _decode_storage_data
    data = valid_data()
    data["source_claims"]["live"] = source_fingerprint("claimed-source")
    # Include cross-kind and well-formed-but-wrong-version examples for events.
    if valid_kind == "event" and invalid.startswith("v1:source:"):
        bad_value = invalid
    elif valid_kind == "source" and invalid.startswith("v1:event:"):
        bad_value = invalid
    else:
        bad_value = invalid
    set_field(data, path, bad_value)
    with pytest.raises(ValueError, match="^invalid stored fingerprint$"):
        _decode_storage_data(data)


@pytest.mark.parametrize("path", [
    ("items", 0, "source_fingerprint"),
    ("seen_source_fingerprints", 0),
    ("seen_event_fingerprints", 0),
    ("source_claims", "live"),
])
async def test_malformed_fingerprint_on_disk_preserves_original_file(hass, monkeypatch, path):
    """A corrupt hash cannot bypass the repair gate or silently enable duplicates."""
    actual_disk_loader(monkeypatch)
    data = valid_data()
    data["source_claims"]["live"] = source_fingerprint("claimed-source")
    set_field(data, path, "corrupt-identity")
    original = json.dumps(envelope(data)).encode()
    location = await write_original(hass, original)
    store = PendingImportStore(hass)
    with pytest.raises(PendingImportStorageError, match="storage needs repair"):
        await store.async_load()
    assert await hass.async_add_executor_job(location.read_bytes) == original
    assert list(location.parent.glob(f"{STORAGE_KEY}.corrupt.*")) == []
    assert not store.accepting_services
    with pytest.raises(PendingImportStorageError):
        await store.async_add(source_text="new source", events=[draft()])
    assert await hass.async_add_executor_job(location.read_bytes) == original


def test_valid_source_claim_and_fingerprint_kinds_survive_decoding():
    from custom_components.daylight_calendar_import.storage import _decode_storage_data
    data = valid_data()
    data["source_claims"]["active"] = source_fingerprint("active-source")
    result = _decode_storage_data(data)
    assert result[3] == (source_fingerprint("handled-source"),)
    assert result[4] == (event_fingerprint(draft()),)
    assert result[5] == {"active": source_fingerprint("active-source")}
