"""Durable operation identity and v2 wire recovery contract regressions."""
import copy
import json
from uuid import uuid4
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.daylight_calendar_import.hosted_operations import (
    digest, payload_digest, source_identity, validate_envelope, validate_records,
)
from custom_components.daylight_calendar_import.hosted_parse_transport import (
    HostedParseError, HostedParseTransport,
)
from custom_components.daylight_calendar_import.hosted_transport import HostedServiceBinding


SERVICE = HostedServiceBinding("https://api.daylight.test", "https://app.daylight.test")
TOKEN = "T" * 43
KEY = str(uuid4())
SHA = "a" * 64


class Response:
    def __init__(self, value=None, *, status=200, headers=None, content_type="application/json", raw=None):
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type
        self.raw = json.dumps(value).encode() if raw is None else raw
        self.content = self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_):
        return None

    async def iter_chunked(self, size):
        for offset in range(0, len(self.raw), size):
            yield self.raw[offset:offset + size]


@pytest.fixture
def transport():
    session = MagicMock(spec=aiohttp.ClientSession)
    return HostedParseTransport(session, SERVICE), session


def test_operation_identity_is_stable_across_ephemeral_fields():
    request = {"source": {"id": "first", "received_at": "today", "text": "Lunch"}}
    other = copy.deepcopy(request)
    other["source"].update(id="second", received_at="tomorrow")
    original = copy.deepcopy(request)
    attachments = (("x", "image/png", b"image"),)
    assert source_identity(request, attachments, namespace="n", upstream=None) == source_identity(
        other, attachments, namespace="n", upstream=None)
    assert request == original
    assert source_identity(request, attachments, namespace="n", upstream="upstream") != source_identity(
        request, attachments, namespace="n", upstream=None)
    assert source_identity(request, attachments, namespace="n", upstream=None, reparse="new") != source_identity(
        request, attachments, namespace="n", upstream=None)
    assert source_identity(request, attachments, namespace="different", upstream=None) != source_identity(
        request, attachments, namespace="n", upstream=None)
    assert digest({"b": 2, "a": 1}) == digest({"a": 1, "b": 2})
    assert payload_digest(request, attachments) != payload_digest(request, (("x", "image/png", b"changed"),))
    assert payload_digest(request, attachments) != payload_digest(request, (("y", "image/png", b"image"),))


def test_operation_journal_is_validated_and_defensively_copied():
    record = {"key": KEY, "digest": SHA, "request_id": "r",
              "received_at": "2026-11-17T00:00:00Z",
              "reference_datetime": "2026-11-17T00:00:00Z", "accepted": False}
    records = {SHA: record}
    assert validate_records(records) == records
    assert validate_records(records) is not records
    assert validate_records(records)[SHA] is not record
    for invalid in ([], None, {"x": record}, {SHA: []},
                    {SHA: {**record, "accepted": 1}},
                    {SHA: {**record, "key": "invalid"}},
                    {SHA: {**record, "digest": "short"}},
                    {SHA: {**record, "request_id": ""}},
                    {SHA: {**record, "received_at": "x" * 129}},
                    {SHA: {**record, "reference_datetime": None}},
                    {SHA: {**record, "extra": "x"}}):
        with pytest.raises((ValueError, TypeError)):
            validate_records(invalid)
    with pytest.raises(ValueError):
        validate_records({str(i): record for i in range(10001)})


@pytest.mark.parametrize("status,result", [
    ("pending", None), ("uncertain", None), ("failed", None),
    ("expired", None), ("succeeded", {"events": []}),
])
def test_operation_envelope_contract(status, result):
    envelope = {"schema_version": 2, "operation_id": KEY, "payload_sha256": SHA,
                "status": status, "result": result, "replay_expires_at": "2999-01-01T00:00:00Z" if status == "succeeded" else None}
    assert validate_envelope(envelope, KEY, SHA) is envelope
    mutations = [
        {**envelope, "schema_version": 1}, {**envelope, "operation_id": str(uuid4())},
        {**envelope, "payload_sha256": "b" * 64},
        {**envelope, "status": "unknown"}, {**envelope, "extra": 1},
        {**envelope, "result": None if status == "succeeded" else {}},
    ]
    for invalid in mutations:
        with pytest.raises(ValueError):
            validate_envelope(invalid, KEY, SHA)
    with pytest.raises(ValueError):
        validate_envelope([], KEY, SHA)


async def test_v2_post_uses_idempotency_key_and_accepts_pending(transport):
    client, session = transport
    session.request.return_value = Response({"status": "pending"}, status=202)
    assert await client.async_parse({"source": {}}, (), token=TOKEN, operation_key=KEY) == {"status": "pending"}
    args, kwargs = session.request.call_args
    assert args == ("POST", SERVICE.api_origin + "/v2/parse")
    assert kwargs["headers"]["Idempotency-Key"] == KEY
    session.request.return_value = Response({}, status=409)
    with pytest.raises(HostedParseError) as error:
        await client.async_parse({}, (), token=TOKEN, operation_key=KEY)
    assert (error.value.code, error.value.status) == ("operation_conflict", 409)


async def test_operation_get_recovery_and_account_allowlist(transport):
    client, session = transport
    session.request.return_value = Response({"status": "pending"})
    assert await client.async_operation(KEY, token=TOKEN) == {"status": "pending"}
    args, kwargs = session.request.call_args
    assert args == ("GET", SERVICE.api_origin + "/v2/parse/operations/" + KEY)
    assert kwargs["allow_redirects"] is False
    assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
    session.request.return_value = Response({"state": "active"})
    assert await client.async_v2_get("/account", token=TOKEN) == {"state": "active"}
    session.request.return_value = Response(status=404)
    assert await client.async_operation(KEY, token=TOKEN) is None
    with pytest.raises(HostedParseError) as error:
        await client.async_v2_get("/admin", token=TOKEN)
    assert error.value.code == "invalid_request"
    with pytest.raises((ValueError, HostedParseError)):
        await client.async_operation("not-a-uuid", token=TOKEN)


@pytest.mark.parametrize("response", [
    Response({}, status=302),
    Response({}, headers={"Content-Encoding": "gzip"}),
    Response({}, content_type="text/html"),
    Response(raw=b"[]"),
    Response(raw=b"{"),
    Response(raw=b'{"a":1,"a":2}'),
])
async def test_operation_get_rejects_untrusted_response(transport, response):
    client, session = transport
    session.request.return_value = response
    with pytest.raises(HostedParseError) as error:
        await client.async_v2_get("/account", token=TOKEN)
    assert error.value.code == "provider_invalid_response"


@pytest.mark.parametrize("failure", [TimeoutError("private"), aiohttp.ClientConnectionError("private")])
async def test_operation_get_network_failure_remains_uncertain(transport, failure):
    client, session = transport
    session.request.side_effect = failure
    with pytest.raises(HostedParseError) as error:
        await client.async_v2_get("/account", token=TOKEN)
    assert error.value.code == "operation_recovery_required"
    assert error.value.uncertain
    assert str(failure) not in str(error.value)


async def test_operation_get_enforces_response_limit(transport, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_parse_transport as module
    client, session = transport
    session.request.return_value = Response({"value": "1234567890"})
    monkeypatch.setattr(module, "MAX_PARSE_RESPONSE_BYTES", 5)
    with pytest.raises(HostedParseError) as error:
        await client.async_v2_get("/account", token=TOKEN)
    assert error.value.code == "provider_invalid_response"


async def test_v2_operation_rejects_noncanonical_uuid(transport):
    client, session = transport
    with pytest.raises(HostedParseError) as error:
        await client.async_operation(KEY.upper(), token=TOKEN)
    assert error.value.code == "invalid_request"
    session.request.assert_not_called()


async def test_v2_get_preserves_structured_api_errors(transport):
    client, session = transport
    session.request.return_value = Response({
        "schema_version": 1,
        "error": {"code": "provider_rate_limited", "message": "private",
                  "retryable": True},
    }, status=429)
    with pytest.raises(HostedParseError) as error:
        await client.async_v2_get("/account", token=TOKEN)
    assert error.value.code == "provider_rate_limited"
    assert error.value.status == 429
    assert "private" not in str(error.value)
