"""Parser wire limits and one-shot semantics with fake and real HTTP transports."""

import asyncio
from decimal import Decimal
import json
from unittest.mock import MagicMock

import aiohttp
from aiohttp import web
import pytest

from custom_components.daylight_calendar_import import hosted_parse_transport as transport_module
from custom_components.daylight_calendar_import.hosted_parse_transport import (
    HostedParseError, HostedParseTransport, _number,
)
from custom_components.daylight_calendar_import.hosted_transport import HostedServiceBinding

SERVICE = HostedServiceBinding("https://api.daylight.test", "https://app.daylight.test")
TOKEN = "T"*43


class Content:
    def __init__(self, raw):
        self.raw = raw
    async def iter_chunked(self, size):
        for start in range(0, len(self.raw), size):
            yield self.raw[start:start + size]


class Response:
    def __init__(self, *, value=None, raw=None, status=200, headers=None, content_type="application/json"):
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type
        self.content = Content(json.dumps(value).encode() if raw is None else raw)
        self.closed = False
    async def __aenter__(self):
        return self
    async def __aexit__(self, *_args):
        self.closed = True


@pytest.fixture
def transport():
    session = MagicMock(spec=aiohttp.ClientSession)
    return HostedParseTransport(session, SERVICE), session


def api_error(code="provider_rate_limited", retryable=True):
    return {"schema_version": 1, "error": {"code": code, "message": "private provider response", "retryable": retryable}}


async def test_form_data_and_headers(transport):
    client, session = transport
    response = Response(value={"okay": 1})
    session.request.return_value = response
    assert await client.async_parse({"source": "Unicode ✓"}, (("one", "image/png", b"PNG"),), token=TOKEN) == {"okay": 1}
    args, kwargs = session.request.call_args
    assert args == ("POST", SERVICE.api_origin + "/v1/parse")
    assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
    assert kwargs["headers"]["Accept-Encoding"] == "identity"
    assert kwargs["allow_redirects"] is False and kwargs["auto_decompress"] is False
    assert kwargs["timeout"].total == 120
    assert len(kwargs["data"]._fields) == 2
    assert response.closed


@pytest.mark.parametrize("raw", [b'[]', b'{', b'\xff', b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":"\\ud800"}', b'['*1200])
async def test_strict_response_decoding(transport, raw):
    client, session = transport
    response = Response(raw=raw)
    session.request.return_value = response
    with pytest.raises(HostedParseError) as error:
        await client.async_parse({}, (), token=TOKEN)
    assert error.value.code == "provider_invalid_response"
    assert response.closed


@pytest.mark.parametrize("status,headers,ctype", [(302, {}, "application/json"), (200, {"Content-Encoding": "gzip"}, "application/json"), (200, {}, "text/plain")])
async def test_unsafe_response_envelopes(transport, status, headers, ctype):
    client, session = transport
    session.request.return_value = Response(value={}, status=status, headers=headers, content_type=ctype)
    with pytest.raises(HostedParseError) as error:
        await client.async_parse({}, (), token=TOKEN)
    assert error.value.code == "provider_invalid_response"
    assert error.value.status == status


async def test_response_limit_is_separate_from_lifecycle(transport, monkeypatch):
    client, session = transport
    response = Response(raw=b'{"x":"' + b'x'*17000 + b'"}')
    session.request.return_value = response
    assert len((await client.async_parse({}, (), token=TOKEN))["x"]) == 17000
    monkeypatch.setattr(transport_module, "MAX_PARSE_RESPONSE_BYTES", 10)
    with pytest.raises(HostedParseError):
        await client.async_parse({}, (), token=TOKEN)
    assert response.closed


@pytest.mark.parametrize("payload", [{"x": float("nan")}, {"x": "\ud800"}, {"x": object()}])
async def test_invalid_request_json_is_not_sent(transport, payload):
    client, session = transport
    with pytest.raises(HostedParseError) as error:
        await client.async_parse(payload, (), token=TOKEN)
    assert error.value.code == "invalid_request"
    session.request.assert_not_called()


async def test_request_limit_no_network(transport, monkeypatch):
    client, session = transport
    monkeypatch.setattr(transport_module, "MAX_PARSE_REQUEST_JSON_BYTES", 10)
    with pytest.raises(HostedParseError) as error:
        await client.async_parse({"x": "abcdefghijk"}, (), token=TOKEN)
    assert error.value.code == "source_too_large"
    session.request.assert_not_called()


@pytest.mark.parametrize("error", [aiohttp.ClientConnectionError("private error"), TimeoutError("secret")])
async def test_ambiguous_transport_failures_never_replayed(transport, error):
    client, session = transport
    session.request.side_effect = error
    with pytest.raises(HostedParseError) as raised:
        await client.async_parse({}, (), token=TOKEN)
    assert raised.value.retryable and raised.value.uncertain
    assert raised.value.code == ("provider_timeout" if isinstance(error, TimeoutError) else "provider_unavailable")
    assert session.request.call_count == 1
    assert "private" not in str(raised.value) and "secret" not in str(raised.value)


async def test_cancellation_propagates_and_closes_response(transport):
    client, session = transport
    response = Response(value={})
    async def blocked(_size):
        raise asyncio.CancelledError
        yield b"unreachable"
    response.content.iter_chunked = blocked
    session.request.return_value = response
    with pytest.raises(asyncio.CancelledError):
        await client.async_parse({}, (), token=TOKEN)
    assert response.closed and session.request.call_count == 1


@pytest.mark.parametrize("status,code,retryable,header", [(401,"unauthenticated",False,None), (403,"forbidden",False,None), (429,"provider_rate_limited",True,None), (429,"provider_rate_limited",True,"12"), (503,"new_code",True,None), (500,"configuration_error",False,None)])
async def test_public_errors_sanitized_preserving_flags(transport, status, code, retryable, header):
    client, session = transport
    headers = {} if header is None else {"Retry-After": header}
    response = Response(value=api_error(code,retryable), status=status, headers=headers)
    session.request.return_value = response
    with pytest.raises(HostedParseError) as error:
        await client.async_parse({}, (), token=TOKEN)
    assert (error.value.code, error.value.status, error.value.retryable) == (code,status,retryable)
    assert error.value.retry_after == (int(header) if header else None)
    assert "private" not in str(error.value)
    assert response.closed


@pytest.mark.parametrize("retry", ["0", "301", "1234", "tomorrow", "-1", "1.5"])
async def test_malformed_retry_after(transport, retry):
    client, session = transport
    session.request.return_value = Response(value=api_error(), status=429, headers={"Retry-After":retry})
    with pytest.raises(HostedParseError) as error:
        await client.async_parse({}, (), token=TOKEN)
    assert error.value.code == "provider_invalid_response"


async def test_malformed_error_envelope(transport):
    client, session = transport
    session.request.return_value = Response(value={"private":"body"}, status=503)
    with pytest.raises(HostedParseError) as error:
        await client.async_parse({}, (), token=TOKEN)
    assert error.value.code == "provider_invalid_response"


@pytest.mark.parametrize("raw,expected", [("1.0",1), ("0.1",Decimal("0.1")), ("100000000000000000000.5",Decimal("100000000000000000000.5"))])
def test_decimal_numbers(raw, expected):
    assert _number(raw) == expected


@pytest.mark.parametrize("raw", ["NaN", "not a number"])
def test_bad_decimal(raw):
    with pytest.raises(ValueError):
        _number(raw)


async def test_real_multipart_and_single_post(socket_enabled, aiohttp_server):
    received = []
    async def parse(request):
        parts = []
        async for part in await request.multipart():
            parts.append((part.name, part.headers.get("Content-Type"), await part.read()))
        received.append((dict(request.headers), parts))
        return web.json_response({"events": []})
    app = web.Application()
    app.router.add_post("/v1/parse", parse)
    test_client = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        client = HostedParseTransport(session, SERVICE)
        # Only the test rewrites the immutable reviewed origin to loopback HTTP.
        client._service = type("Binding", (), {"api_origin": str(test_client.make_url("")).rstrip("/")})()
        assert await client.async_parse({"schema_version":1}, (("a", "image/png", b"\x89PNGbytes"),), token=TOKEN) == {"events":[]}
    assert len(received) == 1
    headers, parts = received[0]
    assert headers["Authorization"] == "Bearer " + TOKEN
    assert parts == [("request", "application/json", b'{"schema_version":1}'), ("attachment.a", "image/png", b"\x89PNGbytes")]


async def test_canonical_unknown_invalid_response_code_is_generic(transport):
    client, session = transport
    session.request.return_value = Response(value=api_error("invalid_response"), status=500)
    with pytest.raises(HostedParseError) as error:
        await client.async_parse({}, (), token=TOKEN)
    assert error.value.code == "provider_invalid_response"


def test_transport_requires_a_validated_service_binding():
    with pytest.raises(ValueError, match="invalid trusted service"):
        HostedParseTransport(MagicMock(), {"api_origin": "http://unsafe.test"})


async def test_real_redirect_does_not_forward_credentials(socket_enabled, aiohttp_server):
    reached = []
    async def redirect(request):
        raise web.HTTPTemporaryRedirect(location="/target")
    async def target(request):
        reached.append(dict(request.headers))
        return web.json_response({})
    app = web.Application()
    app.router.add_post("/v1/parse", redirect)
    app.router.add_post("/target", target)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        client = HostedParseTransport(session, SERVICE)
        client._service = type("Binding", (), {"api_origin": str(server.make_url("")).rstrip("/")})()
        with pytest.raises(HostedParseError) as error:
            await client.async_parse({}, (), token=TOKEN)
        assert error.value.code == "provider_invalid_response"
    assert reached == []


async def test_real_lost_response_never_replays_post(socket_enabled, aiohttp_server):
    calls = []
    async def lost(request):
        calls.append(await request.read())
        request.transport.close()
        return web.Response()
    app = web.Application()
    app.router.add_post("/v1/parse", lost)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        client = HostedParseTransport(session, SERVICE)
        client._service = type("Binding", (), {"api_origin": str(server.make_url("")).rstrip("/")})()
        with pytest.raises(HostedParseError) as error:
            await client.async_parse({}, (), token=TOKEN)
        assert error.value.uncertain
    assert len(calls) == 1


async def test_whole_request_timeout_includes_body_read(transport, monkeypatch):
    client, session = transport
    response = Response(value={})
    closed = asyncio.Event()
    async def stalled(_size):
        try:
            await asyncio.Event().wait()
            yield b"unreachable"
        finally:
            closed.set()
    response.content.iter_chunked = stalled
    session.request.return_value = response
    original = asyncio.timeout
    monkeypatch.setattr(transport_module.asyncio, "timeout", lambda _: original(0.01))
    with pytest.raises(HostedParseError) as error:
        await client.async_parse({}, (), token=TOKEN)
    assert error.value.code == "provider_timeout" and error.value.uncertain
    assert response.closed and closed.is_set()
    assert session.request.call_count == 1
