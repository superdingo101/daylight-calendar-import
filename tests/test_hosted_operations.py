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
from custom_components.daylight_calendar_import.hosted_parse_transport import HostedParseError
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
    source = TextSourceAdapter().create("Meeting")
    with pytest.raises(HostedParseError):
        await provider.async_parse(source, reference_datetime=source.received_at.isoformat(), time_zone="UTC")
    record = next(iter(store._hosted_operations.values()))
    restarted = PendingImportStore(hass)
    restarted._store = backend
    await restarted.async_load()
    provider._operation_store = restarted
    provider._transport.async_operation = AsyncMock(return_value=envelope(record["key"], record["digest"]))
    retry = TextSourceAdapter().create("Meeting")
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
