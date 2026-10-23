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
ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "schemas").is_dir():
    ROOT = ROOT.parent
INSTALLATION_SCHEMA = (
    ROOT / "schemas/hosted/v1/installation-status-response.schema.json"
)
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
    assert (
        options["json"] is None
        if positional[0] == "GET"
        else options["json"]["schema_version"] == 1
    )
    assert options["headers"]["Accept"] == "application/json"
    assert options["headers"]["Accept-Encoding"] == "identity"
    assert options["auto_decompress"] is False
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
    with pytest.raises(HostedTransportError) as error:
        await client.pairing_status(PAIRING, VERIFIER)
    assert error.value.code == "invalid_response"


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
    with pytest.raises(HostedTransportError) as error:
        await client.installation_status(TOKEN)
    assert error.value.code == "invalid_response"
    assert error.value.status == 429
    assert error.value.retryable is False
    assert error.value.retry_after is None


async def test_malformed_error(transport):
    client, session = transport
    session.request.return_value = Response(
        {"schema_version": 1, "error": {"code": TOKEN}}, status=500
    )
    with pytest.raises(HostedTransportError, match="^invalid_response$"):
        await client.installation_status(TOKEN)


async def test_real_aiohttp_post_is_not_replayed(socket_enabled, aiohttp_server):
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


async def test_real_aiohttp_redirect_never_forwards_authority(
    socket_enabled, aiohttp_server
):
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

    def short_timeout(seconds):
        assert seconds == 15
        return original_timeout(0.01)

    monkeypatch.setattr(module.asyncio, "timeout", short_timeout)
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
        ("forwarding_address", ["a@b"]),
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


@pytest.mark.parametrize("status", ["pending", "approved", "exchanged"])
async def test_all_public_pairing_states(transport, status):
    client, session = transport
    data = fixture("pairing-status-response")
    data.update(pairing_id=PAIRING, status=status)
    session.request.return_value = Response(data)
    assert (await client.pairing_status(PAIRING, VERIFIER))["status"] == status


@pytest.mark.parametrize(
    "interval,valid", [(5, True), (30, True), (4, False), (31, False)]
)
def test_public_poll_interval_boundaries(transport, interval, valid):
    client, _ = transport
    data = account_url(fixture("pairing-start-response"))
    data["poll_interval_seconds"] = interval
    if valid:
        assert client._validate(data, "pairing-start") == data
    else:
        with pytest.raises(vol.Invalid):
            client._validate(data, "pairing-start")


@pytest.mark.parametrize("length", [1, 128])
async def test_public_installation_label_boundaries(transport, length):
    client, session = transport
    session.request.return_value = Response(
        account_url(fixture("pairing-start-response")), status=201
    )
    await client.start_pairing(VERIFIER, "H" * length)
    assert (
        session.request.call_args.kwargs["json"]["installation_label"] == "H" * length
    )


@pytest.mark.parametrize("length", [2048, 2049])
def test_public_account_uri_size_boundary(length):
    prefix = "https://app.daylight.test/"
    value = prefix + "x" * (length - len(prefix))
    if length == 2048:
        assert _trusted_url(value, "https://app.daylight.test") == value
    else:
        with pytest.raises(vol.Invalid):
            _trusted_url(value, "https://app.daylight.test")


@pytest.mark.parametrize(
    "address,valid",
    [("a@b", True), ("a@" + "b" * 252, True), ("a@" + "b" * 253, False)],
)
def test_public_forwarding_address_and_overlap(transport, address, valid):
    client, _ = transport
    data = account_url(fixture("account-installation-status"))
    data.update(forwarding_address=address, credential_status="overlap")
    if valid:
        assert client._validate(data, "installation") == data
    else:
        with pytest.raises(vol.Invalid):
            client._validate(data, "installation")


@pytest.mark.parametrize(
    "violation", ["duplicate", "surrogate_message", "surrogate_request_id"]
)
async def test_strict_json_in_otherwise_valid_envelopes(transport, violation):
    client, session = transport
    if violation == "duplicate":
        data = account_url(fixture("account-installation-status"))
        raw = (
            json.dumps(data)
            .replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1')
            .encode()
        )
        response = Response(raw=raw)
    else:
        data = {
            "schema_version": 1,
            "request_id": "safe",
            "error": {"code": "unauthenticated", "message": "safe", "retryable": False},
        }
        if violation == "surrogate_message":
            data["error"]["message"] = "\ud800"
        else:
            data["request_id"] = "\ud800"
        response = Response(data, status=401)
    session.request.return_value = response
    with pytest.raises(HostedTransportError) as error:
        await client.installation_status(TOKEN)
    assert error.value.code == "invalid_response"
    assert error.value.retryable is False


@pytest.mark.parametrize(
    "violation",
    [
        "content_type",
        "cache_control",
        "missing_cache_control",
        "oversized",
        "redirect300",
        "redirect399",
    ],
)
async def test_transport_rejects_valid_payload_with_invalid_envelope(
    transport, violation
):
    client, session = transport
    value = account_url(fixture("account-installation-status"))
    response = Response(value)
    if violation == "content_type":
        response.content_type = "text/html"
    elif violation == "cache_control":
        response.headers["Cache-Control"] = "public"
    elif violation == "missing_cache_control":
        del response.headers["Cache-Control"]
    elif violation == "oversized":
        response.content = Content(json.dumps(value).encode() + b" " * 16385)
    else:
        response.status = int(violation[-3:])
    session.request.return_value = response
    with pytest.raises(HostedTransportError) as error:
        await client.installation_status(TOKEN)
    assert error.value.code == "invalid_response"
    assert error.value.status == response.status
    assert error.value.retryable is False
    assert response.closed


@pytest.mark.parametrize("request_id", ["x", "x" * 128, None])
@pytest.mark.parametrize("message", ["x", "x" * 2048])
async def test_public_error_string_boundaries(transport, request_id, message):
    client, session = transport
    session.request.return_value = Response(
        {
            "schema_version": 1,
            "request_id": request_id,
            "error": {
                "code": "invalid_request",
                "message": message,
                "retryable": False,
            },
        },
        status=400,
    )
    with pytest.raises(HostedTransportError) as error:
        await client.installation_status(TOKEN)
    assert error.value.code == "invalid_request"
    assert error.value.status == 400
    assert error.value.retryable is False
    assert error.value.retry_after is None


@pytest.mark.parametrize(
    "field,value",
    [
        ("request_id", ""),
        ("request_id", "x" * 129),
        ("request_id", ["x"]),
        ("schema_version", True),
        ("schema_version", 2),
        ("error.message", ""),
        ("error.message", "x" * 2049),
        ("error.message", ["x"]),
    ],
)
async def test_malformed_error_field_boundaries(transport, field, value):
    client, session = transport
    data = {
        "schema_version": 1,
        "request_id": "safe",
        "error": {"code": "invalid_request", "message": "safe", "retryable": False},
    }
    if field.startswith("error."):
        data["error"][field.split(".")[1]] = value
    else:
        data[field] = value
    session.request.return_value = Response(data, status=400)
    with pytest.raises(HostedTransportError) as error:
        await client.installation_status(TOKEN)
    assert error.value.code == "invalid_response"
    assert error.value.retryable is False


@pytest.mark.parametrize("cache_control", ["private,no-store", "PRIVATE, NO-STORE"])
async def test_no_store_directive_case_and_comma_whitespace(transport, cache_control):
    client, session = transport
    data = account_url(fixture("account-installation-status"))
    session.request.return_value = Response(
        data, headers={"Cache-Control": cache_control}
    )
    assert await client.installation_status(TOKEN) == data


def test_trusted_urls_preserve_valid_path_punctuation_and_host_case():
    origin = _origin("https://eXample.test")
    assert origin == "https://example.test"
    for path in ("X", "!"):
        value = "https://example.test/" + path
        assert _trusted_url(value, origin) == value
    with pytest.raises(ValueError):
        _origin("https://app daylight.test")
    with pytest.raises(vol.Invalid):
        _trusted_url("https://example.test/with space", origin)


@pytest.mark.parametrize("field", ["reason", "header"])
@pytest.mark.parametrize("length", [4096, 4097, 8191])
async def test_real_http_response_header_budgets(
    socket_enabled, aiohttp_server, field, length
):
    """Bound metadata independently of the JSON body, including exact limits."""
    from aiohttp import web

    value = account_url(fixture("account-installation-status"))
    calls = []

    async def installation(request):
        calls.append(request.path)
        headers = {"Cache-Control": "no-store"}
        options = {}
        if field == "reason":
            options["reason"] = "R" * length
        else:
            headers["X-Untrusted-Metadata"] = "R" * length
        return web.json_response(value, headers=headers, **options)

    app = web.Application()
    app.router.add_get("/v1/installation", installation)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        client = HostedLifecycleTransport(
            session,
            api_origin="https://api.daylight.test",
            account_origin="https://app.daylight.test",
        )
        client._api_origin = str(server.make_url(""))
        if length == 4096:
            assert await client.installation_status(TOKEN) == value
        else:
            with pytest.raises(HostedTransportError) as error:
                await client.installation_status(TOKEN)
            assert error.value.code == "service_unavailable"
            assert error.value.retryable is True
    assert calls == ["/v1/installation"]


def test_verifier_entropy_does_not_follow_provider_default(monkeypatch):
    """P01 fixes 32 random bytes even if the stdlib's default entropy changes."""
    import secrets

    monkeypatch.setattr(secrets, "DEFAULT_ENTROPY", 16)
    assert len(new_pairing_verifier()) == 43


async def test_valid_uri_registered_name_subdelimiter():
    # RFC3986 reg-name permits sub-delimiters. Trust validation does not resolve DNS.
    session = MagicMock(spec=aiohttp.ClientSession)
    client = HostedLifecycleTransport(
        session,
        api_origin="https://service!name.example",
        account_origin="https://app.daylight.test",
    )
    data = account_url(fixture("account-installation-status"))
    session.request.return_value = Response(data)
    assert await client.installation_status(TOKEN) == data
    assert (
        session.request.call_args.args[1]
        == "https://service!name.example/v1/installation"
    )


@pytest.mark.parametrize(
    "raw",
    [
        b'{"value":NaN}',
        b'{"value":Infinity}',
        b'{"value":-Infinity}',
        b'{"\\ud800":"safe"}',
    ],
)
async def test_malformed_json_never_reaches_response_validation(transport, raw):
    """The JSON boundary rejects constants before any schema consumer sees them."""
    client, session = transport
    received = []

    def validate(value, schema):
        received.append(value)
        return value

    client._validate = validate
    session.request.return_value = Response(raw=raw)
    with pytest.raises(HostedTransportError) as error:
        await client.installation_status(TOKEN)
    assert error.value.code == "invalid_response"
    assert received == []


@pytest.mark.parametrize(
    "configured,returned,expected",
    [
        (
            "https://app.daylight.test",
            "https://app.daylight.test:443/connect",
            "https://app.daylight.test",
        ),
        (
            "https://app.daylight.test:443",
            "https://app.daylight.test/connect",
            "https://app.daylight.test",
        ),
        (
            "https://app.daylight.test:0443",
            "https://app.daylight.test:443/connect",
            "https://app.daylight.test",
        ),
        (
            "https://[2001:DB8::1]:443",
            "https://[2001:db8::1]/connect",
            "https://[2001:db8::1]",
        ),
        (
            "https://[2001:DB8::1]",
            "https://[2001:db8::1]:443/connect",
            "https://[2001:db8::1]",
        ),
        ("https://[::1]:8443", "https://[::1]:8443/connect", "https://[::1]:8443"),
    ],
)
async def test_default_https_port_origin_equivalence(
    transport, configured, returned, expected
):
    _, session = transport
    client = HostedLifecycleTransport(
        session, api_origin=configured, account_origin=configured
    )
    value = fixture("account-installation-status")
    value["account_url"] = returned
    session.request.return_value = Response(value)
    assert await client.installation_status(TOKEN) == value
    assert session.request.call_args.args[1] == expected + "/v1/installation"


@pytest.mark.parametrize(
    "configured,returned",
    [
        ("https://app.daylight.test", "https://app.daylight.test:8443/connect"),
        ("https://app.daylight.test:8443", "https://app.daylight.test:443/connect"),
        ("https://[::1]", "https://[::1]:8443/connect"),
        ("https://[::1]:8443", "https://[::1]/connect"),
        ("https://[::1]:443", "https://[::2]:443/connect"),
    ],
)
async def test_other_origin_ports_and_hosts_remain_untrusted(
    transport, configured, returned
):
    _, session = transport
    client = HostedLifecycleTransport(
        session, api_origin="https://api.daylight.test", account_origin=configured
    )
    value = fixture("account-installation-status")
    value["account_url"] = returned
    session.request.return_value = Response(value)
    with pytest.raises(HostedTransportError) as error:
        await client.installation_status(TOKEN)
    assert error.value.code == "invalid_response"


@pytest.mark.parametrize(
    "encoding", ["gzip", "deflate", "br", "identity", "unknown", ""]
)
async def test_encoded_responses_rejected_before_body_read(transport, encoding):
    client, session = transport
    response = Response(
        account_url(fixture("account-installation-status")),
        headers={"Content-Encoding": encoding},
    )
    read = []

    async def track_read(_size):
        read.append(True)
        yield b"unused"

    response.content.iter_chunked = track_read
    session.request.return_value = response
    with pytest.raises(HostedTransportError) as error:
        await client.installation_status(TOKEN)
    assert error.value.code == "invalid_response"
    assert error.value.status == 200
    assert error.value.retryable is False
    assert read == []
    assert response.closed


@pytest.mark.parametrize("expanded_size", [100, 4 * 1024 * 1024])
async def test_real_gzip_never_decompresses(
    socket_enabled, aiohttp_server, monkeypatch, expanded_size
):
    import gzip

    from aiohttp import compression_utils, web

    compressed = gzip.compress(b"x" * expanded_size)
    calls = []

    def forbidden_decompression(*args, **kwargs):
        calls.append(True)
        raise AssertionError("untrusted response was decompressed")

    monkeypatch.setattr(
        compression_utils.ZLibDecompressor, "decompress_sync", forbidden_decompression
    )

    async def installation(request):
        assert request.headers["Accept-Encoding"] == "identity"
        return web.Response(
            body=compressed,
            headers={
                "Content-Type": "application/json",
                "Cache-Control": "no-store",
                "Content-Encoding": "gzip",
            },
        )

    app = web.Application()
    app.router.add_get("/v1/installation", installation)
    server = await aiohttp_server(app)
    async with aiohttp.ClientSession() as session:
        client = HostedLifecycleTransport(
            session,
            api_origin="https://api.daylight.test",
            account_origin="https://app.daylight.test",
        )
        client._api_origin = str(server.make_url(""))
        with pytest.raises(HostedTransportError) as error:
            await client.installation_status(TOKEN)
        assert error.value.code == "invalid_response"
    assert calls == []


@pytest.mark.parametrize("count", [32, 33, 129])
async def test_real_aggregate_response_header_budget(socket_enabled, count):
    value = account_url(fixture("account-installation-status"))
    body = json.dumps(value).encode()
    lines = [
        b"Content-Type: application/json",
        b"Cache-Control: no-store",
        b"Content-Length: " + str(len(body)).encode(),
        b"Connection: close",
    ]
    lines += [f"X-Test-{index}: value".encode() for index in range(count - len(lines))]
    wire = b"HTTP/1.1 200 OK\r\n" + b"\r\n".join(lines) + b"\r\n\r\n" + body
    completed = asyncio.Event()

    async def serve(reader, writer):
        try:
            await reader.readuntil(b"\r\n\r\n")
            writer.write(wire)
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()
            completed.set()

    server = await asyncio.start_server(serve, "127.0.0.1", 0)
    try:
        async with aiohttp.ClientSession() as session:
            client = HostedLifecycleTransport(
                session,
                api_origin="https://api.daylight.test",
                account_origin="https://app.daylight.test",
            )
            port = server.sockets[0].getsockname()[1]
            client._api_origin = f"http://127.0.0.1:{port}"
            if count == 32:
                assert await client.installation_status(TOKEN) == value
            else:
                with pytest.raises(HostedTransportError) as error:
                    await client.installation_status(TOKEN)
                assert error.value.code == "service_unavailable"
        await asyncio.wait_for(completed.wait(), timeout=1)
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    "address",
    [
        None,
        "@",
        '"a b"@example.test',
        " a@b ",
        "a@b\n",
        "名前@例え.テスト",
        "a@@b",
        "",
        "missing-at",
        1,
        True,
        [],
        "a@" + "b" * 252,
        "a@" + "b" * 253,
    ],
)
def test_forwarding_address_matches_canonical_schema(transport, address):
    from jsonschema import Draft202012Validator, FormatChecker

    client, _ = transport
    value = account_url(fixture("account-installation-status"))
    value["forwarding_address"] = address
    canonical = Draft202012Validator(
        json.loads(INSTALLATION_SCHEMA.read_text()), format_checker=FormatChecker()
    )
    if canonical.is_valid(value):
        assert client._validate(value, "installation") == value
    else:
        with pytest.raises(vol.Invalid):
            client._validate(value, "installation")


@pytest.mark.parametrize(
    "path",
    ["/%GG", "/%", "/%0", "/<x>", "/[x]", '/"x"', "/{x}", "/x|y", "/x^y", "/x`y"],
)
def test_account_link_uri_syntax_matches_public_schema(transport, path):
    from jsonschema import Draft202012Validator, FormatChecker

    client, _ = transport
    value = account_url(fixture("account-installation-status"))
    value["account_url"] = "https://app.daylight.test" + path
    canonical = Draft202012Validator(
        json.loads(INSTALLATION_SCHEMA.read_text()), format_checker=FormatChecker()
    )
    assert not canonical.is_valid(value)
    with pytest.raises(vol.Invalid):
        client._validate(value, "installation")


@pytest.mark.parametrize(
    "url",
    [
        "HTTPS://app.daylight.test/connect",
        "https://app.daylight.test:70000/connect",
        "https://[bad]/connect",
        "https://app%GG.test/connect",
        "https://app<bad>.test/connect",
        "https://[fe80::1%25eth0]/connect",
    ],
)
def test_invalid_account_uri_authority_and_scheme(transport, url):
    client, _ = transport
    value = account_url(fixture("account-installation-status"))
    value["account_url"] = url
    with pytest.raises(vol.Invalid):
        client._validate(value, "installation")


@pytest.mark.parametrize(
    "url",
    [
        "https://app.daylight.test/connect/a:b@c;d,e=1&f!$'()*+_~.---",
        "https://app.daylight.test/%2f%2F%3a%3A%40%23%25%5B%5D",
        "https://[2001:db8::1]:443/connect/_/x:y",
        "https://[v1.test]/path",
    ],
)
def test_valid_uri_punctuation_and_ip_literals(url):
    from urllib.parse import urlsplit

    parsed = urlsplit(url)
    origin = _origin(f"https://{parsed.netloc}")
    assert _trusted_url(url, origin) == url


@pytest.mark.parametrize(
    "suffix,valid",
    [
        ("+00:00", True),
        ("+00:59", True),
        ("+01:00", True),
        ("+23:59", True),
        ("-00:59", True),
        ("-23:59", True),
        ("+00:60", False),
        ("-00:60", False),
        ("+01:99", False),
        ("+24:00", False),
        ("+25:00", False),
    ],
)
def test_timestamp_offset_ranges_match_public_format(suffix, valid):
    from jsonschema import FormatChecker

    value = "2026-10-21T21:40:01" + suffix
    assert FormatChecker().conforms(value, "date-time") is valid
    if valid:
        assert _timestamp(value) == value
    else:
        with pytest.raises(vol.Invalid):
            _timestamp(value)


@pytest.mark.parametrize("number", ["1.0", "1e0", "10e-1", "1.00000000000000000000"])
async def test_exact_integral_json_version_is_accepted(transport, number):
    client, session = transport
    value = account_url(fixture("account-installation-status"))
    raw = (
        json.dumps(value)
        .replace('"schema_version": 1', '"schema_version": ' + number)
        .encode()
    )
    session.request.return_value = Response(raw=raw)
    result = await client.installation_status(TOKEN)
    assert result == value
    assert type(result["schema_version"]) is int


@pytest.mark.parametrize(
    "number,interval", [("5.0", 5), ("0.5e1", 5), ("30.0", 30), ("3e1", 30)]
)
async def test_exact_integral_json_poll_interval_is_accepted(
    transport, number, interval
):
    client, session = transport
    value = account_url(fixture("pairing-start-response"))
    previous = value["poll_interval_seconds"]
    raw = (
        json.dumps(value)
        .replace(
            '"poll_interval_seconds": ' + str(previous),
            '"poll_interval_seconds": ' + number,
        )
        .encode()
    )
    value["poll_interval_seconds"] = interval
    session.request.return_value = Response(raw=raw, status=201)
    result = await client.start_pairing(VERIFIER, "Home")
    assert result == value
    assert type(result["poll_interval_seconds"]) is int


@pytest.mark.parametrize(
    "number",
    [
        "1.00000000000000000001",
        "0.99999999999999999999",
        "true",
        "1e999999999999999999999999999",
        "1e100000000",
        "-1e100000000",
        "1e-100000000",
        "1.5",
    ],
)
async def test_fractional_boolean_and_unbounded_json_numbers_are_rejected(
    transport, number
):
    client, session = transport
    value = account_url(fixture("account-installation-status"))
    raw = (
        json.dumps(value)
        .replace('"schema_version": 1', '"schema_version": ' + number)
        .encode()
    )
    session.request.return_value = Response(raw=raw)
    with pytest.raises(HostedTransportError) as error:
        await client.installation_status(TOKEN)
    assert error.value.code == "invalid_response"
    assert error.value.retryable is False
