"""Credential checkpoints use actual HA private atomic disk storage."""

import asyncio
import json
import stat
import threading
from dataclasses import asdict, replace
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from homeassistant.helpers.storage import Store

from custom_components.daylight_calendar_import.hosted_credentials import (
    BoundCredential,
    CandidateCredential,
    CredentialCheckpoint,
    HostedCredentialConflict,
    HostedCredentialStorageError,
    HostedCredentialStore,
    _consume_result,
    _deadline,
    _decode,
    _ImmediatePrivateStore,
    _read,
    _text,
    _unique,
)

_REAL_LOAD = Store._async_load
_REAL_WRITE = Store._async_write_data

TOKEN = "secret-token-" + "x" * 43
VERIFIER = "v" * 43
ACCOUNT = "account_1234567890"
INSTALLATION = "installation_1234567890"
EXPIRES = "2026-12-01T00:00:00+00:00"
CONFIRMS = "2026-10-24T00:10:00+00:00"
STORAGE_ERROR = r"^Hosted credential storage needs repair; preserve the original file\.$"


def bound(**changes):
    return BoundCredential(**{
        "account_id": ACCOUNT, "installation_id": INSTALLATION,
        "token": TOKEN, "expires_at": EXPIRES, **changes,
    })


def candidate(**changes):
    return CandidateCredential(**{
        "installation_id": INSTALLATION, "token": TOKEN + "new",
        "expires_at": EXPIRES, "confirmation_expires_at": CONFIRMS, **changes,
    })


@pytest.fixture
def make_store(hass, tmp_path, monkeypatch):
    hass.config.config_dir = str(tmp_path)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_load", _REAL_LOAD)
    monkeypatch.setattr(_ImmediatePrivateStore, "_async_write_data", _REAL_WRITE)
    return lambda entry="entry": HostedCredentialStore(hass, entry)


async def test_private_atomic_restart_preserves_both_bundles_and_namespaces(make_store):
    store = make_store()
    initial = await store.async_load()
    assert initial == CredentialCheckpoint()
    consent = await store.async_checkpoint(initial, replace(
        initial, processing_consent=True, pairing_verifier=VERIFIER,
    ))
    restarted = make_store()
    assert await restarted.async_load() == consent
    assert consent.revision == 1
    active = await store.async_checkpoint(consent, replace(consent, current=bound()))
    staged = await store.async_checkpoint(active, replace(active, candidate=candidate()))
    assert staged.revision == 3
    assert staged.current.namespace == (ACCOUNT, INSTALLATION)
    assert not hasattr(staged.candidate, "namespace")
    assert not any(secret in repr(staged) for secret in (TOKEN, VERIFIER))
    assert TOKEN not in repr(staged.current)
    assert TOKEN not in repr(staged.candidate)
    assert await make_store().async_load() == staged
    path = Path(store._store.path)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert json.loads(path.read_text())["key"] == store._store.key
    assert await make_store("other").async_load() == CredentialCheckpoint()
    assert not list(path.parent.glob("*.corrupt.*"))
    assert store._store._atomic_writes is True


async def test_checkpoint_not_available_until_loaded_and_load_only_once(make_store):
    store = make_store()
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        _ = store.snapshot
    initial = await store.async_load()
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await store.async_load()
    assert store.snapshot is initial


async def test_stale_concurrent_snapshots_cannot_overwrite_candidate(make_store):
    store = make_store()
    initial = await store.async_load()
    results = await asyncio.gather(
        store.async_checkpoint(initial, replace(initial, processing_consent=True)),
        store.async_checkpoint(initial, replace(initial, processing_consent=True, pairing_verifier=VERIFIER)),
        return_exceptions=True,
    )
    assert isinstance(results[0], CredentialCheckpoint)
    assert isinstance(results[1], HostedCredentialConflict)
    assert store.snapshot.revision == 1
    with pytest.raises(HostedCredentialConflict):
        await store.async_checkpoint(replace(store.snapshot), store.snapshot)


@pytest.mark.parametrize("proposed", [None, CredentialCheckpoint(revision=1)])
async def test_invalid_proposal_revision_rejected_without_io(make_store, monkeypatch, proposed):
    store = make_store()
    initial = await store.async_load()
    save = AsyncMock()
    monkeypatch.setattr(store._store, "async_save", save)
    with pytest.raises(HostedCredentialConflict):
        await store.async_checkpoint(initial, proposed)
    save.assert_not_called()


async def test_candidate_staging_cannot_replace_or_discard_working_credential(make_store):
    store = make_store()
    initial = await store.async_load()
    active = await store.async_checkpoint(initial, replace(initial, processing_consent=True, current=bound()))
    for proposed in (
        replace(active, candidate=candidate(), current=None),
        replace(active, candidate=candidate(), current=bound(token=TOKEN + "other")),
    ):
        with pytest.raises(HostedCredentialConflict):
            await store.async_checkpoint(active, proposed)
    assert store.snapshot is active
    assert await make_store().async_load() == active


async def test_failed_atomic_write_preserves_original_and_blocks_future_work(make_store, monkeypatch, caplog):
    store = make_store()
    initial = await store.async_load()
    committed = await store.async_checkpoint(initial, replace(initial, processing_consent=True, current=bound()))
    original = Path(store._store.path).read_bytes()
    def fail(*args):
        raise OSError(TOKEN)
    monkeypatch.setattr(store._store, "_write_prepared_data", fail)
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR) as error:
        await store.async_checkpoint(committed, replace(committed, candidate=candidate()))
    assert TOKEN not in str(error.value)
    assert TOKEN not in caplog.text
    assert Path(store._store.path).read_bytes() == original
    for operation in (
        store.async_load(), store.async_checkpoint(committed, committed),
    ):
        with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
            await operation
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        _ = store.snapshot
    assert await make_store().async_load() == committed


async def test_read_only_store_cannot_report_checkpoint_success(make_store):
    store = make_store()
    initial = await store.async_load()
    store._store.make_read_only()
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await store.async_checkpoint(initial, initial)


@pytest.mark.parametrize("failure", [False, True, "cancel"])
async def test_caller_cancellation_does_not_interrupt_or_publish_early(make_store, monkeypatch, failure):
    store = make_store()
    initial = await store.async_load()
    started, release = asyncio.Event(), asyncio.Event()
    original_save = store._store.async_save
    async def slow(data):
        started.set()
        await release.wait()
        if failure == "cancel":
            raise asyncio.CancelledError
        if failure:
            raise OSError(TOKEN)
        await original_save(data)
    monkeypatch.setattr(store._store, "async_save", slow)
    task = asyncio.create_task(store.async_checkpoint(
        initial, replace(initial, processing_consent=True, pairing_verifier=VERIFIER),
    ))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not store._pending_write.done()
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        _ = store.snapshot
    restarted = make_store()
    loading = asyncio.create_task(restarted.async_load())
    await asyncio.sleep(0)
    assert not loading.done()
    release.set()
    if failure:
        with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
            await store._pending_write
        if failure == "cancel":
            with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
                await loading
        else:
            assert await loading == initial
    else:
        result = await store._pending_write
        assert result.revision == 1
        assert result.pairing_verifier == VERIFIER
        assert await loading == result
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        _ = store.snapshot


async def test_other_store_cannot_overwrite_a_stale_disk_revision(make_store):
    first, second = make_store(), make_store()
    old = await first.async_load()
    other = await second.async_load()
    new = await first.async_checkpoint(old, replace(old, processing_consent=True))
    with pytest.raises(HostedCredentialConflict):
        await second.async_checkpoint(other, other)
    assert await make_store().async_load() == new


async def test_internal_write_task_cancellation_is_consumed():
    task = asyncio.create_task(asyncio.sleep(60))
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    _consume_result(task)


async def test_cancelled_executor_future_blocks_all_entry_instances_before_late_write(
    make_store, monkeypatch, hass,
):
    store = make_store()
    initial = await store.async_load()
    initial = await store.async_checkpoint(initial, replace(initial, processing_consent=True))
    started, release, finished = threading.Event(), threading.Event(), threading.Event()
    original_write = store._store._write_prepared_data
    def delayed(*args):
        started.set()
        release.wait(10)
        try:
            original_write(*args)
        finally:
            finished.set()
    monkeypatch.setattr(store._store, "_write_prepared_data", delayed)
    outer = asyncio.create_task(store.async_checkpoint(initial, replace(initial, pairing_verifier=VERIFIER)))
    while not started.is_set():
        await asyncio.sleep(0.001)
    assert store._pending_write in hass._tasks
    # Match final shutdown cancellation after the real OS writer has started.
    store._pending_write.cancel("Home Assistant final writes shutdown stage")
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await outer
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await make_store().async_load()
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await store.async_checkpoint(initial, initial)
    assert await make_store("other").async_load() == CredentialCheckpoint()
    release.set()
    while not finished.is_set():
        await asyncio.sleep(0.001)
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await make_store().async_load()
    assert json.loads(Path(store._store.path).read_text())["data"]["pairing_verifier"] == VERIFIER


@pytest.mark.parametrize("raw", [
    b"", b"{", b"{}", b"null", b"[]", b"x" * 16385,
    b'{"version":1,"version":1}',
    b'{"version":true,"minor_version":1,"key":"x","data":{}}',
])
async def test_corrupt_original_is_not_renamed_or_overwritten(make_store, raw):
    store = make_store()
    path = Path(store._store.path)
    path.parent.mkdir(exist_ok=True)
    path.write_bytes(raw)
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await store.async_load()
    assert path.read_bytes() == raw
    assert not list(path.parent.glob("*.corrupt.*"))
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await store.async_load()


@pytest.mark.parametrize("change", [
    {"version":2}, {"minor_version":True}, {"minor_version":2},
    {"key":"another-entry"}, {"extra":"bad"}, {"data":None},
    {"data":{}}, {"data":{**asdict(CredentialCheckpoint()), "current":[]}},
    {"data":{**asdict(CredentialCheckpoint()), "candidate":[]}},
    {"data":{**asdict(CredentialCheckpoint()), "current":{"token":TOKEN}}},
])
async def test_bad_envelope_or_bundle_fail_closed(make_store, change):
    store = make_store()
    envelope = {"version":1, "minor_version":1, "key":store._store.key, "data":asdict(CredentialCheckpoint())}
    envelope.update(change)
    path = Path(store._store.path)
    path.parent.mkdir(exist_ok=True)
    path.write_text(json.dumps(envelope))
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await store.async_load()


async def test_missing_after_prior_corruption_requires_repair(make_store):
    store = make_store()
    path = Path(store._store.path)
    path.parent.mkdir(exist_ok=True)
    path.with_name(path.name + ".corrupt.old").write_text(TOKEN)
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await store.async_load()
    assert not path.exists()


async def test_failed_load_blocks_instance_even_after_disk_is_repaired(make_store):
    store = make_store()
    path = Path(store._store.path)
    path.parent.mkdir(exist_ok=True)
    path.write_text("broken")
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await store.async_load()
    path.unlink()
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await store.async_load()
    assert await make_store().async_load() == CredentialCheckpoint()


async def test_cancelled_load_fails_closed(make_store, monkeypatch):
    store = make_store()
    monkeypatch.setattr(store._store, "async_load", AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        await store.async_load()
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR):
        _ = store.snapshot


@pytest.mark.parametrize("kwargs", [
    {"revision":True}, {"revision":-1}, {"revision":2**63},
    {"processing_consent":"yes"}, {"pairing_verifier":"bad"},
    {"current":{}}, {"candidate":{}},
    {"pairing_verifier":VERIFIER}, {"current":bound()}, {"candidate":candidate()},
])
def test_invalid_checkpoint(kwargs):
    with pytest.raises(ValueError):
        CredentialCheckpoint(**kwargs)


@pytest.mark.parametrize("kind,kwargs", [
    (bound, {"account_id":"bad"}), (bound, {"installation_id":3}),
    (bound, {"token":"bad"}), (bound, {"expires_at":None}),
    (bound, {"expires_at":"x" * 65}), (bound, {"expires_at":"garbage"}),
    (bound, {"expires_at":"2026-12-01T00:00:00"}),
    (candidate, {"installation_id":"bad"}), (candidate, {"token":False}),
    (candidate, {"confirmation_expires_at":"2027-01-01T00:00:00+00:00"}),
])
def test_invalid_bundle(kind, kwargs):
    with pytest.raises((ValueError, TypeError)):
        kind(**kwargs)


@pytest.mark.parametrize("stamp", [
    "2026-12-01t00:00:00z", "2026-12-01T00:00:00." + "1" * 100 + "Z",
])
def test_schema_valid_deadlines_normalize_to_bounded_durable_timestamps(stamp):
    from custom_components.daylight_calendar_import.hosted_transport import _timestamp
    assert _timestamp(stamp) == stamp
    credential = bound(expires_at=stamp)
    assert len(credential.expires_at) <= 32
    staged = candidate(expires_at=stamp)
    assert staged.expires_at == credential.expires_at


def test_invalid_entry_id_and_decode(hass):
    for entry in ("../secret", "", False, "a" * 129):
        with pytest.raises(ValueError):
            HostedCredentialStore(hass, entry)
    for value in (None, [], {}, {**asdict(CredentialCheckpoint()), "extra":"bad"}):
        with pytest.raises(ValueError):
            _decode(value)


def test_read_io_failure_is_not_empty(tmp_path):
    with pytest.raises(IsADirectoryError):
        _read(str(tmp_path), "key")


@pytest.mark.parametrize("operation,message", [
    (lambda: bound(account_id="bad"), "invalid credential checkpoint"),
    (lambda: bound(expires_at=None), "invalid credential deadline"),
    (lambda: candidate(confirmation_expires_at="2027-01-01T00:00:00Z"), "invalid credential deadline ordering"),
    (lambda: CredentialCheckpoint(revision=-1), "invalid checkpoint revision"),
    (lambda: CredentialCheckpoint(processing_consent="yes"), "invalid processing consent"),
    (lambda: CredentialCheckpoint(current={}), "invalid bound credential"),
    (lambda: CredentialCheckpoint(candidate={}), "invalid candidate credential"),
    (lambda: CredentialCheckpoint(current=bound()), "credential recovery material requires processing consent"),
    (lambda: _decode({}), "invalid credential checkpoint"),
    (lambda: _decode({**asdict(CredentialCheckpoint()), "current":[]}), "invalid credential bundle"),
    (lambda: _unique([("secret",1),("secret",2)]), "duplicate checkpoint key"),
])
def test_validation_errors_have_fixed_secret_free_messages(operation, message):
    with pytest.raises(ValueError) as error:
        operation()
    assert str(error.value) == message
    assert TOKEN not in str(error.value)
    assert VERIFIER not in str(error.value)


def test_validation_boundaries_and_exact_types():
    import re
    class StringSubclass(str):
        pass
    with pytest.raises(ValueError, match="^invalid credential checkpoint$"):
        _text(StringSubclass("okay"), re.compile("okay"))
    assert _text("okay", re.compile("okay")) == "okay"
    for value in (False, 1, None):
        with pytest.raises(ValueError, match="^invalid credential deadline$"):
            _deadline(value)
    stamp = "2026-12-01T00:00:00." + "1" * (16384 - 21) + "Z"
    assert len(stamp) == 16384
    assert _deadline(stamp) == "2026-12-01T00:00:00.111111+00:00"
    with pytest.raises(ValueError, match="^invalid credential deadline$"):
        _deadline(stamp.replace("Z", "1Z"))
    assert CredentialCheckpoint(revision=2**63 - 1).revision == 2**63 - 1
    assert CredentialCheckpoint(revision=127).revision == 127
    assert candidate(confirmation_expires_at=EXPIRES).confirmation_expires_at == EXPIRES


def test_disk_read_byte_budget_duplicate_keys_and_fixed_errors(tmp_path, monkeypatch):
    from unittest.mock import mock_open
    key = "storage-key"
    data = asdict(CredentialCheckpoint())
    envelope = {"version":1, "minor_version":1, "key":key, "data":data}
    serialized = json.dumps(envelope).encode()
    maximum = serialized + b" " * (16384 - len(serialized))
    path = tmp_path / key
    path.write_bytes(maximum)
    assert _read(str(path), key) == data
    path.write_bytes(maximum + b" ")
    with pytest.raises(ValueError, match="^oversized credential checkpoint$"):
        _read(str(path), key)
    duplicate = json.dumps(envelope)[:-1] + ', "version": 1}'
    path.write_text(duplicate)
    with pytest.raises(ValueError, match="^duplicate checkpoint key$"):
        _read(str(path), key)
    path.write_text(json.dumps({**envelope, "version":1.0}))
    with pytest.raises(ValueError, match="^invalid credential envelope$"):
        _read(str(path), key)
    path.unlink()
    path.with_name(path.name + ".corrupt.old").write_text(TOKEN)
    with pytest.raises(ValueError, match="^missing credential checkpoint$"):
        _read(str(path), key)
    reader = mock_open(read_data=maximum)
    monkeypatch.setattr(Path, "open", reader)
    assert _read(str(path), key) == data
    # One byte beyond the maximum detects overflow without reading the file.
    reader().read.assert_called_once_with(16385)


async def test_fixed_storage_error_and_conflict_messages(make_store):
    store = make_store()
    initial = await store.async_load()
    current = await store.async_checkpoint(initial, replace(initial, processing_consent=True, current=bound()))
    for expected, proposed, message in (
        (initial, initial, "stale credential checkpoint"),
        (current, replace(current, revision=9), "invalid checkpoint revision"),
        (current, replace(current, candidate=candidate(), current=None), "retain current credential while staging candidate"),
    ):
        with pytest.raises(HostedCredentialConflict) as error:
            await store.async_checkpoint(expected, proposed)
        assert str(error.value) == message
    other = make_store()
    old = await other.async_load()
    await store.async_checkpoint(current, replace(current, pairing_verifier=VERIFIER))
    with pytest.raises(HostedCredentialConflict) as error:
        await other.async_checkpoint(old, old)
    assert str(error.value) == "stale persisted credential checkpoint"
    store._store.make_read_only()
    with pytest.raises(HostedCredentialStorageError, match=STORAGE_ERROR) as error:
        await store._store.async_save({})
    assert str(error.value) == "Hosted credential storage needs repair; preserve the original file."
