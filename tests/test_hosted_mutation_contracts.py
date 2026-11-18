"""Strict M5 operation wire and journal contracts, exercised at mutation boundaries.

These tests pin canonical bytes, request constraints and fail-closed recovery,
rather than merely observing that an exception occurred.
"""
import asyncio
from hashlib import sha256
import json
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.daylight_calendar_import import hosted_parse_transport as module
from custom_components.daylight_calendar_import.hosted_operations import (
    digest, payload_digest, source_identity, validate_envelope, validate_records,
)
from custom_components.daylight_calendar_import.hosted_parse_transport import (
    HostedParseError, HostedParseTransport,
)
from custom_components.daylight_calendar_import.hosted_transport import HostedServiceBinding
from tests.test_hosted_operations_v2 import Response, KEY, SHA, TOKEN

SERVICE = HostedServiceBinding("https://api.daylight.test", "https://app.daylight.test")


def canonical(value):
    return sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                             separators=(",", ":")).encode()).hexdigest()


def test_digest_is_canonical_utf8_compact_and_finite():
    value = {"🌞": "Événement", "s": "mañana", "l": [1, 2], "a": {"x": True}}
    assert digest(value) == canonical(value)
    assert digest(value) == canonical({"a": {"x": True}, "l": [1, 2],
                                       "s": "mañana", "🌞": "Événement"})
    assert digest(value) != sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()
    for invalid in (float("nan"), float("inf"), -float("inf")):
        with pytest.raises(ValueError):
            digest({"number": invalid})


def test_payload_digest_exact_attachments_and_stable_sorting():
    request = {"source": {"text": "Café", "id": "1"}, "time_zone": "UTC"}
    a = ("imageB", "image/png", b"\x89PNG-movie")
    b = ("imageA", "application/pdf", b"%PDF-poster")
    expectation = canonical({"request": request, "attachments": [
        {"id": b[0], "media_type": b[1], "sha256": sha256(b[2]).hexdigest()},
        {"id": a[0], "media_type": a[1], "sha256": sha256(a[2]).hexdigest()},
    ]})
    assert payload_digest(request, (a, b)) == expectation
    assert payload_digest(request, (b, a)) == expectation
    assert payload_digest(request, (a, b)) != payload_digest(
        request, (("imageB", "application/pdf", a[2]), b))
    assert payload_digest(request, (a, b)) != payload_digest(
        request, (("imageB", "image/png", b"modified"), b))


def test_source_identity_exact_contract_and_attachment_order():
    request = {"source": {"id": "transient", "received_at": "2026-11-01T00:00:00Z",
                          "kind": "email", "text": "Café", "title": "School"}}
    images = (("first", "image/png", b"first"), ("second", "application/pdf", b"second"))
    expected = canonical({"namespace": "entry:origin", "upstream": "email/123",
                          "reparse": "manual-2",
                          "source": {"kind": "email", "text": "Café", "title": "School"},
                          "attachments": [
                              {"media_type": "image/png", "sha256": sha256(b"first").hexdigest()},
                              {"media_type": "application/pdf", "sha256": sha256(b"second").hexdigest()},
                          ]})
    before = json.dumps(request, sort_keys=True)
    assert source_identity(request, images, namespace="entry:origin",
                           upstream="email/123", reparse="manual-2") == expected
    assert json.dumps(request, sort_keys=True) == before
    assert source_identity(request, images[::-1], namespace="entry:origin",
                           upstream="email/123", reparse="manual-2") != expected
    for overrides in ({"namespace": "entry:other"}, {"upstream": "other"},
                      {"reparse": "manual-3"}):
        opts = {"namespace": "entry:origin", "upstream": "email/123", "reparse": "manual-2"}
        opts.update(overrides)
        assert source_identity(request, images, **opts) != expected


def record():
    return {"key": KEY, "digest": SHA, "request_id": "source",
            "received_at": "2026-11-17T12:00:00Z",
            "reference_datetime": "2026-11-17T12:00:00Z", "accepted": False}


@pytest.mark.parametrize("field", ["request_id", "received_at", "reference_datetime"])
def test_operation_journal_string_bounds_and_error_contract(field):
    for acceptable in ("x", "x" * 128):
        item = {**record(), field: acceptable}
        assert validate_records({SHA: item}) == {SHA: item}
    for forbidden in (None, 9, "", "x" * 129):
        item = {**record(), field: forbidden}
        with pytest.raises(ValueError, match="^invalid operation journal$"):
            validate_records({SHA: item})


def test_operation_journal_invalid_shape_and_exact_limit():
    base = record()
    records = {str(i).zfill(64): base for i in range(10000)}
    assert len(validate_records(records)) == 10000
    records["z" * 64] = base
    with pytest.raises(ValueError, match="^invalid operation journal$"):
        validate_records(records)
    for bad in (
        None, [], {SHA: {**base, "accepted": None}},
        {SHA: {**base, "key": "not-uuid"}},
        {SHA: {**base, "digest": "x" * 63}},
        {SHA: {**base, "accepted": 0}},
        {"short": base}, {SHA: {**base, "other": 42}},
    ):
        with pytest.raises((ValueError, TypeError), match="invalid operation journal|badly formed"):
            validate_records(bad)


@pytest.mark.parametrize("status", ["pending", "uncertain", "failed", "expired", "succeeded"])
def test_operation_envelope_error_message_and_exact_schema(status):
    result = {"events": []} if status == "succeeded" else None
    envelope = {"schema_version": 2, "operation_id": KEY, "payload_sha256": SHA,
                "status": status, "result": result, "replay_expires_at": None}
    assert validate_envelope(envelope, KEY, SHA) is envelope
    for broken in (
        {**envelope, "schema_version": 1},
        {**envelope, "operation_id": "different"},
        {**envelope, "payload_sha256": "different"},
        {**envelope, "status": "not-a-status"},
        {**envelope, "result": None if status == "succeeded" else {}},
        {**envelope, "unexpected": True},
    ):
        with pytest.raises(ValueError, match="^invalid operation envelope$"):
            validate_envelope(broken, KEY, SHA)


@pytest.fixture
def client():
    session = MagicMock(spec=aiohttp.ClientSession)
    return HostedParseTransport(session, SERVICE), session


async def test_v2_get_exact_http_request_contract(client, monkeypatch):
    transport, session = client
    timeout_values = []
    original_timeout = asyncio.timeout
    def capture_timeout(seconds):
        timeout_values.append(seconds)
        return original_timeout(seconds)
    monkeypatch.setattr(module.asyncio, "timeout", capture_timeout)
    session.request.return_value = Response({"state": "active"})
    assert await transport.async_v2_get("/account", token=TOKEN) == {"state": "active"}
    args, kwargs = session.request.call_args
    assert args == ("GET", SERVICE.api_origin + "/v2/account")
    assert set(kwargs) == {"headers", "allow_redirects", "auto_decompress", "timeout",
                           "max_line_size", "max_field_size", "max_headers"}
    assert kwargs["allow_redirects"] is False
    assert kwargs["auto_decompress"] is False
    assert kwargs["timeout"].total == module.PARSE_TIMEOUT_SECONDS
    assert timeout_values == [module.PARSE_TIMEOUT_SECONDS]
    assert (kwargs["max_line_size"], kwargs["max_field_size"], kwargs["max_headers"]) == (4096, 4096, 32)
    assert kwargs["headers"]["Accept"] == "application/json"
    assert kwargs["headers"]["Accept-Encoding"] == "identity"
    assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
    from uuid import UUID
    rid = kwargs["headers"]["X-Request-ID"]
    assert str(UUID(rid)) == rid
    assert set(kwargs["headers"]) == {"Accept", "Accept-Encoding", "Authorization", "X-Request-ID"}


@pytest.mark.parametrize("status", [300, 301, 302, 399])
async def test_v2_get_rejects_every_redirect(client, status):
    transport, session = client
    session.request.return_value = Response({"schema_version": 1}, status=status)
    with pytest.raises(HostedParseError) as error:
        await transport.async_v2_get("/account", token=TOKEN)
    assert (error.value.code, error.value.status) == ("provider_invalid_response", status)


@pytest.mark.parametrize("raw", [
    b'{"duplicated":1,"duplicated":2}', b'{"n":NaN}', b'{"n":Infinity}',
    b'{"x":"\\ud800"}', b'{"x":[1,2,3]}',
])
async def test_v2_get_strict_json_decoding(client, raw):
    transport, session = client
    session.request.return_value = Response(raw=raw)
    if raw == b'{"x":[1,2,3]}':
        assert await transport.async_v2_get("/account", token=TOKEN) == {"x": [1, 2, 3]}
    else:
        with pytest.raises(HostedParseError) as error:
            await transport.async_v2_get("/account", token=TOKEN)
        assert error.value.code == "provider_invalid_response"


async def test_v2_get_response_size_and_chunk_boundary(client, monkeypatch):
    transport, session = client
    response = Response(raw=b'{"v":7}')
    sizes = []
    async def chunks(size):
        sizes.append(size)
        yield b'{"v":7}'
    response.content.iter_chunked = chunks
    session.request.return_value = response
    monkeypatch.setattr(module, "MAX_PARSE_RESPONSE_BYTES", 7)
    assert await transport.async_v2_get("/account", token=TOKEN) == {"v": 7}
    assert sizes == [16384]
    monkeypatch.setattr(module, "MAX_PARSE_RESPONSE_BYTES", 6)
    with pytest.raises(HostedParseError) as raised:
        await transport.async_v2_get("/account", token=TOKEN)
    assert (raised.value.code, raised.value.status) == ("provider_invalid_response", 200)


@pytest.mark.parametrize("status", [401, 429, 500])
async def test_v2_get_error_and_failure_status_preserved(client, status):
    transport, session = client
    error = {"schema_version": 1,
             "error": {"code": "provider_rate_limited" if status == 429 else "upstream_error",
                       "message": "private provider data", "retryable": status == 429}}
    response = Response(error, status=status,
                        headers={"Retry-After": "7"} if status == 429 else {})
    session.request.return_value = response
    with pytest.raises(HostedParseError) as raised:
        await transport.async_v2_get("/account", token=TOKEN)
    assert raised.value.status == status
    assert raised.value.code == error["error"]["code"]
    if status == 429:
        assert raised.value.retry_after == 7
    assert "private provider data" not in str(raised.value)


async def test_v2_get_recovery_failure_after_headers_preserves_status(client):
    transport, session = client
    response = Response({}, status=503)
    async def fail(_size):
        raise aiohttp.ClientConnectionError("secret")
        yield b"never"
    response.content.iter_chunked = fail
    session.request.return_value = response
    with pytest.raises(HostedParseError) as raised:
        await transport.async_v2_get("/account", token=TOKEN)
    assert raised.value.code == "operation_recovery_required"
    assert raised.value.uncertain is True
    assert raised.value.status == 503
    response.content.iter_chunked = fail


async def test_v2_get_invalid_json_after_headers_keeps_status(client):
    transport, session = client
    session.request.return_value = Response(raw=b"{", status=200)
    with pytest.raises(HostedParseError) as raised:
        await transport.async_v2_get("/account", token=TOKEN)
    assert (raised.value.code, raised.value.status) == ("provider_invalid_response", 200)
