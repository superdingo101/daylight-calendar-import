"""Strict claim-response validation and one-shot source transport."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import MagicMock

import aiohttp
import pytest

from custom_components.daylight_calendar_import.hosted_source_transport import HostedSourceTransport, _page
from custom_components.daylight_calendar_import.hosted_transport import HostedServiceBinding, HostedTransportError
from tests.test_hosted_parse_transport import Response

SERVICE = HostedServiceBinding("https://api.daylight.test", "https://app.daylight.test")
TOKEN = "t" * 43
NOW = datetime.now(timezone.utc)


def page():
    return {"schema_version": 1, "deliveries": [{
        "delivery_id": "d" * 32, "source_id": "s" * 32, "lease_token": "l" * 32,
        "lease_expires_at": (NOW + timedelta(seconds=90)).isoformat(),
        "source_expires_at": (NOW + timedelta(days=1)).isoformat(),
        "source": {"kind": "email", "received_at": NOW.isoformat(), "text": "hello",
                   "attachments": [], "metadata": {"sender": "example"}},
    }]}


def attachment():
    return {"id": "a.png", "media_type": "image/png", "size_bytes": 12, "sha256": "a" * 64}


def test_page_accepts_valid_source_and_bounded_attachment():
    value = page()
    source = value["deliveries"][0]["source"]
    source["attachments"] = [attachment()]
    source["title"] = None
    source["attachments"][0]["filename"] = "picture.png"
    assert _page(value, 20, NOW) == value


@pytest.mark.parametrize("modify", [
    lambda p: p.update(extra=1), lambda p: p.update(schema_version=True),
    lambda p: p.update(deliveries={}), lambda p: p["deliveries"].append(deepcopy(p["deliveries"][0])),
    lambda p: p["deliveries"][0].update(lease_token="bad"),
    lambda p: p["deliveries"][0].update(source_expires_at=(NOW - timedelta(seconds=1)).isoformat()),
    lambda p: p["deliveries"][0].update(source_expires_at=(NOW + timedelta(days=8)).isoformat()),
    lambda p: p["deliveries"][0].update(lease_expires_at=(NOW + timedelta(days=2)).isoformat()),
    lambda p: p["deliveries"][0]["source"].update(kind="other"),
    lambda p: p["deliveries"][0]["source"].update(received_at="tomorrow"),
    lambda p: p["deliveries"][0]["source"].update(text=""),
    lambda p: p["deliveries"][0]["source"].update(metadata={"x": 1}),
    lambda p: p["deliveries"][0]["source"].update(attachments=[attachment()] * 2),
    lambda p: p["deliveries"][0]["source"].update(attachments=[{**attachment(), "size_bytes": True}]),
])
def test_invalid_pages_are_never_empty(modify):
    value = page()
    modify(value)
    with pytest.raises(ValueError):
        _page(value, 20, NOW)


async def test_one_claim_and_no_transparent_retries():
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = Response(value=page(), headers={"Cache-Control": "no-store"})
    client = HostedSourceTransport(session, SERVICE)
    assert client.service_binding == SERVICE
    result = await client.async_claim(token=TOKEN)
    assert len(result["deliveries"]) == 1
    args, kwargs = session.request.call_args
    assert args == ("POST", "https://api.daylight.test/v1/sources/claim")
    assert kwargs["json"] == {"schema_version": 1, "limit": 20}
    assert not kwargs["allow_redirects"] and not kwargs["auto_decompress"]
    assert session.request.call_count == 1


async def test_claim_accepts_maximum_escaped_unicode_fields():
    value = page()
    source = value["deliveries"][0]["source"]
    source["text"] = "😀" * 1_000_000
    source["title"] = "😀" * 512
    source["metadata"]["sender"] = "😀" * 512
    source["attachments"] = [
        {**attachment(), "id": f"a{i}.png", "filename": "😀" * 512}
        for i in range(4)
    ]
    raw = json.dumps(value, ensure_ascii=True, separators=(",", ":")).encode()
    assert len(raw) > 12_010_000
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = Response(raw=raw, headers={"Cache-Control": "no-store"})
    result = await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN, limit=1)
    assert result == value


@pytest.mark.parametrize("raw", [b'{"x":1,"x":2}', b'{"schema_version": NaN}', b'{', b'"\\ud800"'])
async def test_bad_json_does_not_become_empty_queue(raw):
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = Response(raw=raw, headers={"Cache-Control": "no-store"})
    with pytest.raises(HostedTransportError, match="invalid_response"):
        await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)
    assert session.request.call_count == 1


async def test_lost_response_is_never_retried():
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.side_effect = aiohttp.ClientConnectionError()
    with pytest.raises(HostedTransportError, match="service_unavailable"):
        await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)
    assert session.request.call_count == 1


async def test_unsafe_envelope_fails_closed():
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = Response(raw=json.dumps(page()).encode())
    with pytest.raises(HostedTransportError, match="invalid_response"):
        await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)


def test_invalid_attachment_shapes_and_source_rules():
    value = page()
    source = value["deliveries"][0]["source"]
    source["metadata"] = {}
    assert _page(value, 20, NOW)
    source["text"] = None
    with pytest.raises(ValueError):
        _page(value, 20, NOW)
    source["attachments"] = [{**attachment(), "size_bytes": 10_485_760},
                             {**attachment(), "id": "other", "size_bytes": 1}]
    with pytest.raises(ValueError):
        _page(value, 20, NOW)


async def test_rejects_invalid_request_without_network():
    session = MagicMock(spec=aiohttp.ClientSession)
    with pytest.raises(ValueError):
        HostedSourceTransport(session, "https://bad.example")
    client = HostedSourceTransport(session, SERVICE)
    with pytest.raises(ValueError):
        await client.async_claim(token=TOKEN, limit=0)
    session.request.assert_not_called()


async def test_limit_and_api_errors(monkeypatch):
    from custom_components.daylight_calendar_import import hosted_source_transport as module
    session = MagicMock(spec=aiohttp.ClientSession)
    client = HostedSourceTransport(session, SERVICE)
    monkeypatch.setattr(module, "MAX_CLAIM_BYTES", 3)
    session.request.return_value = Response(value=page(), headers={"Cache-Control": "no-store"})
    with pytest.raises(HostedTransportError, match="invalid_response"):
        await client.async_claim(token=TOKEN)
    monkeypatch.setattr(module, "MAX_CLAIM_BYTES", 10000)
    error = {"schema_version": 1, "error": {"code": "not_authorized", "message": "private", "retryable": False}}
    session.request.return_value = Response(value=error, status=401, headers={"Cache-Control": "no-store"})
    with pytest.raises(HostedTransportError, match="not_authorized") as exc:
        await client.async_claim(token=TOKEN)
    assert exc.value.status == 401 and "private" not in str(exc.value)


# Mutation regressions: strict source bounds, duplicate deliveries and one-shot HTTP contracts.
# All cases exercise the public claim transport or the same full-batch page validator.

def _assert_bad_page(modify):
    value = page()
    modify(value)
    with pytest.raises(ValueError):
        _page(value, 20, NOW)


def test_field_and_scalar_helpers_reject_wrong_types_and_allow_one_character():
    from custom_components.daylight_calendar_import.hosted_source_transport import _fields, _string
    with pytest.raises(ValueError):
        _fields(["kind"], {"kind"})
    with pytest.raises(ValueError):
        _string(123, 4)
    assert _string("a", 1) == "a"


@pytest.mark.parametrize("field,invalid", [
    ("title", ""), ("title", 4), ("title", "t" * 513),
    ("text", 4), ("text", "t" * 1_000_001),
])
def test_source_field_bounds_reject_bad_values(field, invalid):
    _assert_bad_page(lambda p: p["deliveries"][0]["source"].__setitem__(field, invalid))


@pytest.mark.parametrize("invalid", ["", 4, "s" * 513])
def test_sender_bounds_reject_bad_values(invalid):
    _assert_bad_page(lambda p: p["deliveries"][0]["source"]["metadata"].__setitem__("sender", invalid))


@pytest.mark.parametrize("invalid", ["", 5, "f" * 513])
def test_attachment_filename_bounds_reject_bad_values(invalid):
    def modify(value):
        value["deliveries"][0]["source"]["attachments"] = [{**attachment(), "filename": invalid}]
    _assert_bad_page(modify)


@pytest.mark.parametrize("change", [
    lambda s: s.update(attachments=tuple([attachment()])),
    lambda s: s.update(attachments=[{**attachment(), "id": f"a{i}.png", "size_bytes": 1} for i in range(5)]),
    lambda s: s.update(attachments=[{**attachment(), "size_bytes": 0}]),
    lambda s: s.update(attachments=[{**attachment(), "size_bytes": 10_485_761}]),
    lambda s: s.update(attachments=[{**attachment(), "media_type": 1}]),
    lambda s: s.update(attachments=[{**attachment(), "media_type": type("Media", (str,), {})("image/png")}]),
])
def test_rejects_attachment_shapes_and_cardinality(change):
    _assert_bad_page(lambda p: change(p["deliveries"][0]["source"]))


def test_valid_attachment_boundary_sizes_and_metadata():
    value = page()
    source = value["deliveries"][0]["source"]
    source["text"] = None
    source["title"] = "x"
    source["metadata"]["sender"] = "s" * 512
    source["attachments"] = [
        {**attachment(), "id": "a0.png", "size_bytes": 10_485_759, "filename": "f" * 512},
        {**attachment(), "id": "a1.png", "size_bytes": 1},
    ]
    assert _page(value, 20, NOW) == value
    source["text"] = "t" * 1_000_000
    assert _page(value, 20, NOW) == value


def test_source_accepts_lowercase_iso_timestamp():
    value = page()
    value["deliveries"][0]["source"]["received_at"] = "2026-11-08t12:30:00z"
    assert _page(value, 20, NOW) == value


@pytest.mark.parametrize("duplicate_key", ["delivery_id", "source_id"])
def test_page_rejects_duplicate_delivery_identifiers_independently(duplicate_key):
    value = page()
    original = value["deliveries"][0]
    second = deepcopy(original)
    second["delivery_id"] = "a" * 32
    second["source_id"] = "b" * 32
    second[duplicate_key] = original[duplicate_key]
    value["deliveries"].append(second)
    with pytest.raises(ValueError):
        _page(value, 20, NOW)


def test_page_accepts_distinct_delivery_and_source_ids():
    value = page()
    second = deepcopy(value["deliveries"][0])
    second["delivery_id"] = "a" * 32
    second["source_id"] = "b" * 32
    value["deliveries"].append(second)
    assert len(_page(value, 2, NOW)["deliveries"]) == 2
    with pytest.raises(ValueError):
        _page(value, 1, NOW)


def test_expiration_boundaries_and_equal_lease_and_source_expiry():
    value = page()
    item = value["deliveries"][0]
    limit = NOW + timedelta(days=7, minutes=5)
    item["source_expires_at"] = limit.isoformat()
    item["lease_expires_at"] = limit.isoformat()
    assert _page(value, 20, NOW) == value
    item["source_expires_at"] = (limit + timedelta(seconds=1)).isoformat()
    with pytest.raises(ValueError):
        _page(value, 20, NOW)
    item["source_expires_at"] = NOW.isoformat()
    item["lease_expires_at"] = NOW.isoformat()
    with pytest.raises(ValueError):
        _page(value, 20, NOW)


def _claim_session(*, raw=None, value=None, status=200, headers=None, content_type="application/json"):
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = Response(
        raw=raw, value=page() if raw is None and value is None else value,
        status=status, headers={"Cache-Control": "no-store"} if headers is None else headers,
        content_type=content_type,
    )
    return session


async def test_claim_exact_http_security_options_and_status():
    session = _claim_session()
    response = session.request.return_value
    client = HostedSourceTransport(session, SERVICE)
    assert await client.async_claim(token=TOKEN, limit=50) == page()
    args, opts = session.request.call_args
    assert args == ("POST", SERVICE.api_origin + "/v1/sources/claim")
    assert opts["headers"] == {
        "Accept": "application/json", "Accept-Encoding": "identity", "Authorization": "Bearer " + TOKEN,
    }
    assert opts["json"] == {"schema_version": 1, "limit": 50}
    assert opts["allow_redirects"] is False
    assert opts["timeout"].total == 15
    assert opts["max_line_size"] == 4096
    assert opts["max_field_size"] == 4096
    assert opts["max_headers"] == 32
    assert opts["auto_decompress"] is False
    assert response.closed and session.request.call_count == 1


@pytest.mark.parametrize("limit", [False, 51, "1", -1])
async def test_claim_rejects_noninteger_or_excessive_limit(limit):
    session = _claim_session()
    with pytest.raises(ValueError, match="^invalid claim request$"):
        await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN, limit=limit)
    session.request.assert_not_called()


@pytest.mark.parametrize("token", [123, "", "t" * 42, "t" * 257, "!" * 43])
async def test_claim_rejects_invalid_tokens_without_network(token):
    session = _claim_session()
    with pytest.raises(ValueError, match="^invalid claim request$"):
        await HostedSourceTransport(session, SERVICE).async_claim(token=token)
    session.request.assert_not_called()


async def test_claim_accepts_uppercase_token_and_preserves_case():
    session = _claim_session()
    token = "T" * 43
    await HostedSourceTransport(session, SERVICE).async_claim(token=token)
    assert session.request.call_args.kwargs["headers"]["Authorization"] == "Bearer " + token


def test_claim_rejects_untrusted_binding_with_specific_error():
    session = _claim_session()
    with pytest.raises(ValueError, match="^invalid trusted service$"):
        HostedSourceTransport(session, "https://api.daylight.test")


async def test_claim_uses_fifteen_second_outer_timeout(monkeypatch):
    from custom_components.daylight_calendar_import import hosted_source_transport as module
    real_timeout = module.asyncio.timeout
    arguments = []
    def observe_timeout(seconds):
        arguments.append(seconds)
        return real_timeout(seconds)
    monkeypatch.setattr(module.asyncio, "timeout", observe_timeout)
    session = _claim_session()
    await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)
    assert arguments == [15]


@pytest.mark.parametrize("status,headers,ctype", [
    (300, {"Cache-Control": "no-store"}, "application/json"),
    (301, {"Cache-Control": "no-store"}, "application/json"),
    (302, {"Cache-Control": "no-store"}, "application/json"),
    (200, {"Cache-Control": "no-store", "Content-Encoding": "gzip"}, "application/json"),
    (200, {"Cache-Control": "no-store"}, "text/plain"),
    (200, {}, "application/json"),
])
async def test_claim_rejects_unsafe_http_envelopes(status, headers, ctype):
    session = _claim_session(status=status, headers=headers, content_type=ctype)
    with pytest.raises(HostedTransportError) as error:
        await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)
    assert error.value.code == "invalid_response"
    assert error.value.status == status
    assert session.request.call_count == 1


@pytest.mark.parametrize("header", ["max-age=0, no-store", "no-cache,no-store", "NO-STORE"])
async def test_claim_accepts_compound_cache_control(header):
    session = _claim_session(headers={"Cache-Control": header})
    assert await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN) == page()


async def test_claim_duplicate_fields_in_otherwise_valid_json_are_rejected():
    raw = json.dumps(page()).replace('"schema_version": 1', '"schema_version": 1, "schema_version": 1', 1).encode()
    session = _claim_session(raw=raw)
    with pytest.raises(HostedTransportError, match="invalid_response"):
        await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)


async def test_claim_rejects_surrogate_in_otherwise_valid_json():
    value = page()
    value["deliveries"][0]["source"]["text"] = "\ud800"
    session = _claim_session(raw=json.dumps(value).encode())
    with pytest.raises(HostedTransportError, match="invalid_response"):
        await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)


async def test_claim_accepts_exact_wire_byte_limit(monkeypatch):
    from custom_components.daylight_calendar_import import hosted_source_transport as module
    raw = json.dumps(page()).encode()
    monkeypatch.setattr(module, "MAX_CLAIM_BYTES", len(raw))
    session = _claim_session(raw=raw)
    response = session.request.return_value
    seen_chunk_sizes = []
    async def iter_chunked(size):
        seen_chunk_sizes.append(size)
        yield raw
    response.content.iter_chunked = iter_chunked
    assert await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN, limit=1) == page()
    assert seen_chunk_sizes == [16384]


@pytest.mark.parametrize("status,code,retry_after", [
    (400, "invalid_request", None),
    (401, "not_authorized", None),
    (429, "rate_limited", "42"),
])
async def test_claim_preserves_valid_error_status_and_retry_after(status, code, retry_after):
    error = {"schema_version": 1, "error": {"code": code, "message": "private", "retryable": True}}
    headers = {"Cache-Control": "no-store"}
    if retry_after:
        headers["Retry-After"] = retry_after
    session = _claim_session(value=error, status=status, headers=headers)
    with pytest.raises(HostedTransportError) as raised:
        await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)
    assert raised.value.code == code
    assert raised.value.status == status
    assert raised.value.retryable is True
    assert raised.value.retry_after == (int(retry_after) if retry_after else None)
    assert "private" not in str(raised.value)


async def test_claim_transport_error_after_response_preserves_status_and_retryable():
    session = _claim_session(status=503)
    response = session.request.return_value
    async def corrupt_body(_size):
        raise aiohttp.ClientPayloadError("sensitive response body")
        yield b""  # pragma: no cover
    response.content.iter_chunked = corrupt_body
    with pytest.raises(HostedTransportError) as raised:
        await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)
    assert raised.value.code == "service_unavailable"
    assert raised.value.status == 503
    assert raised.value.retryable is True
    assert "sensitive" not in str(raised.value)
    assert session.request.call_count == 1



async def test_attachment_download_is_bounded_and_lease_authenticated():
    import hashlib
    content = b"synthetic-png"
    descriptor = {"id": "image.png", "media_type": "image/png", "size_bytes": len(content),
                  "sha256": hashlib.sha256(content).hexdigest()}
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = Response(raw=content, content_type="image/png", headers={
        "Cache-Control": "no-store", "Content-Length": str(len(content)),
    })
    result = await HostedSourceTransport(session, SERVICE).async_fetch_attachment(
        token=TOKEN, delivery_id="d" * 32, lease_token="l" * 32, descriptor=descriptor,
    )
    assert result == content
    args, kwargs = session.request.call_args
    assert args == ("GET", SERVICE.api_origin + "/v1/sources/" + "d" * 32 + "/attachments/image.png")
    assert kwargs["headers"]["Authorization"] == "Bearer " + TOKEN
    assert kwargs["headers"]["X-Daylight-Lease-Token"] == "l" * 32
    assert kwargs["allow_redirects"] is False and kwargs["auto_decompress"] is False
    assert session.request.call_count == 1


@pytest.mark.parametrize("override", [
    {"token": "bad"}, {"token": None}, {"delivery_id": "../bad"}, {"delivery_id": None},
    {"lease_token": "bad"}, {"lease_token": None},
    {"descriptor": None}, {"descriptor": {"id": "../bad"}},
    {"descriptor": {"id": "..", "media_type": "image/png", "size_bytes": 1, "sha256": "a" * 64}},
    {"descriptor": {"id": "a", "media_type": "text/html", "size_bytes": 1, "sha256": "a" * 64}},
    {"descriptor": {"id": "a", "media_type": 1, "size_bytes": 1, "sha256": "a" * 64}},
    {"descriptor": {"id": "a", "media_type": "image/png", "size_bytes": True, "sha256": "a" * 64}},
    {"descriptor": {"id": "a", "media_type": "image/png", "size_bytes": 0, "sha256": "a" * 64}},
    {"descriptor": {"id": "a", "media_type": "image/png", "size_bytes": 10_485_761, "sha256": "a" * 64}},
    {"descriptor": {"id": "a", "media_type": "image/png", "size_bytes": 1, "sha256": "bad"}},
])
async def test_attachment_rejects_invalid_inputs_before_network(override):
    session = MagicMock(spec=aiohttp.ClientSession)
    args = {"token": TOKEN, "delivery_id": "d" * 32, "lease_token": "l" * 32,
            "descriptor": attachment()}
    args.update(override)
    with pytest.raises(ValueError, match="invalid attachment request"):
        await HostedSourceTransport(session, SERVICE).async_fetch_attachment(**args)
    session.request.assert_not_called()


@pytest.mark.parametrize("status,headers,content_type,body,code", [
    (302, {"Cache-Control": "no-store"}, "image/png", b"hello", "invalid_response"),
    (200, {"Cache-Control": "no-store", "Content-Encoding": "gzip"}, "image/png", b"hello", "invalid_response"),
    (200, {}, "image/png", b"hello", "invalid_response"),
    (200, {"Cache-Control": "no-store", "Content-Length": "5"}, "text/html", b"hello", "invalid_response"),
    (200, {"Cache-Control": "no-store", "Content-Length": "6"}, "image/png", b"hello", "invalid_response"),
    (200, {"Cache-Control": "no-store", "Content-Length": "5"}, "image/png", b"helloo", "invalid_response"),
    (200, {"Cache-Control": "no-store", "Content-Length": "5"}, "image/png", b"hell", "invalid_response"),
    (200, {"Cache-Control": "no-store", "Content-Length": "5"}, "image/png", b"wrong", "invalid_response"),
    (409, {"Cache-Control": "no-store"}, "application/json", b"{}", "lease_not_current"),
    (429, {"Cache-Control": "no-store"}, "application/json", b"{}", "rate_limited"),
    (503, {"Cache-Control": "no-store"}, "application/json", b"{}", "attachment_unavailable"),
])
async def test_attachment_rejects_unsafe_envelopes_and_integrity_mismatches(
    status, headers, content_type, body, code,
):
    import hashlib
    descriptor = {"id": "a", "media_type": "image/png", "size_bytes": 5,
                  "sha256": hashlib.sha256(b"hello").hexdigest()}
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = Response(raw=body, status=status, headers=headers,
                                            content_type=content_type)
    with pytest.raises(HostedTransportError) as error:
        await HostedSourceTransport(session, SERVICE).async_fetch_attachment(
            token=TOKEN, delivery_id="d" * 32, lease_token="l" * 32, descriptor=descriptor,
        )
    assert error.value.code == code
    assert error.value.retryable is (status in (429, 503))


async def test_attachment_network_failure_never_retries():
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.side_effect = aiohttp.ClientConnectionError()
    with pytest.raises(HostedTransportError, match="service_unavailable"):
        await HostedSourceTransport(session, SERVICE).async_fetch_attachment(
            token=TOKEN, delivery_id="d" * 32, lease_token="l" * 32, descriptor=attachment(),
        )
    assert session.request.call_count == 1


# Mutation coverage for the H3 attachment download wire contract. Keep unrelated
# validation fields valid so removal of any one safeguard fails its own case.
def _download_fixture(raw=b"hello", *, size=None, media="image/png", headers=None, status=200,
                      descriptor_overrides=None):
    import hashlib
    evidence = {
        "id": "a.png",
        "media_type": media,
        "size_bytes": len(raw) if size is None else size,
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    evidence.update(descriptor_overrides or {})
    session = MagicMock(spec=aiohttp.ClientSession)
    response = Response(
        raw=raw, status=status, content_type=media,
        headers=({"Cache-Control": "no-store",
                  "Content-Length": str(evidence["size_bytes"])}
                 if headers is None else headers),
    )
    session.request.return_value = response
    return session, response, evidence


async def _fetch_download(session, descriptor, *, token=TOKEN):
    return await HostedSourceTransport(session, SERVICE).async_fetch_attachment(
        token=token, delivery_id="d" * 32, lease_token="l" * 32, descriptor=descriptor,
    )


async def test_attachment_request_has_exact_trusted_origin_headers_and_limits(monkeypatch):
    from custom_components.daylight_calendar_import import hosted_source_transport as module

    observed_timeouts = []
    real_timeout = module.asyncio.timeout

    def record_timeout(seconds):
        observed_timeouts.append(seconds)
        return real_timeout(seconds)

    monkeypatch.setattr(module.asyncio, "timeout", record_timeout)
    session, response, descriptor = _download_fixture()
    assert await _fetch_download(session, descriptor) == b"hello"
    assert observed_timeouts == [30]
    assert response.closed
    assert session.request.call_count == 1
    args, options = session.request.call_args
    assert args == ("GET", SERVICE.api_origin + "/v1/sources/" + "d" * 32
                    + "/attachments/a.png")
    assert options["headers"] == {
        "Accept": "image/png",
        "Accept-Encoding": "identity",
        "Authorization": "Bearer " + TOKEN,
        "X-Daylight-Lease-Token": "l" * 32,
    }
    assert set(options) == {
        "headers", "allow_redirects", "timeout", "max_line_size",
        "max_field_size", "max_headers", "auto_decompress",
    }
    assert options["allow_redirects"] is False
    assert options["auto_decompress"] is False
    assert isinstance(options["timeout"], aiohttp.ClientTimeout)
    assert options["timeout"].total == 30
    assert options["max_line_size"] == 4096
    assert options["max_field_size"] == 4096
    assert options["max_headers"] == 32


@pytest.mark.parametrize("body_size", [1, 10_485_760])
async def test_attachment_download_accepts_exact_permitted_size_boundaries(body_size):
    raw = b"p" * body_size
    session, _, descriptor = _download_fixture(raw)
    assert await _fetch_download(session, descriptor) == raw


async def test_attachment_optional_filename_is_permitted_but_does_not_affect_url():
    session, _, descriptor = _download_fixture(
        descriptor_overrides={"filename": "School flyer – original.png"},
    )
    assert await _fetch_download(session, descriptor) == b"hello"
    assert session.request.call_args.args[1].endswith("/attachments/a.png")


@pytest.mark.parametrize("token", ["T" * 43, "t" * 43])
async def test_attachment_authentication_token_case_is_preserved(token):
    session, _, descriptor = _download_fixture()
    assert await _fetch_download(session, descriptor, token=token) == b"hello"
    assert session.request.call_args.kwargs["headers"]["Authorization"] == "Bearer " + token


async def test_attachment_invalid_inputs_have_exact_error_without_request():
    for override in ({"token": "bad"}, {"descriptor": {"id": "a.png"}}):
        session, _, descriptor = _download_fixture()
        values = {"token": TOKEN, "delivery_id": "d" * 32,
                  "lease_token": "l" * 32, "descriptor": descriptor}
        values.update(override)
        with pytest.raises(ValueError, match=r"^invalid attachment request$"):
            await HostedSourceTransport(session, SERVICE).async_fetch_attachment(**values)
        session.request.assert_not_called()


@pytest.mark.parametrize("headers", [
    {"Content-Length": "5"},  # cache-control omission alone
    {"Cache-Control": "no-store", "Content-Length": "5", "Content-Encoding": "gzip"},
])
async def test_attachment_rejects_each_unsafe_header_independently(headers):
    session, response, descriptor = _download_fixture(headers=headers)
    with pytest.raises(HostedTransportError) as raised:
        await _fetch_download(session, descriptor)
    assert raised.value.code == "invalid_response"
    assert raised.value.status == 200
    assert raised.value.retryable is False
    assert response.closed


@pytest.mark.parametrize("cache_control", [
    "max-age=0, no-store",
    "NO-CACHE, NO-STORE",
    "private,no-store",
])
async def test_attachment_accepts_comma_separated_no_store(cache_control):
    session, _, descriptor = _download_fixture(headers={
        "Cache-Control": cache_control, "Content-Length": "5",
    })
    assert await _fetch_download(session, descriptor) == b"hello"


@pytest.mark.parametrize("status,code,retryable", [
    (300, "invalid_response", False),
    (301, "invalid_response", False),
    (302, "invalid_response", False),
    (399, "invalid_response", False),
    (400, "attachment_unavailable", False),
    (401, "not_authorized", False),
    (402, "attachment_unavailable", False),
    (403, "entitlement_required", False),
    (404, "not_found", False),
    (409, "lease_not_current", False),
    (429, "rate_limited", True),
    (499, "attachment_unavailable", False),
    (500, "attachment_unavailable", True),
    (501, "attachment_unavailable", True),
])
async def test_attachment_status_mapping_preserves_code_status_and_retryability(
    status, code, retryable,
):
    session, response, descriptor = _download_fixture(status=status)
    with pytest.raises(HostedTransportError) as raised:
        await _fetch_download(session, descriptor)
    assert raised.value.code == code
    assert raised.value.status == status
    assert raised.value.retryable is retryable
    assert response.closed


async def test_attachment_stream_requests_fixed_chunks_and_aborts_on_overrun():
    session, response, descriptor = _download_fixture(size=5)
    chunk_sizes = []

    async def oversized_body(chunk_size):
        chunk_sizes.append(chunk_size)
        yield b"123456"
        pytest.fail("over-limit response must be rejected before the next chunk")

    response.content.iter_chunked = oversized_body
    with pytest.raises(HostedTransportError) as raised:
        await _fetch_download(session, descriptor)
    assert chunk_sizes == [16384]
    assert raised.value.code == "invalid_response"
    assert raised.value.status == 200


async def test_attachment_payload_transport_failure_after_headers_keeps_status():
    session, response, descriptor = _download_fixture(status=200)

    async def interrupted_body(_chunk_size):
        raise aiohttp.ClientPayloadError("private upstream response")
        yield b""  # pragma: no cover

    response.content.iter_chunked = interrupted_body
    with pytest.raises(HostedTransportError) as raised:
        await _fetch_download(session, descriptor)
    assert raised.value.code == "service_unavailable"
    assert raised.value.status == 200
    assert raised.value.retryable is True
    assert "private" not in str(raised.value)
    assert response.closed
    assert session.request.call_count == 1


async def test_claim_integral_json_float_normalized_exactly_by_shared_wire_decoder():
    response = page()
    raw = json.dumps(response).replace('"schema_version": 1', '"schema_version": 1.0').encode()
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = Response(raw=raw, headers={"Cache-Control": "no-store"})
    assert await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN) == response


@pytest.mark.parametrize("status", [300, 301, 302, 399])
async def test_claim_rejects_every_http_redirect_without_following(status):
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.return_value = Response(
        value=page(), status=status, headers={"Cache-Control": "no-store"},
    )
    with pytest.raises(HostedTransportError) as caught:
        await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)
    assert (caught.value.code, caught.value.status) == ("invalid_response", status)
    assert session.request.call_args.kwargs["allow_redirects"] is False


async def test_claim_failure_before_headers_preserves_absent_status():
    session = MagicMock(spec=aiohttp.ClientSession)
    session.request.side_effect = aiohttp.ClientConnectionError("private provider response")
    with pytest.raises(HostedTransportError) as caught:
        await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)
    assert (caught.value.code, caught.value.status, caught.value.retryable) == (
        "service_unavailable", None, True,
    )
    assert "private" not in str(caught.value)
