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
    result = await HostedSourceTransport(session, SERVICE).async_claim(token=TOKEN)
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
