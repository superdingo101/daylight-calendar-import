"""Production durable operation persistence and recovery; no model calls."""
import asyncio
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import json
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
import voluptuous as vol

from custom_components.daylight_calendar_import.hosted_operations import (
    payload_digest, source_identity, validate_account, validate_envelope, validate_records,
)
from custom_components.daylight_calendar_import.hosted_parse_transport import (
    HostedParseError, HostedParseNotAdmitted,
)
from custom_components.daylight_calendar_import.hosted_parser import DaylightHostedParserProvider
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.storage import PendingImportStore, PendingImportStorageError
from custom_components.daylight_calendar_import.sources import TextSourceAdapter
from test_hosted_parser import checkpoint, SERVICE, result
from test_hosted_parse_transport import Response, transport


class Backend:
    def __init__(self):
        self.data = None
        self.fail = False

    async def async_load(self):
        return deepcopy(self.data)

    async def async_save(self, data):
        self.data = deepcopy(data)
        if self.fail:
            raise OSError("uncertain write")

    async_save_immediate = async_save


def request():
    return {"schema_version": 1, "reference_datetime": "2026-11-17T10:00:00Z", "time_zone": "UTC",
            "source": {"id": "source", "kind": "manual_text", "received_at": "2026-11-17T09:00:00Z",
                       "title": None, "text": "Meeting", "attachments": [], "metadata": {}}}


def envelope(key, digest, *, status="succeeded"):
    return {"schema_version": 2, "operation_id": key, "payload_sha256": digest,
            "status": status, "result": result() if status == "succeeded" else None,
            "replay_expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat()
            if status == "succeeded" else None}


async def test_restart_payload_binding_and_failed_write_fails_closed(hass):
    backend = Backend()
    store = PendingImportStore(hass)
    store._store = backend
    record, fresh = await store.async_prepare_hosted_operation("a" * 64, request(), ())
    assert fresh and record["digest"] == payload_digest(request(), ())
    assert "Meeting" not in json.dumps(backend.data)
    restarted = PendingImportStore(hass)
    restarted._store = backend
    await restarted.async_load()
    retry = request()
    retry["source"]["id"] = "regenerated"
    retry["reference_datetime"] = "2026-11-18T10:00:00Z"
    retry["time_zone"] = "America/Los_Angeles"
    recovered, fresh = await restarted.async_prepare_hosted_operation("a" * 64, retry, ())
    assert recovered == record and not fresh and retry == request()
    retry["source"]["text"] = "Changed"
    with pytest.raises(ValueError):
        await restarted.async_prepare_hosted_operation("a" * 64, retry, ())
    backend.fail = True
    with pytest.raises(OSError):
        await restarted.async_prepare_hosted_operation("b" * 64, request(), ())
    with pytest.raises(PendingImportStorageError):
        await restarted.async_prepare_hosted_operation("c" * 64, request(), ())


@pytest.mark.parametrize("events", [(), (EventDraft(title="Meeting",start="2026-12-01T10:00:00Z",end="2026-12-01T11:00:00Z",all_day=False),)])
async def test_local_acceptance_tombstone_commits_atomically_with_pending(hass, events):
    backend = Backend()
    store = PendingImportStore(hass)
    store._store = backend
    record, _ = await store.async_prepare_hosted_operation("a" * 64, request(), ())
    added = await store.async_add(source_text="Meeting", events=events,
                                 hosted_operation_id=record["key"])
    assert (added.pending is not None) == bool(events)
    # Deduplication clearing must never erase operation acceptance.
    store._seen_source_fingerprints = store._seen_event_fingerprints = ()
    restarted = PendingImportStore(hass)
    restarted._store = backend
    await restarted.async_load()
    replay = await restarted.async_add(source_text="Meeting", events=events,
                                      reimport_duplicates=True, hosted_operation_id=record["key"])
    assert replay.duplicate_source
    assert len(restarted.list()) == len(events)
    assert backend.data["hosted_parse_operations"]["a" * 64]["accepted"] is True
    with pytest.raises(ValueError):
        await restarted.async_add(source_text="Meeting", events=events, hosted_operation_id=str(uuid4()))


async def test_provider_lost_response_recovers_without_second_post_and_rotation(hass):
    backend = Backend()
    store = PendingImportStore(hass)
    store._store = backend
    saved = checkpoint()
    provider = DaylightHostedParserProvider(hass, service_binding=SERVICE, checkpoint=lambda: saved,
                                           active=lambda: True, operation_store=store,
                                           operation_namespace="entry:service")
    provider._transport.async_parse = AsyncMock(side_effect=HostedParseError("provider_timeout", uncertain=True))
    source = TextSourceAdapter().create("Meeting", source_id="stable-source")
    with pytest.raises(HostedParseError):
        await provider.async_parse(source, reference_datetime=source.received_at.isoformat(), time_zone="UTC")
    record = next(iter(store._hosted_operations.values()))
    restarted = PendingImportStore(hass)
    restarted._store = backend
    await restarted.async_load()
    provider._operation_store = restarted
    provider._transport.async_operation = AsyncMock(return_value=envelope(record["key"], record["digest"]))
    retry = TextSourceAdapter().create("Meeting", source_id="stable-source")
    outcome = await provider.async_parse(retry, reference_datetime=retry.received_at.isoformat(), time_zone="UTC")
    assert len(outcome.events) == 1 and provider._transport.async_parse.await_count == 1
    assert provider.last_operation_id == record["key"]
    provider._transport.async_operation.return_value = envelope(record["key"], record["digest"], status="uncertain")
    with pytest.raises(HostedParseError) as error:
        await provider.async_parse(retry, reference_datetime=retry.received_at.isoformat(), time_zone="UTC")
    assert error.value.code == "operation_uncertain" and provider._transport.async_parse.await_count == 1


def test_binding_explicit_reparse_and_strict_envelopes():
    req = request()
    assert payload_digest(req, (("a", "image/png", b"one"),)) != payload_digest(req, (("a", "image/png", b"two"),))
    normal = source_identity(req, (), namespace="account", upstream=None)
    assert normal != source_identity(req, (), namespace="other", upstream=None)
    assert normal != source_identity(req, (), namespace="account", upstream=None, reparse="explicit")
    key, digest = str(uuid4()), "a" * 64
    assert validate_envelope(envelope(key, digest), key, digest)["status"] == "succeeded"
    for change in ({"schema_version": True}, {"operation_id": str(uuid4())}, {"status": "unknown"},
                   {"payload_sha256": "b" * 64}, {"result": None}, {"replay_expires_at": None},
                   {"replay_expires_at": "2020-01-01T00:00:00Z"}):
        with pytest.raises(ValueError):
            validate_envelope({**envelope(key, digest), **change}, key, digest)
    assert validate_records({}) == {}
    for malformed in ([], {"x": {}}, {"a" * 64: {"key": "bad"}}):
        with pytest.raises(ValueError):
            validate_records(malformed)


def account_status():
    return {"schema_version": 2, "parse_operations_v2": True,
            "capabilities": {"managed_ai": True, "new_email_intake": True, "accepted_source_drain": True},
            "billing": {"state": "trial", "trial_ends_at": "2099-01-01T00:00:00Z",
                        "trial_remaining": 20, "paid_remaining": 0, "period_ends_at": None,
                        "grace_ends_at": None, "cancel_at_period_end": False},
            "drain_cutoff_at": None, "account_url": SERVICE.account_origin + "/account"}


def test_strict_account_status_excludes_provider_ids_and_foreign_actions():
    value = account_status()
    assert validate_account(value, SERVICE.account_origin) == value
    for changed in ({"schema_version": 1}, {"extra": "provider"}, {"capabilities": {}},
                    {"capabilities": {**value["capabilities"], "managed_ai": 1}},
                    {"billing": {}}, {"account_url": "https://evil.test/account"},
                    {"account_url": SERVICE.account_origin + "/account?customer=forged"}):
        with pytest.raises(ValueError):
            validate_account({**value, **changed}, SERVICE.account_origin)
    for key, invalid in (("state", "forged"), ("trial_remaining", True), ("paid_remaining", 101),
                         ("cancel_at_period_end", 1), ("trial_ends_at", "invalid")):
        with pytest.raises((ValueError, vol.Invalid)):
            validate_account({**value, "billing": {**value["billing"], key: invalid}}, SERVICE.account_origin)


async def test_operation_transport_wire_binding_and_recovery(transport):
    client, session = transport
    key = str(uuid4())
    session.request.return_value = Response(value={"okay": True}, status=202)
    assert await client.async_parse(request(), (), token="T" * 43, operation_key=key) == {"okay": True}
    assert session.request.call_args.args[1].endswith("/v2/parse")
    assert session.request.call_args.kwargs["headers"]["Idempotency-Key"] == key
    session.request.return_value = Response(value={"okay": True})
    assert await client.async_operation(key, token="T" * 43) == {"okay": True}
    assert session.request.call_args.args[0] == "GET"
    assert session.request.call_args.args[1].endswith("/v2/parse/operations/" + key)
    session.request.return_value = Response(status=404)
    assert await client.async_operation(key, token="T" * 43) is None
    with pytest.raises(HostedParseError):
        await client.async_operation(key.upper(), token="T" * 43)
    with pytest.raises(HostedParseError):
        await client.async_v2_get("/forged", token="T" * 43)
    session.request.return_value = Response(value={}, status=409)
    with pytest.raises(HostedParseError) as error:
        await client.async_parse(request(), (), token="T" * 43, operation_key=key)
    assert error.value.code == "operation_conflict"


@pytest.mark.parametrize("response", [Response(status=302), Response(headers={"Content-Encoding": "gzip"}),
                                    Response(raw=b"invalid"), Response(value=[]),
                                    Response(value={"schema_version": 1, "error": {"code": "forbidden", "message": "No", "retryable": False}}, status=403)])
async def test_operation_recovery_rejects_redirects_encoding_and_invalid_payload(transport, response):
    client, session = transport
    session.request.return_value = response
    with pytest.raises(HostedParseError):
        await client.async_v2_get("/account", token="T" * 43)


@pytest.mark.parametrize("error", [TimeoutError(), __import__('aiohttp').ClientConnectionError()])
async def test_operation_recovery_transport_uncertainty(transport, error):
    client, session = transport
    session.request.side_effect = error
    with pytest.raises(HostedParseError) as failure:
        await client.async_operation(str(uuid4()), token="T" * 43)
    assert failure.value.uncertain


@pytest.mark.parametrize("invalid", ["invalid", 123, [], "2026-02-30T00:00:00Z"])
def test_operation_invalid_expiry_is_sanitized(invalid):
    key = str(uuid4())
    value = envelope(key, "a" * 64)
    value["replay_expires_at"] = invalid
    with pytest.raises(ValueError, match="^invalid operation expiry$"):
        validate_envelope(value, key, "a" * 64)


def test_operation_expiry_boundary_and_lowercase_utc(freezer):
    freezer.move_to("2026-11-17T10:00:00Z")
    key = str(uuid4())
    value = envelope(key, "a" * 64)
    for expiry in (None, "2026-11-17T09:59:59Z", "2026-11-17T10:00:00Z"):
        value["replay_expires_at"] = expiry
        with pytest.raises(ValueError, match="^expired operation result$"):
            validate_envelope(value, key, "a" * 64)
    value["replay_expires_at"] = "2026-11-17t10:00:01z"
    assert validate_envelope(value, key, "a" * 64) is value
    value["status"] = "pending"
    value["result"] = None
    value["replay_expires_at"] = "2026-11-17T09:59:59Z"
    assert validate_envelope(value, key, "a" * 64) is value


@pytest.mark.parametrize("url", [None, [], "https://[invalid/account",
                                 "https://app.daylight.test/acc\nount"])
def test_account_url_must_be_exact_trusted_destination(url):
    value = account_status()
    value["account_url"] = url
    with pytest.raises(ValueError, match="^untrusted account action$"):
        validate_account(value, SERVICE.account_origin)


def test_account_non_string_billing_state_is_sanitized():
    value = account_status()
    value["billing"]["state"] = []
    with pytest.raises(ValueError, match="^invalid billing status$"):
        validate_account(value, SERVICE.account_origin)


@pytest.mark.parametrize("missing", [True, False])
async def test_operation_journal_load_failure_fences_existing_store(hass, missing):
    backend = Backend()
    store = PendingImportStore(hass)
    store._store = backend
    record, _ = await store.async_prepare_hosted_operation("a" * 64, request(), ())
    store.accepting_services = True
    if missing:
        backend.data = None
    else:
        backend.data["hosted_parse_operations"] = {"bad": {}}
    with pytest.raises(PendingImportStorageError):
        await store.async_load()
    assert store.accepting_services is False
    assert store._hosted_operations["a" * 64] == record
    with pytest.raises(PendingImportStorageError):
        await store.async_prepare_hosted_operation("b" * 64, request(), ())


@pytest.mark.parametrize("claim_matches", [True, False, None])
async def test_accepted_operation_replay_releases_only_matching_claim(hass, claim_matches):
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    backend = Backend()
    store = PendingImportStore(hass)
    store._store = backend
    record, _ = await store.async_prepare_hosted_operation("a" * 64, request(), ())
    await store.async_add(source_text="Meeting", events=(), hosted_operation_id=record["key"])
    fingerprint = source_fingerprint("source" if claim_matches else "other")
    store._source_claims = {"claim": fingerprint}
    source_id = None if claim_matches is None else "source"
    before = deepcopy(backend.data)
    kwargs = dict(source_text="Meeting", events=(), hosted_operation_id=record["key"],
                  activity_id="claim", source_id=source_id)
    if claim_matches:
        result = await store.async_add(**kwargs)
        assert result.duplicate_source and result.pending is None
        assert store._source_claims == {}
        assert backend.data["hosted_parse_operations"]["a" * 64]["accepted"] is True
    else:
        with pytest.raises(ValueError, match="^source_id does not match claimed source$"):
            await store.async_add(**kwargs)
        assert backend.data == before
        assert store._source_claims == {"claim": fingerprint}


@pytest.mark.parametrize("status", ["pending", "uncertain", "failed", "expired"])
async def test_delivery_retry_rekeys_only_verified_terminal_operation(hass, status):
    from dataclasses import replace
    from custom_components.daylight_calendar_import.sources import SourceKind
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    backend = Backend()
    store = PendingImportStore(hass)
    store._store = backend
    saved = checkpoint()
    provider = DaylightHostedParserProvider(
        hass, service_binding=SERVICE, checkpoint=lambda: saved, active=lambda: True,
        operation_store=store, operation_namespace="entry:service",
    )
    source = replace(TextSourceAdapter().create("Meeting"), kind=SourceKind.EMAIL,
                     upstream_source_id="hosted:entry:receipt-123")
    await store.async_change_hosted_parse_attempt(source.upstream_source_id)

    async def post(req, attachments, *, token, operation_key):
        return envelope(operation_key, payload_digest(req, attachments), status=status)

    provider._transport.async_parse = AsyncMock(side_effect=post)
    with pytest.raises(HostedParseError) as error:
        await provider.async_parse(source, reference_datetime=source.received_at.isoformat(), time_zone="UTC")
    assert error.value.code == "operation_" + status
    terminal = status in ("failed", "expired")
    assert error.value.uncertain is not terminal
    identity, record = next(iter(store._hosted_operations.items()))
    assert record.get("terminal_source") == (
        source_fingerprint(source.upstream_source_id) if terminal else None)
    assert store.hosted_parse_uncertain
    if terminal:
        with pytest.raises(HostedParseError, match="Daylight Cloud") as blocked:
            await provider.async_parse(source, reference_datetime=source.received_at.isoformat(), time_zone="UTC")
        assert blocked.value.code == "operation_terminal"
        assert provider._transport.async_parse.await_count == 1
        with pytest.raises(ValueError, match="terminal hosted operation cannot be accepted"):
            await store.async_add(source_text="Meeting", events=(), hosted_operation_id=record["key"])
    restarted = PendingImportStore(hass)
    restarted._store = backend
    await restarted.async_load()
    await restarted.async_change_hosted_parse_attempt(None, retry_terminal=True)
    assert not restarted.hosted_parse_uncertain
    assert (identity in restarted._hosted_operations) is not terminal
    retained = next(iter(restarted._hosted_operations.values()))
    assert retained["key"] == record["key"]
    assert retained.get("terminal_source") == record.get("terminal_source")
    provider._operation_store = restarted
    provider._transport.async_operation = AsyncMock(
        return_value=envelope(record["key"], record["digest"], status=status))
    await restarted.async_change_hosted_parse_attempt(source.upstream_source_id)
    with pytest.raises(HostedParseError):
        await provider.async_parse(source, reference_datetime=source.received_at.isoformat(), time_zone="UTC")
    if terminal:
        provider._transport.async_operation.assert_not_awaited()
        assert provider._transport.async_parse.await_count == 2
        assert provider.last_operation_id != record["key"]
        assert len(restarted._hosted_operations) == 2
        with pytest.raises(ValueError, match="terminal hosted operation cannot be accepted"):
            await restarted.async_add(source_text="Meeting", events=(), hosted_operation_id=record["key"])
        new_key = provider.last_operation_id
        await restarted.async_change_hosted_parse_attempt(None, retry_terminal=True)
        assert not restarted.hosted_parse_uncertain
        assert {row["key"] for row in restarted._hosted_operations.values()} == {record["key"], new_key}
        assert identity not in restarted._hosted_operations
    else:
        provider._transport.async_operation.assert_awaited_once_with(record["key"], token=saved.current.token)
        assert provider._transport.async_parse.await_count == 1
        assert provider.last_operation_id == record["key"]


async def test_terminal_marker_cannot_retire_unrelated_or_accepted_operation(hass):
    backend = Backend()
    store = PendingImportStore(hass)
    store._store = backend
    identity = "a" * 64
    record, _ = await store.async_prepare_hosted_operation(identity, request(), ())
    await store.async_mark_hosted_operation_terminal(identity, record["key"], "different")
    assert store._hosted_operations[identity] == record
    await store.async_change_hosted_parse_attempt("source")
    for wrong_identity, wrong_key in (("b" * 64, record["key"]), (identity, "different")):
        await store.async_mark_hosted_operation_terminal(wrong_identity, wrong_key, "source")
        assert store._hosted_operations[identity] == record
    await store.async_add(source_text="Meeting", events=(), hosted_operation_id=record["key"])
    await store.async_mark_hosted_operation_terminal(identity, record["key"], "source")
    assert store._hosted_operations[identity]["accepted"] is True
    assert "terminal_source" not in store._hosted_operations[identity]
    store.close_hosted_writes()
    with pytest.raises(PendingImportStorageError):
        await store.async_mark_hosted_operation_terminal(identity, record["key"], "source")


@pytest.mark.parametrize("conflict", ["archive", "ambiguous"])
async def test_explicit_terminal_retry_conflicts_preserve_entire_journal(hass, conflict):
    from custom_components.daylight_calendar_import.hosted_operations import digest

    store = PendingImportStore(hass)
    backend = Backend()
    store._store = backend
    identity = "a" * 64
    record, _ = await store.async_prepare_hosted_operation(identity, request(), ())
    await store.async_change_hosted_parse_attempt("source")
    await store.async_mark_hosted_operation_terminal(identity, record["key"], "source")
    other = digest({"retired_operation": record["key"]}) if conflict == "archive" else "b" * 64
    other_record, _ = await store.async_prepare_hosted_operation(other, request(), ())
    if conflict == "ambiguous":
        await store.async_mark_hosted_operation_terminal(other, other_record["key"], "source")
    before = deepcopy(backend.data)
    with pytest.raises(ValueError, match="operation archive conflict|ambiguous terminal operation"):
        await store.async_change_hosted_parse_attempt(None, retry_terminal=True)
    assert backend.data == before
    assert store.hosted_parse_uncertain


def test_terminal_journal_marker_requires_valid_source_fingerprint_and_unaccepted_operation():
    from tests.test_hosted_mutation_contracts import record
    from custom_components.daylight_calendar_import.dedup import source_fingerprint

    identity = "a" * 64
    legacy = record()
    del legacy["time_zone"]
    assert validate_records({identity: legacy}) == {identity: legacy}
    marked = {**record(), "terminal_source": source_fingerprint("source")}
    assert validate_records({identity: marked}) == {identity: marked}
    for invalid in ({**marked, "accepted": True}, {**marked, "terminal_source": 1},
                    {**marked, "terminal_source": "wrong"}):
        with pytest.raises(ValueError, match="invalid operation journal"):
            validate_records({identity: invalid})


async def test_cancelled_provider_preparation_never_dispatches_and_reuses_key(hass, monkeypatch):
    backend = Backend()
    store = PendingImportStore(hass)
    store._store = backend
    saved = checkpoint()
    provider = DaylightHostedParserProvider(
        hass, service_binding=SERVICE, checkpoint=lambda: saved, active=lambda: True,
        operation_store=store, operation_namespace="entry:service",
    )
    started, release = asyncio.Event(), asyncio.Event()
    original = backend.async_save_immediate

    async def delayed(data):
        started.set()
        await release.wait()
        await original(data)

    monkeypatch.setattr(backend, "async_save_immediate", delayed)
    provider._transport.async_parse = AsyncMock()
    provider._transport.async_operation = AsyncMock()
    source = TextSourceAdapter().create("Meeting", source_id="stable-cancel")
    task = asyncio.create_task(provider.async_parse(
        source, reference_datetime=source.received_at.isoformat(), time_zone="UTC",
    ))
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done()
        assert store._lock.locked()
        provider._transport.async_parse.assert_not_awaited()
        provider._transport.async_operation.assert_not_awaited()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(task, timeout=5)
    provider._transport.async_parse.assert_not_awaited()
    provider._transport.async_operation.assert_not_awaited()
    assert provider.last_operation_id is None
    assert not store._storage_blocked
    row = next(iter(backend.data["hosted_parse_operations"].values()))
    restarted = PendingImportStore(hass)
    restarted._store = backend
    await restarted.async_load()
    provider._operation_store = restarted
    provider._transport.async_operation.return_value = envelope(row["key"], row["digest"])
    retry = TextSourceAdapter().create("Meeting", source_id="stable-cancel")
    outcome = await provider.async_parse(retry, reference_datetime=retry.received_at.isoformat(), time_zone="UTC")
    assert len(outcome.events) == 1
    assert provider.last_operation_id == row["key"]
    provider._transport.async_operation.assert_awaited_once_with(row["key"], token=saved.current.token)
    provider._transport.async_parse.assert_not_awaited()


@pytest.mark.parametrize("mode", ["novel", "empty", "unknown", "terminal", "closed", "blocked", "failed"])
async def test_reprocess_operation_acceptance_is_atomic_and_fenced(hass, mode):
    from custom_components.daylight_calendar_import.storage import PendingEvent
    from tests.test_storage import draft

    backend = Backend()
    store = PendingImportStore(hass)
    store._store = backend
    original = (await store.async_add(source_text="Meeting", events=[draft()])).pending
    identity = "a" * 64
    record, _ = await store.async_prepare_hosted_operation(identity, request(), ())
    if mode == "terminal":
        await store.async_change_hosted_parse_attempt("source")
        await store.async_mark_hosted_operation_terminal(identity, record["key"], "source")
    if mode == "closed":
        store.close_hosted_writes()
    if mode == "blocked":
        store._storage_blocked = True
    before = deepcopy(backend.data)
    candidate = PendingEvent.create(EventDraft(
        title="New meeting", start="2026-12-02T10:00:00Z", end="2026-12-02T11:00:00Z",
        all_day=False,
    ), None)

    async def parser(item):
        assert item is original
        return (() if mode == "empty" else (candidate,)), ("New warning",)

    def guard():
        pass

    guard.hosted_operation_id = "unknown" if mode == "unknown" else record["key"]
    if mode in ("unknown", "terminal", "closed", "blocked", "failed"):
        if mode == "failed":
            backend.fail = True
        error = (OSError if mode == "failed" else PendingImportStorageError
                 if mode in ("closed", "blocked") else ValueError)
        with pytest.raises(error):
            await store.async_reprocess(original.id, parser, acceptance_guard=guard)
        assert store.get(original.id) is original
        assert store._hosted_operations[identity]["accepted"] is False
        if mode != "failed":
            assert backend.data == before
        else:
            assert store._storage_blocked and not store.accepting_services
            assert backend.data["hosted_parse_operations"][identity]["accepted"] is True
        return
    updated, added, duplicates = await store.async_reprocess(original.id, parser, acceptance_guard=guard)
    assert added == (0 if mode == "empty" else 1) and duplicates == 0
    assert updated.events == original.events + (() if mode == "empty" else (candidate,))
    assert updated.warnings == ("New warning",)
    assert backend.data["hosted_parse_operations"][identity]["accepted"] is True
    assert backend.data["items"] == [updated.as_dict()]
    restarted = PendingImportStore(hass)
    restarted._store = backend
    await restarted.async_load()
    async def replay(item):
        return (candidate,), ("Must not be appended again",)
    restored = restarted.get(original.id)
    snapshot = deepcopy(backend.data)
    assert await restarted.async_reprocess(original.id, replay, acceptance_guard=guard) == (restored, 0, 0)
    assert backend.data == snapshot


@pytest.mark.parametrize("accepted", [False, True])
@pytest.mark.parametrize("fence", ["closed", "blocked"])
async def test_manual_operation_acceptance_respects_storage_owner_fence(hass, accepted, fence):
    backend = Backend()
    store = PendingImportStore(hass)
    store._store = backend
    record, _ = await store.async_prepare_hosted_operation("a" * 64, request(), ())
    if accepted:
        await store.async_add(source_text="Meeting", events=(), hosted_operation_id=record["key"])
    if fence == "closed":
        store.close_hosted_writes()
    else:
        store._storage_blocked = True
    before = deepcopy(backend.data)
    with pytest.raises(PendingImportStorageError):
        await store.async_add(source_text="Meeting", events=(), hosted_operation_id=record["key"])
    assert backend.data == before
    assert store._hosted_operations["a" * 64]["accepted"] is accepted
    assert not store.list()


@pytest.mark.parametrize("proof", [{"error": {"code": "operation_not_found"}}, {"detail": "Not Found"}])
async def test_operation_absence_requires_canonical_authenticated_response(transport, proof):
    client, session = transport
    key = str(uuid4())
    session.request.return_value = Response(value=proof, status=404)
    if "error" in proof:
        assert await client.async_operation(key, token="T" * 43, prove_absence=True) is None
    else:
        with pytest.raises(HostedParseError):
            await client.async_operation(key, token="T" * 43, prove_absence=True)


@pytest.mark.parametrize("recovery", ["missing", "uncertain", "denied"])
async def test_retryable_503_proves_absence_or_retains_the_operation(hass, recovery):
    store = PendingImportStore(hass)
    store._store = Backend()
    saved = checkpoint()
    provider = DaylightHostedParserProvider(
        hass, service_binding=SERVICE, checkpoint=lambda: saved,
        active=lambda: True, operation_store=store, operation_namespace="entry:service",
    )

    async def recover(key, *, token, prove_absence=False):
        assert token == saved.current.token and prove_absence
        record = next(iter(store._hosted_operations.values()))
        assert key == record["key"]
        if recovery == "denied":
            raise HostedParseError("forbidden", status=403)
        if recovery == "uncertain":
            return envelope(key, record["digest"], status="uncertain")
        return None

    provider._transport.async_operation = AsyncMock(side_effect=recover)
    post = AsyncMock(side_effect=HostedParseError(
        "provider_unavailable", status=503, retryable=True, retry_after=30,
    ))
    provider._transport.async_parse = post
    source = TextSourceAdapter().create("Meeting", source_id="stable-source")
    with pytest.raises(HostedParseError) as caught:
        await provider.async_parse(
            source, reference_datetime=source.received_at.isoformat(), time_zone="UTC",
        )
    record = next(iter(store._hosted_operations.values()))
    assert post.await_count == 1
    assert provider.last_operation_id == record["key"]
    if recovery != "missing":
        assert caught.value.uncertain and not isinstance(caught.value, HostedParseNotAdmitted)
        return
    assert isinstance(caught.value, HostedParseNotAdmitted) and caught.value.retry_after == 30
    provider._transport.async_operation = AsyncMock(return_value=None)
    post.side_effect = None
    post.return_value = envelope(record["key"], record["digest"])
    await provider.async_parse(
        source, reference_datetime=source.received_at.isoformat(), time_zone="UTC",
    )
    assert [call.kwargs["operation_key"] for call in post.await_args_list] == [record["key"]] * 2


async def test_journal_capacity_compacts_accepted_evidence_across_restart(hass, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_operations

    monkeypatch.setattr(hosted_operations, "_MAX_ACTIVE_OPERATIONS", 2)
    monkeypatch.setattr(hosted_operations, "_MAX_COMPACTED_OPERATIONS", 1)
    backend = Backend()
    store = PendingImportStore(hass)
    store._store = backend
    accepted, _ = await store.async_prepare_hosted_operation("a" * 64, request(), ())
    await store.async_add(source_text="Meeting", events=(), hosted_operation_id=accepted["key"])
    unresolved, _ = await store.async_prepare_hosted_operation("b" * 64, request(), ())
    new, fresh = await store.async_prepare_hosted_operation("c" * 64, request(), ())
    assert fresh and new["key"] not in {accepted["key"], unresolved["key"]}
    assert backend.data["hosted_parse_operations"]["a" * 64] == {
        "key": accepted["key"], "digest": accepted["digest"], "accepted": True, "compacted": True,
    }
    restarted = PendingImportStore(hass)
    restarted._store = backend
    await restarted.async_load()
    record, fresh = await restarted.async_prepare_hosted_operation("b" * 64, request(), ())
    assert record == unresolved and not fresh
    record, fresh = await restarted.async_prepare_hosted_operation("a" * 64, request(), ())
    assert record["accepted"] and record["key"] == accepted["key"] and not fresh
    replay = await restarted.async_add(
        source_text="Meeting", events=(), reimport_duplicates=True,
        hosted_operation_id=accepted["key"],
    )
    assert replay.duplicate_source
    with pytest.raises(ValueError, match="requires owner recovery"):
        await restarted.async_prepare_hosted_operation("d" * 64, request(), ())


async def test_journal_compaction_drops_only_already_retired_terminal_keys(hass, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_operations

    monkeypatch.setattr(hosted_operations, "_MAX_ACTIVE_OPERATIONS", 2)
    store = PendingImportStore(hass)
    store._store = Backend()
    retired, _ = await store.async_prepare_hosted_operation("a" * 64, request(), ())
    await store.async_change_hosted_parse_attempt("inbound-source-id")
    await store.async_mark_hosted_operation_terminal("a" * 64, retired["key"], "inbound-source-id")
    await store.async_change_hosted_parse_attempt(None, retry_terminal=True)
    unresolved, _ = await store.async_prepare_hosted_operation("b" * 64, request(), ())
    replacement, fresh = await store.async_prepare_hosted_operation("a" * 64, request(), ())
    assert fresh and replacement["key"] not in {retired["key"], unresolved["key"]}
    assert set(store._hosted_operations) == {"a" * 64, "b" * 64}
    assert store._hosted_operations["b" * 64] == unresolved


async def test_accepted_compact_evidence_refuses_provider_replay(hass, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_operations

    monkeypatch.setattr(hosted_operations, "_MAX_ACTIVE_OPERATIONS", 1)
    store = PendingImportStore(hass)
    store._store = Backend()
    saved = checkpoint()
    provider = DaylightHostedParserProvider(
        hass, service_binding=SERVICE, checkpoint=lambda: saved,
        active=lambda: True, operation_store=store, operation_namespace="entry:service",
    )

    async def submit(*_args, **_kwargs):
        record = next(iter(store._hosted_operations.values()))
        return envelope(record["key"], record["digest"])

    provider._transport.async_parse = AsyncMock(side_effect=submit)
    provider._transport.async_operation = AsyncMock()
    source = TextSourceAdapter().create("Meeting", source_id="stable-source")
    options = {"reference_datetime": source.received_at.isoformat(), "time_zone": "UTC"}
    await provider.async_parse(source, **options)
    original = provider.last_operation_id
    await store.async_add(source_text="Meeting", events=(), hosted_operation_id=original)
    await store.async_prepare_hosted_operation("b" * 64, request(), ())
    with pytest.raises(HostedParseError, match="Daylight Cloud") as failure:
        await provider.async_parse(source, **options)
    assert failure.value.code == "operation_expired" and provider.last_operation_id == original
    provider._transport.async_parse.assert_awaited_once()
    provider._transport.async_operation.assert_not_awaited()
