"""P02 transport tests: no remote state/credential activation is implied here."""

import asyncio
import json
from copy import deepcopy
from pathlib import Path
from unittest.mock import MagicMock

import aiohttp
import pytest
import voluptuous as vol

from custom_components.daylight_calendar_import.hosted_transport import (
    HostedLifecycleTransport,
    HostedTransportError,
    _origin,
    _strict_strings,
    _timestamp,
    _trusted_url,
    new_pairing_verifier,
    pairing_challenge,
)

FIXTURES = Path(__file__).parent / "fixtures/hosted/v1/valid"
PAIRING = "P" * 43
VERIFIER = "V" * 43
TOKEN = "T" * 43


def fixture(name):
    return json.loads((FIXTURES / f"{name}.json").read_text())


class Content:
    def __init__(self, raw):
        self.raw = raw

    async def iter_chunked(self, size):
        for start in range(0, len(self.raw), size):
            yield self.raw[start : start + size]


class Response:
    def __init__(
        self,
        value=None,
        *,
        raw=None,
        status=200,
        headers=None,
        content_type="application/json",
    ):
        self.status = status
        self.headers = {"Cache-Control": "private, no-store", **(headers or {})}
        self.content_type = content_type
        self.content = Content(raw if raw is not None else json.dumps(value).encode())
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True


@pytest.fixture
def transport():
    session = MagicMock(spec=aiohttp.ClientSession)
    client = HostedLifecycleTransport(
        session,
        api_origin="https://api.daylight.test",
        account_origin="https://app.daylight.test",
    )
    return client, session


def test_verifier_challenge():
    values = {new_pairing_verifier() for _ in range(20)}
    assert len(values) == 20
    assert all(len(value) == len(pairing_challenge(value)) == 43 for value in values)
    assert pairing_challenge("A" * 43) == "DwBzhbb51LfusnSGBa_hqYSgo7-j8BTQnip4TOnlzRo"
    with pytest.raises(vol.Invalid, match="invalid value"):
        pairing_challenge("secret\n")


@pytest.mark.parametrize(
    "value",
    [
        "http://example.test",
        "https://x/a",
        "https://u:p@x",
        "https://x?",
        "https://x#",
        "https://x:0",
        "https://x:bad",
        "https://x\\evil",
        "https://x\n",
        "https:///",
        None,
        "https://[bad",
        "https://é.test",
    ],
)
def test_bad_origins(value):
    with pytest.raises(ValueError, match="invalid trusted origin"):
        _origin(value)


def test_origin_canonicalization():
    assert _origin("https://APP.daylight.test/") == "https://app.daylight.test"
    assert _origin("https://app.daylight.test:8443") == "https://app.daylight.test:8443"


@pytest.mark.parametrize(
    "value",
    [
        None,
        "x" * 2049,
        "https://evil.test",
        "https://u:p@app.daylight.test",
        "https://app.daylight.test:bad",
        "https://app.daylight.test/?secret=x",
        "https://app.daylight.test/#",
        "https://app.daylight.test/\\x",
        "https://app.daylight.test/\n",
        "https://app.daylight.test/é",
    ],
)
def test_untrusted_account_urls(value):
    with pytest.raises(vol.Invalid):
        _trusted_url(value, "https://app.daylight.test")


def test_timestamp_and_nested_strings():
    assert _timestamp("2026-10-21T21:40:01.123+01:00")
    with pytest.raises(vol.Invalid):
        _timestamp("2026-99-21T21:40:01Z")
    with pytest.raises(vol.Invalid):
        _timestamp("2026-10-21T21:40:01")
    _strict_strings([{"x": [None, True, 3, "okay"]}])
    with pytest.raises(UnicodeError):
        _strict_strings({"x": ["\ud800"]})


def account_url(value):
    result = deepcopy(value)
    for key in ("verification_uri", "account_url"):
        if key in result:
            result[key] = "https://app.daylight.test/connect"
    return result


@pytest.mark.parametrize(
    "method,name,schema,status,args,path",
    [
        (
            "start_pairing",
            "pairing-start-response",
            "pairing-start",
            201,
            (VERIFIER, "Home"),
            "/v1/pairings",
        ),
        (
            "pairing_status",
            "pairing-status-response",
            "pairing-status",
            200,
            (PAIRING, VERIFIER),
            f"/v1/pairings/{PAIRING}",
        ),
        (
            "exchange",
            "credential-issued-response",
            "issued",
            200,
            (PAIRING, VERIFIER),
            f"/v1/pairings/{PAIRING}/exchange",
        ),
        (
            "installation_status",
            "account-installation-status",
            "installation",
            200,
            (TOKEN,),
            "/v1/installation",
        ),
        (
            "confirm",
            "credential-confirmed-response",
            "confirmed",
            200,
            (TOKEN,),
            "/v1/installation/confirm",
        ),
        (
            "rotate",
            "credential-issued-response",
            "issued",
            200,
            (TOKEN,),
            "/v1/installation/credentials",
        ),
        (
            "disconnect",
            "account-installation-disconnect",
            "disconnected",
            200,
            (TOKEN,),
            "/v1/installation/disconnect",
        ),
    ],
)
async def test_fixed_routes(transport, method, name, schema, status, args, path):
    client, session = transport
    value = account_url(fixture(name))
    if schema == "pairing-status":
        value["pairing_id"] = PAIRING
    response = Response(value, status=status)
    session.request.return_value = response
    assert await getattr(client, method)(*args) == value
    session.request.assert_called_once()
    positional, options = session.request.call_args
    assert positional == (
        "GET" if method in ("installation_status", "pairing_status") else "POST",
        "https://api.daylight.test" + path,
    )
    assert options["allow_redirects"] is False
    assert options["timeout"].total == 15
    assert options["headers"]["Accept"] == "application/json"
    if method == "start_pairing":
        assert options["json"]["code_challenge"] == pairing_challenge(VERIFIER)
        assert VERIFIER not in json.dumps(options["json"])
    elif method in ("pairing_status", "exchange"):
        assert options["headers"]["X-Daylight-Pairing-Verifier"] == VERIFIER
    else:
        assert options["headers"]["Authorization"] == "Bearer " + TOKEN
    assert response.closed


@pytest.mark.parametrize(
    "method,args",
    [
        ("start_pairing", (VERIFIER, "")),
        ("start_pairing", (VERIFIER, 1)),
        ("start_pairing", (VERIFIER, "a" * 129)),
        ("start_pairing", (VERIFIER, "\ud800")),
        ("pairing_status", ("../x", VERIFIER)),
        ("exchange", (PAIRING, "bad")),
        ("rotate", ("x\nY: leaked",)),
    ],
)
async def test_reject_invalid_local_values_before_io(transport, method, args):
    client, session = transport
    with pytest.raises((ValueError, vol.Invalid)):
        await getattr(client, method)(*args)
    session.request.assert_not_called()


async def test_pairing_identity_mismatch(transport):
    client, session = transport
    session.request.return_value = Response(fixture("pairing-status-response"))
    with pytest.raises(HostedTransportError, match="invalid_response"):
        await client.pairing_status(PAIRING, VERIFIER)


@pytest.mark.parametrize(
    "schema,name",
    [
        ("pairing-start", "pairing-start-response"),
        ("pairing-status", "pairing-status-response"),
        ("issued", "credential-issued-response"),
        ("confirmed", "credential-confirmed-response"),
        ("disconnected", "account-installation-disconnect"),
        ("installation", "account-installation-status"),
    ],
)
def test_closed_response_fields(transport, schema, name):
    client, _ = transport
    value = account_url(fixture(name))
    assert client._validate(value, schema) == value
    for field in value:
        altered = deepcopy(value)
        del altered[field]
        with pytest.raises(vol.Invalid):
            client._validate(altered, schema)
    value["unexpected"] = "no"
    with pytest.raises(vol.Invalid):
        client._validate(value, schema)


@pytest.mark.parametrize(
    "field,value",
    [
        ("schema_version", True),
        ("schema_version", 2),
        ("installation_id", "x"),
        ("installation_token", "secret\n"),
        ("expires_at", "tomorrow"),
        ("confirmation_expires_at", "2026-99-21T21:40:01Z"),
    ],
)
def test_invalid_credential_responses(transport, field, value):
    data = fixture("credential-issued-response")
    data[field] = value
    with pytest.raises(vol.Invalid):
        transport[0]._validate(data, "issued")


@pytest.mark.parametrize(
    "response",
    [
        Response({}, status=302),
        Response({}, headers={"Cache-Control": "public"}),
        Response({}, content_type="text/html"),
        Response(raw=b"x" * 16385),
        Response(raw=b"bad"),
        Response(raw=b'{"x":1,"x":2}'),
        Response(raw=b'{"x":NaN}'),
        Response(raw=b'{"x":"\\ud800"}'),
        Response(raw=b"\xff"),
        Response(raw=b"[" * 2000 + b"]" * 2000),
        Response([]),
    ],
)
async def test_invalid_responses_are_sanitized_and_closed(transport, response):
    client, session = transport
    session.request.return_value = response
    with pytest.raises(HostedTransportError, match="^invalid_response$"):
        await client.installation_status(TOKEN)
    assert response.closed
    assert session.request.call_count == 1


@pytest.mark.parametrize(
    "failure", [aiohttp.ClientError("SECRET_URL"), TimeoutError("TOKEN")]
)
async def test_network_failures_never_retry_or_leak(transport, failure):
    client, session = transport
    session.request.side_effect = failure
    with pytest.raises(HostedTransportError) as error:
        await client.exchange(PAIRING, VERIFIER)
    assert str(error.value) == "service_unavailable"
    assert error.value.retryable
    assert error.value.__suppress_context__
    assert session.request.call_count == 1


async def test_cancellation_propagates(transport):
    client, session = transport
    session.request.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        await client.rotate(TOKEN)
    session.request.assert_called_once()


@pytest.mark.parametrize(
    "status,code,retryable,header,expected",
    [
        (401, "unauthenticated", False, None, None),
        (403, "forbidden", False, None, None),
        (409, "rotation_pending", True, None, None),
        (503, "future_code", True, None, None),
        (429, "rate_limited", True, "1", 1),
        (429, "rate_limited", True, "300", 300),
    ],
)
async def test_api_errors_keep_only_safe_code_and_retryability(
    transport, status, code, retryable, header, expected
):
    client, session = transport
    response = Response(
        {
            "schema_version": 1,
            "request_id": None,
            "error": {"code": code, "message": "SECRET_TOKEN", "retryable": retryable},
        },
        status=status,
        headers={"Retry-After": header} if header is not None else {},
    )
    session.request.return_value = response
    with pytest.raises(HostedTransportError) as error:
        await client.disconnect(TOKEN)
    assert error.value.code == code
    assert error.value.status == status
    assert error.value.retryable is retryable
    assert error.value.retry_after == expected
    assert "SECRET" not in repr(error.value)
    assert response.closed
    session.request.assert_called_once()


@pytest.mark.parametrize(
    "header",
    [None, "", "0", "301", "9999", "-1", "1.5", "Wed, 21 Oct 2026 00:00:00 GMT"],
)
async def test_invalid_rate_limit_deadlines(transport, header):
    client, session = transport
    session.request.return_value = Response(
        {
            "schema_version": 1,
            "error": {"code": "rate_limited", "message": "bounded", "retryable": True},
        },
        status=429,
        headers={"Retry-After": header} if header is not None else {},
    )
    with pytest.raises(HostedTransportError, match="^invalid_response$"):
        await client.installation_status(TOKEN)


async def test_malformed_error(transport):
    client, session = transport
    session.request.return_value = Response(
        {"schema_version": 1, "error": {"code": TOKEN}}, status=500
    )
    with pytest.raises(HostedTransportError, match="^invalid_response$"):
        await client.installation_status(TOKEN)


@pytest.mark.enable_socket
async def test_real_aiohttp_post_is_not_replayed(aiohttp_server):
    """A connection lost after receiving a mutation must never replay that POST."""
    from aiohttp import web

    calls = []

    async def exchange(request):
        calls.append(await request.json())
        request.transport.close()
        return web.Response()

    app = web.Application()
    app.router.add_post(f"/v1/pairings/{PAIRING}/exchange", exchange)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        client = HostedLifecycleTransport(
            session,
            api_origin="https://api.daylight.test",
            account_origin="https://app.daylight.test",
        )
        # Loopback HTTP is test-only; production construction always requires HTTPS.
        client._api_origin = str(server.make_url(""))
        with pytest.raises(HostedTransportError, match="service_unavailable"):
            await client.exchange(PAIRING, VERIFIER)
    assert calls == [{"schema_version": 1}]


@pytest.mark.enable_socket
async def test_real_aiohttp_redirect_never_forwards_authority(aiohttp_server):
    from aiohttp import web

    reached = []

    async def target(request):
        reached.append(dict(request.headers))
        return web.json_response({})

    async def redirect(request):
        raise web.HTTPTemporaryRedirect(location="/target")

    app = web.Application()
    app.router.add_get("/v1/installation", redirect)
    app.router.add_get("/target", target)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        client = HostedLifecycleTransport(
            session,
            api_origin="https://api.daylight.test",
            account_origin="https://app.daylight.test",
        )
        client._api_origin = str(server.make_url(""))
        with pytest.raises(HostedTransportError, match="invalid_response"):
            await client.installation_status(TOKEN)
    assert reached == []


async def test_whole_request_deadline_includes_stalled_body(transport, monkeypatch):
    import custom_components.daylight_calendar_import.hosted_transport as module

    client, session = transport
    response = Response({})
    released = asyncio.Event()

    async def stalled(_size):
        try:
            await asyncio.Event().wait()
            yield b"unreachable"
        finally:
            released.set()

    response.content.iter_chunked = stalled
    session.request.return_value = response
    original_timeout = asyncio.timeout
    monkeypatch.setattr(
        module.asyncio, "timeout", lambda _seconds: original_timeout(0.01)
    )
    with pytest.raises(HostedTransportError, match="service_unavailable"):
        await client.rotate(TOKEN)
    assert released.is_set()
    assert response.closed
    session.request.assert_called_once()


@pytest.mark.parametrize("length", [16, 128])
async def test_public_pairing_id_boundaries(transport, length):
    client, session = transport
    identity = "I" * length
    value = fixture("pairing-status-response")
    value["pairing_id"] = identity
    session.request.return_value = Response(value)
    assert await client.pairing_status(identity, VERIFIER) == value


@pytest.mark.parametrize("identity", ["I" * 15, "I" * 129, "I" * 16 + "\n", 1])
async def test_public_pairing_id_invalid_boundaries(transport, identity):
    client, session = transport
    with pytest.raises(vol.Invalid):
        await client.pairing_status(identity, VERIFIER)
    session.request.assert_not_called()


@pytest.mark.parametrize("length", [42, 44])
async def test_verifier_exact_length(transport, length):
    client, session = transport
    with pytest.raises(vol.Invalid):
        await client.exchange(PAIRING, "V" * length)
    session.request.assert_not_called()


@pytest.mark.parametrize("length", [43, 256])
async def test_token_valid_boundaries(transport, length):
    client, session = transport
    value = fixture("credential-issued-response")
    value["installation_token"] = "T" * length
    session.request.return_value = Response(value)
    assert await client.rotate("T" * length) == value


@pytest.mark.parametrize("length", [42, 257])
async def test_token_invalid_boundaries(transport, length):
    client, session = transport
    with pytest.raises(vol.Invalid):
        await client.rotate("T" * length)
    session.request.assert_not_called()


async def test_complete_pairing_request_and_body_limit_boundary(transport):
    client, session = transport
    value = account_url(fixture("pairing-start-response"))
    raw = json.dumps(value).encode()
    session.request.return_value = Response(
        raw=raw + b" " * (16384 - len(raw)), status=201
    )
    assert await client.start_pairing(VERIFIER, "H" * 128) == value
    assert session.request.call_args.kwargs["json"] == {
        "schema_version": 1,
        "code_challenge": pairing_challenge(VERIFIER),
        "code_challenge_method": "S256",
        "installation_label": "H" * 128,
    }


@pytest.mark.parametrize(
    "field,value",
    [
        (
            "capabilities",
            {"managed_ai": 1, "email_intake": False, "source_delivery": False},
        ),
        ("credential_status", "staged"),
        ("account_status", "suspended"),
        ("forwarding_address", "not-an-email"),
        ("forwarding_address", "a@b\n"),
        ("account_url", "https://evil.test"),
    ],
)
def test_installation_status_rejects_invalid_authority_fields(transport, field, value):
    client, _ = transport
    data = account_url(fixture("account-installation-status"))
    data[field] = value
    with pytest.raises(vol.Invalid):
        client._validate(data, "installation")


def test_percent_encoded_account_uri_is_valid():
    value = "https://app.daylight.test/%C3%A9"
    assert _trusted_url(value, "https://app.daylight.test") == value
