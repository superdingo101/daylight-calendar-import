"""Dormant hosted parser conformance; no provider/model calls or activation."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from hashlib import sha256
import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from custom_components.daylight_calendar_import.hosted_credentials import (
    BoundCredential, CandidateCredential, CredentialCheckpoint, ReconnectIntent,
)
from custom_components.daylight_calendar_import.hosted_parse_transport import HostedParseError
from custom_components.daylight_calendar_import.hosted_parser import (
    DaylightHostedParserProvider, _attachment_bytes, _draft, _object, _text, _warnings,
    parse_hosted_result,
)
from custom_components.daylight_calendar_import.hosted_transport import HostedServiceBinding
from custom_components.daylight_calendar_import.models import UNSUPPORTED_RECURRENCE_WARNING
from custom_components.daylight_calendar_import.providers import SourceValidationError
from custom_components.daylight_calendar_import.sources import (
    SourceAttachment, SourceDocument, SourceKind, TextSourceAdapter,
)

FIXTURES = Path(__file__).parent / "fixtures/hosted/v1/valid"
SERVICE = HostedServiceBinding("https://api.daylight.test", "https://app.daylight.test")
TOKEN = "T" * 43


def result():
    return json.loads((FIXTURES / "parse-response-partial.json").read_text())


def credential(**kwargs):
    return BoundCredential("A" * 32, "I" * 32, TOKEN,
                           (datetime.now(UTC) + timedelta(days=1)).isoformat(), **kwargs)


def checkpoint():
    return CredentialCheckpoint(service_binding=SERVICE, processing_consent=True, current=credential())


@pytest.fixture
async def provider(hass):
    saved = [checkpoint()]
    active = [True]
    client = DaylightHostedParserProvider(
        hass, service_binding=SERVICE, checkpoint=lambda: saved[0], active=lambda: active[0],
    )
    client._transport = MagicMock()
    client._transport.async_parse = AsyncMock(return_value=result())
    return client, saved, active


def test_response_fixtures_and_partial_mapping():
    source = TextSourceAdapter().create("One-off source")
    for name in ("parse-response-partial", "parse-response-long-description", "parse-response-remote-meeting"):
        raw = json.loads((FIXTURES / f"{name}.json").read_text())
        parsed = parse_hosted_result(raw, source)
        assert len(parsed.events) == len(raw["events"])
        assert parsed.events[0].description == (raw["events"][0]["draft"]["description"].strip() if raw["events"][0]["draft"]["description"] else None)
        assert parsed.event_assumptions == [()]
    parsed = parse_hosted_result(result(), source)
    assert parsed.warnings == [
        "source_partial_parse: One candidate was rejected.",
        "event 0: provider_low_confidence: One source detail was ambiguous.",
        "candidate 1: invalid_time_range: End time was not after start time.",
    ]
    raw = result()
    raw["rejected"][0]["index"] = None
    parsed = parse_hosted_result(raw, source)
    assert parsed.warnings[-1].startswith("candidate: invalid_time_range:")
    raw["rejected"][0]["index"] = Decimal("10485761.0")
    assert "candidate 10485761.0:" in parse_hosted_result(raw, source).warnings[-1]
    raw["events"][0]["draft"]["confidence"] = Decimal("0.94")
    assert parse_hosted_result(raw, source).events[0].confidence == 0.94


@pytest.mark.parametrize("where", ["source", "global", "event", "rejected"])
def test_recurrence_guard(where):
    raw = result()
    source = TextSourceAdapter().create("Class meets every Monday" if where == "source" else "Class night")
    if where == "global":
        raw["warnings"][0]["code"] = "unsupported_recurrence"
    if where == "event":
        raw["events"][0]["warnings"][0]["code"] = "unsupported_recurrence"
    if where == "rejected":
        raw["rejected"][0]["reason_code"] = "unsupported_recurrence"
    parsed = parse_hosted_result(raw, source)
    assert parsed.events == []
    assert UNSUPPORTED_RECURRENCE_WARNING in parsed.warnings


def test_all_day_and_empty_result():
    raw = result()
    raw["events"][0]["draft"].update(all_day=True, start="2026-11-05", end="2026-11-06")
    raw["provider_metadata"] = {"ignored": {"list": ["data", 123]}}
    assert parse_hosted_result(raw, TextSourceAdapter().create("School holiday")).events[0].all_day
    raw["events"] = raw["rejected"] = raw["warnings"] = []
    assert parse_hosted_result(raw, TextSourceAdapter().create("No date")).events == []


@pytest.mark.parametrize("value", [None, {}, [], "x"])
def test_bad_top_level(value):
    with pytest.raises(ValueError):
        parse_hosted_result(value, TextSourceAdapter().create("source"))


@pytest.mark.parametrize("key,value", [
    ("schema_version", True), ("schema_version", 2),
    ("events", None), ("events", [None] * 257), ("rejected", {}),
    ("rejected", [None] * 257), ("provider_metadata", []),
    ("provider_metadata", {str(i): i for i in range(33)}),
    ("provider_metadata", {"x" * 129: 1}), ("provider_metadata", {1: "x"}),
    ("warnings", {}), ("warnings", [None] * 129),
    ("warnings", [{"code": "bad-code", "message": "x", "field": None}]),
    ("warnings", [{"code": "okay", "message": "", "field": None}]),
    ("warnings", [{"code": "okay", "message": "x", "field": ""}]),
])
def test_invalid_result_fields(key, value):
    raw = result()
    raw[key] = value
    with pytest.raises(ValueError):
        parse_hosted_result(raw, TextSourceAdapter().create("source"))


@pytest.mark.parametrize("key,value", [
    ("title", ""), ("title", "x" * 513), ("location", "x" * 2049),
    ("description", "x" * 65537), ("all_day", 1), ("start", "not date"),
    ("start", "2026-11-05T18:00:00"), ("end", "2026-11-05T17:00:00-08:00"),
    ("confidence", True), ("confidence", -0.01), ("confidence", Decimal("1.00000000000000000001")),
])
def test_invalid_event_fields(key, value):
    raw = result()
    raw["events"][0]["draft"][key] = value
    with pytest.raises(ValueError):
        parse_hosted_result(raw, TextSourceAdapter().create("source"))


def test_nested_structure_and_all_day_validation():
    with pytest.raises(ValueError):
        _object({"extra": 1}, set())
    with pytest.raises(ValueError):
        _text("\ud800", 10)
    with pytest.raises(ValueError):
        _warnings([{"code": 1, "message": "x", "field": None}], 1)
    draft = result()["events"][0]["draft"]
    draft.update(all_day=True, start="20261105", end="2026-11-06")
    with pytest.raises(ValueError):
        _draft(draft)
    draft["start"] = None
    with pytest.raises(ValueError):
        _draft(draft)
    raw = result()
    raw["events"][0]["warnings"] *= 65
    with pytest.raises(ValueError):
        parse_hosted_result(raw, TextSourceAdapter().create("source"))


@pytest.mark.parametrize("key,value", [
    ("index", True), ("index", -1), ("index", Decimal("10485761.5")), ("reason_code", "bad code"),
    ("reason_code", 123), ("message", ""), ("raw_summary", ""),
])
def test_invalid_rejected_candidate(key, value):
    raw = result()
    raw["rejected"][0][key] = value
    with pytest.raises(ValueError):
        parse_hosted_result(raw, TextSourceAdapter().create("source"))


async def test_text_request_uses_minimal_canonical_source_and_current_authority(provider):
    client, saved, _ = provider
    source = replace(TextSourceAdapter().create("School night"), metadata={"private": "local routing"},
                     upstream_source_id="private IMAP id")
    parsed = await client.async_parse(source, reference_datetime="2026-10-28T10:00:00-07:00", time_zone="America/Los_Angeles")
    assert len(parsed.events) == 1
    assert client.capabilities.text and client.capabilities.images and client.capabilities.pdfs
    request, binaries = client._transport.async_parse.call_args.args
    assert binaries == ()
    assert request == {
        "schema_version": 1, "reference_datetime": "2026-10-28T10:00:00-07:00",
        "time_zone": "America/Los_Angeles", "source": {
            "id": source.id, "kind": "manual_text", "received_at": source.received_at.isoformat(),
            "title": None, "text": "School night", "attachments": [], "metadata": {},
        },
    }
    assert client._transport.async_parse.call_args.kwargs == {"token": saved[0].current.token}
    assert TOKEN not in repr(client)


@pytest.mark.parametrize("change", ["inactive", "missing", "service", "consent", "disconnect", "credential", "expired", "reconnect", "getter_error"])
async def test_authority_gates_no_network(provider, change):
    client, saved, active = provider
    if change == "inactive":
        active[0] = False
    elif change == "missing":
        saved[0] = None
    elif change == "service":
        saved[0] = replace(saved[0], service_binding=HostedServiceBinding("https://other.test", "https://app.daylight.test"))
    elif change == "consent":
        saved[0] = CredentialCheckpoint(service_binding=SERVICE)
    elif change == "disconnect":
        saved[0] = replace(saved[0], local_disconnect="unconfirmed")
    elif change == "credential":
        saved[0] = replace(saved[0], current=None)
    elif change == "expired":
        saved[0] = replace(saved[0], current=replace(saved[0].current, expires_at=(datetime.now(UTC)-timedelta(seconds=1)).isoformat()))
    elif change == "reconnect":
        saved[0] = replace(saved[0], pairing_verifier="V"*43, reconnect=ReconnectIntent("A"*32, "I"*32, "none", "pending-v2-local-only"))
    else:
        client._checkpoint = MagicMock(side_effect=RuntimeError("private data"))
    with pytest.raises(HostedParseError) as error:
        await client.async_parse(TextSourceAdapter().create("source"), reference_datetime="2026-10-28T10:00:00Z", time_zone="UTC")
    assert "private" not in str(error.value)
    client._transport.async_parse.assert_not_called()


async def test_current_working_credential_survives_candidate_staging(provider):
    client, saved, _ = provider
    saved[0] = replace(saved[0], candidate=CandidateCredential("I"*32, "C"*43,
                        saved[0].current.expires_at, saved[0].current.expires_at))
    assert len((await client.async_parse(TextSourceAdapter().create("source"), reference_datetime="2026-10-28T10:00:00Z", time_zone="GMT")).events) == 1


async def test_credential_change_after_response_rejects_stale_result(provider):
    client, saved, _ = provider
    async def changed(*args, **kwargs):
        saved[0] = replace(saved[0], current=replace(saved[0].current, token="R"*43))
        return result()
    client._transport.async_parse.side_effect = changed
    with pytest.raises(HostedParseError, match="Daylight Cloud"):
        await client.async_parse(TextSourceAdapter().create("source"), reference_datetime="2026-10-28T10:00:00Z", time_zone="UTC")
    assert client._transport.async_parse.call_count == 1


async def test_invalid_response_is_fixed_error(provider):
    client, _, _ = provider
    client._transport.async_parse.return_value = {"private": "raw provider data"}
    with pytest.raises(HostedParseError) as error:
        await client.async_parse(TextSourceAdapter().create("source"), reference_datetime="2026-10-28T10:00:00Z", time_zone="UTC")
    assert error.value.code == "provider_invalid_response"
    assert "private" not in str(error.value)


@pytest.mark.parametrize("reference,zone", [("no date", "UTC"), ("2026-10-28T10:00:00Z", "No/SuchZone"), ("2026-10-28T10:00:00Z", "bad zone"), ("2026-10-28T10:00:00Z", "x"*129)])
async def test_bad_context_no_network(provider, reference, zone):
    client, _, _ = provider
    with pytest.raises(SourceValidationError):
        await client.async_parse(TextSourceAdapter().create("source"), reference_datetime=reference, time_zone=zone)
    client._transport.async_parse.assert_not_called()


def attachment(tmp_path, content=b"\x89PNG\r\n\x1a\nbody", media="image/png"):
    name = {"image/png": "daylight-fixture.png", "image/jpeg": "daylight-fixture.jpg", "image/webp": "daylight-email-fixture.webp", "application/pdf": "daylight-fixture.pdf"}[media]
    (tmp_path / name).write_bytes(content)
    return SourceAttachment("a", media, len(content), f"media-source://media_source/local/{name}", "flyer", sha256(content).hexdigest())


@pytest.mark.parametrize("content,media", [(b"\x89PNG\r\n\x1a\nbody", "image/png"), (b"\xff\xd8\xffbody", "image/jpeg"), (b"RIFFxxxxWEBPbody", "image/webp"), (b"%PDF-1.7 body", "application/pdf")])
def test_safe_attachment_read(tmp_path, content, media):
    item = attachment(tmp_path, content, media)
    assert _attachment_bytes({"local": str(tmp_path)}, item) == content
    assert _attachment_bytes({"local": str(tmp_path)}, replace(item, sha256=None)) == content


@pytest.mark.parametrize("ref", ["https://evil.test/x", "/etc/passwd", "media-source://other/local/daylight-x.png", "media-source://media_source/local/../secret", "media-source://media_source/other/daylight-x.png", "media-source://media_source/local/secret.png", "media-source://media_source/local/daylight-x.png?query=x", "media-source://media_source/local/daylight-x.png#x", "media-source://media_source/local/daylight-%2e.png"])
def test_unsafe_attachment_refs(tmp_path, ref):
    with pytest.raises(ValueError):
        _attachment_bytes({"local": str(tmp_path)}, replace(attachment(tmp_path), content_ref=ref))


@pytest.mark.parametrize("change", ["size", "hash", "type", "symlink", "directory"])
def test_unsafe_attachment_bytes(tmp_path, change):
    item = attachment(tmp_path)
    path = tmp_path / "daylight-fixture.png"
    if change == "size":
        item = replace(item, size_bytes=1)
    elif change == "hash":
        item = replace(item, sha256="0"*64)
    elif change == "type":
        item = replace(item, media_type="application/pdf")
    elif change == "symlink":
        path.unlink()
        path.symlink_to(tmp_path / "missing")
    else:
        path.unlink()
        path.mkdir()
    with pytest.raises((ValueError, OSError)):
        _attachment_bytes({"local": str(tmp_path)}, item)


async def test_attachment_request_and_change_during_file_read(provider, hass, tmp_path):
    client, saved, _ = provider
    hass.config.media_dirs = {"local": str(tmp_path)}
    item = attachment(tmp_path)
    source = SourceDocument("source", SourceKind.IMAGE, datetime.now(UTC), attachments=(item,))
    await client.async_parse(source, reference_datetime="2026-10-28T10:00:00Z", time_zone="UTC")
    request, binaries = client._transport.async_parse.call_args.args
    assert "content_ref" not in request["source"]["attachments"][0]
    assert binaries == (("a", "image/png", b"\x89PNG\r\n\x1a\nbody"),)
    client._transport.async_parse.reset_mock()
    async def change(*args):
        saved[0] = replace(saved[0], current=replace(saved[0].current, token="R"*43))
        return b"\x89PNG\r\n\x1a\nbody"
    hass.async_add_executor_job = change
    with pytest.raises(HostedParseError):
        await client.async_parse(source, reference_datetime="2026-10-28T10:00:00Z", time_zone="UTC")
    client._transport.async_parse.assert_not_called()


@pytest.mark.parametrize("change", ["duplicate", "id", "filename", "hash", "bool_size", "naive", "title"])
async def test_bad_source_metadata(provider, tmp_path, hass, change):
    client, _, _ = provider
    hass.config.media_dirs = {"local": str(tmp_path)}
    item = attachment(tmp_path)
    source = SourceDocument("source", SourceKind.IMAGE, datetime.now(UTC), attachments=(item,))
    if change == "duplicate":
        source = replace(source, attachments=(item, item))
    elif change == "id":
        source = replace(source, attachments=(replace(item, id="../x"),))
    elif change == "filename":
        source = replace(source, attachments=(replace(item, filename=""),))
    elif change == "hash":
        source = replace(source, attachments=(replace(item, sha256="bad"),))
    elif change == "bool_size":
        source = replace(source, attachments=(replace(item, size_bytes=True),))
    elif change == "naive":
        source = replace(source, received_at=datetime(2026, 10, 28))
    else:
        source = replace(source, title="x" * 513)
    with pytest.raises(SourceValidationError):
        await client.async_parse(source, reference_datetime="2026-10-28T10:00:00Z", time_zone="UTC")
    client._transport.async_parse.assert_not_called()


def test_attachment_shrinks_after_stat_is_not_sent(tmp_path, monkeypatch):
    import os
    from types import SimpleNamespace
    item = attachment(tmp_path)
    (tmp_path / "daylight-fixture.png").write_bytes(b"short")
    monkeypatch.setattr(os, "fstat", lambda _: SimpleNamespace(st_mode=0o100600, st_size=item.size_bytes))
    with pytest.raises(ValueError):
        _attachment_bytes({"local": str(tmp_path)}, item)


@pytest.mark.parametrize("start,end", [
    ("2026-11-01T01:15:00-07:00", "2026-11-01T01:45:00-08:00"),
    ("2026-03-08T01:45:00-08:00", "2026-03-08T03:15:00-07:00"),
    ("2026-11-05T18:00:00+01:00", "2026-11-05T19:00:00+01:00"),
])
def test_explicit_offsets_and_temporal_warnings_do_not_invent_inference(start, end):
    raw = result()
    raw["events"][0]["draft"].update(start=start, end=end)
    raw["events"][0]["warnings"] = [{
        "code": "ambiguous_source_time", "message": "Confirm the supplied local time.", "field": "start",
    }]
    parsed = parse_hosted_result(raw, TextSourceAdapter().create("School night"))
    assert (parsed.events[0].start, parsed.events[0].end) == (start, end)
    assert parsed.event_assumptions == [()]
    assert "event 0: ambiguous_source_time: Confirm the supplied local time." in parsed.warnings
