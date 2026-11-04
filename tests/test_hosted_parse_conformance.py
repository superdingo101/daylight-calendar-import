"""Compose the real hosted HTTP parser with durable native HA review.

Only credential verification and the remote service location are fixture priming;
transport decoding, provider selection, result mapping and storage remain real.
These synthetic local responses are not evidence of deployed model quality.
"""

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from aiohttp import web
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daylight_calendar_import.const import (
    CONF_PARSER_PROVIDER,
    DOMAIN,
    PARSER_PROVIDER_DAYLIGHT_CLOUD,
)
from custom_components.daylight_calendar_import.hosted_credentials import (
    BoundCredential,
    HostedCredentialStore,
    _ImmediatePrivateStore,
)
from custom_components.daylight_calendar_import.hosted_lifecycle import (
    HostedConnectionResult,
)
from custom_components.daylight_calendar_import.hosted_parse_transport import (
    HostedParseTransport,
)
from custom_components.daylight_calendar_import.hosted_runtime import CONF_HOSTED_SERVICE
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.storage import _PendingStore

from tests.test_hosted_lifecycle import _REAL_LOAD, _REAL_WRITE
from tests.test_hosted_parse_pipeline import SERVICE, call

FIXTURES = Path(__file__).parent / "fixtures/hosted/v1/valid"


def canonical_result(name="partial"):
    return json.loads((FIXTURES / f"parse-response-{name}.json").read_text())


@pytest.fixture
async def conformance_runtime(hass, monkeypatch, tmp_path, socket_enabled, aiohttp_server):
    hass.config.config_dir = str(tmp_path)
    # Escape HA's in-memory storage fixture for both private credentials and
    # pending review, so reload assertions read actual atomic files.
    for storage in (_ImmediatePrivateStore, _PendingStore):
        monkeypatch.setattr(storage, "_async_load", _REAL_LOAD)
        monkeypatch.setattr(storage, "_async_write_data", _REAL_WRITE)
    wire = SimpleNamespace(response=canonical_result(), received=[])

    async def parse(request):
        parts = []
        async for part in await request.multipart():
            parts.append((part.name, part.headers.get("Content-Type"), await part.read()))
        wire.received.append((request.method, parts))
        return web.json_response(wire.response)

    app = web.Application()
    app.router.add_post("/v1/parse", parse)
    server = await aiohttp_server(app)
    original_init = HostedParseTransport.__init__

    def local_transport(self, session, service):
        original_init(self, session, service)
        # Keep the reviewed HTTPS binding in the provider/runtime/checkpoint.
        # Only the transport's remote destination is redirected to local HTTP.
        self._service = SimpleNamespace(api_origin=str(server.make_url("")).rstrip("/"))

    monkeypatch.setattr(HostedParseTransport, "__init__", local_transport)
    generate = AsyncMock(side_effect=AssertionError("Hosted selection invoked BYO"))
    monkeypatch.setattr("homeassistant.components.ai_task.async_generate_data", generate)
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_classify_conflicts",
        AsyncMock(return_value=()),
    )
    writes = AsyncMock()
    hass.services.async_register("calendar", "create_event", writes)
    entry = MockConfigEntry(
        domain=DOMAIN,
        version=2,
        data={CONF_PARSER_PROVIDER: PARSER_PROVIDER_DAYLIGHT_CLOUD},
        options={CONF_HOSTED_SERVICE: {
            "api_origin": SERVICE.api_origin,
            "account_origin": SERVICE.account_origin,
        }},
    )
    entry.add_to_hass(hass)
    credentials = HostedCredentialStore(hass, entry.entry_id)
    initial = await credentials.async_load()
    credential = BoundCredential(
        "account-identifier-1234",
        "installation-identifier-1234",
        "x" * 60,
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    await credentials.async_checkpoint(initial, replace(
        initial, current=credential, processing_consent=True, service_binding=SERVICE,
    ))
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    def verify():
        # M2 suites own remote verification. Exercise the unmodified M3 factory
        # using an actually loaded, persisted credential after each HA reload.
        store = hass.data[DOMAIN][entry.entry_id]
        connection = store.hosted_runtime
        assert connection._credential_store.snapshot.current == credential
        connection._verified_parse_credential = credential
        connection._state = HostedConnectionResult("connected", "current")
        return store

    return SimpleNamespace(
        entry=entry, store=verify(), verify=verify, wire=wire, writes=writes, generate=generate,
    )


async def test_mixed_http_result_survives_reload_and_authorized_review(
    hass, hass_ws_client, conformance_runtime,
):
    runtime = conformance_runtime
    result = canonical_result()
    result["events"].extend(canonical_result("long-description")["events"])
    result["events"].extend(canonical_result("remote-meeting")["events"])
    template = deepcopy(result["events"][0]["draft"])
    for draft in (
        {**template, "title": "School holiday", "all_day": True,
         "start": "2026-11-05", "end": "2026-11-06"},
        {**template, "title": "DST transition",
         "start": "2026-11-01T01:30:00-07:00", "end": "2026-11-01T02:30:00-08:00"},
        {**template, "title": "Foreign time zone",
         "start": "2026-11-02T19:00:00+01:00", "end": "2026-11-02T20:00:00+01:00"},
    ):
        result["events"].append({"draft": draft, "warnings": []})
    runtime.wire.response = result
    expected = [EventDraft.from_mapping(event["draft"]) for event in result["events"]]
    client = await hass_ws_client(hass)
    submitted = await call(
        client, "submit_text", text="Independent school dates and meeting details.",
        source_id="manual-conformance-mixed",
    )
    assert submitted["success"], submitted
    response = submitted["result"]["response"]
    assert response["status"] == "pending_review"
    pending_id = response["pending"]["id"]
    pending = runtime.store.get(pending_id)
    assert [event.draft for event in pending.events] == expected
    assert all(event.calendar_entity is None and event.routing_unresolved for event in pending.events)
    assert pending.warnings[:3] == (
        "source_partial_parse: One candidate was rejected.",
        "event 0: provider_low_confidence: One source detail was ambiguous.",
        "candidate 1: invalid_time_range: End time was not after start time.",
    )
    assert len(runtime.wire.received) == 1
    method, parts = runtime.wire.received[0]
    assert method == "POST"
    assert [(name, content_type) for name, content_type, _ in parts] == [
        ("request", "application/json"),
    ]
    request = json.loads(parts[0][2])
    assert request["source"]["text"] == "Independent school dates and meeting details."
    assert request["source"]["attachments"] == []
    assert request["time_zone"] == hass.config.time_zone
    assert await hass.config_entries.async_reload(runtime.entry.entry_id)
    reloaded = runtime.verify()
    assert reloaded is not runtime.store
    assert reloaded.get(pending_id) == pending
    review = await call(client, "get_pending", pending_id=pending_id)
    assert review["success"], review
    detail = review["result"]["response"]["pending"]
    for event, draft in zip(detail["events"], expected, strict=True):
        assert {key: event[key] for key in draft.as_dict()} == draft.as_dict()
    assert detail["warnings"] == list(pending.warnings)
    assert "Second candidate from source" not in json.dumps(detail)
    assert len(runtime.wire.received) == 1
    runtime.writes.assert_not_awaited()
    runtime.generate.assert_not_awaited()


async def test_zero_event_http_success_is_durable_without_automatic_replay(
    hass, hass_ws_client, conformance_runtime,
):
    runtime = conformance_runtime
    runtime.wire.response = {
        "schema_version": 1, "events": [], "rejected": [], "warnings": [],
        "provider_metadata": None,
    }
    client = await hass_ws_client(hass)
    submitted = await call(
        client, "submit_text", text="A newsletter with no scheduled events.",
        source_id="manual-conformance-zero",
    )
    assert submitted["success"], submitted
    response = submitted["result"]["response"]
    assert response["status"] == "no_events" and response["pending"] is None
    activity_id = response["activity_id"]
    activity = runtime.store.get_activity(activity_id)
    assert activity["status"] == "no_events"
    assert not runtime.store.list()
    assert len(runtime.wire.received) == 1
    assert await hass.config_entries.async_reload(runtime.entry.entry_id)
    reloaded = runtime.verify()
    assert reloaded is not runtime.store
    assert reloaded.get_activity(activity_id) == activity
    assert not reloaded.list()
    read = await call(client, "get_activity", pending_id=activity_id)
    assert read["success"], read
    assert read["result"]["response"]["activity"]["status"] == "no_events"
    await hass.async_block_till_done()
    # Reload and review reads must not replay an accepted parse. Deliberate
    # later submissions retain existing semantics; M5 idempotency is separate.
    assert len(runtime.wire.received) == 1
    runtime.writes.assert_not_awaited()
    runtime.generate.assert_not_awaited()


async def test_malformed_http_success_remains_retryable_after_reload(
    hass, hass_ws_client, conformance_runtime,
):
    runtime = conformance_runtime
    runtime.wire.response["unexpected"] = "Invalid complete response, despite valid events."
    client = await hass_ws_client(hass)
    source_id = "manual-conformance-invalid"
    failed = await call(client, "submit_text", text="A school event.", source_id=source_id)
    assert not failed["success"]
    assert not runtime.store.list()
    assert not runtime.store.is_source_durable(source_id)
    failed_activity = runtime.store.list_activity()[0]
    assert failed_activity["status"] == "failed"
    assert failed_activity["failure_code"] == "hosted_parse_failed"
    assert len(runtime.wire.received) == 1
    assert await hass.config_entries.async_reload(runtime.entry.entry_id)
    reloaded = runtime.verify()
    assert reloaded.get_activity(failed_activity["id"])["status"] == "failed"
    assert not reloaded.is_source_durable(source_id)
    assert not reloaded.list()
    assert len(runtime.wire.received) == 1
    runtime.wire.response = canonical_result("remote-meeting")
    recovered = await call(client, "submit_text", text="A school event.", source_id=source_id)
    assert recovered["success"], recovered
    response = recovered["result"]["response"]
    assert response["status"] == "pending_review"
    assert response["activity_id"] != failed_activity["id"]
    assert reloaded.is_source_durable(source_id)
    assert len(reloaded.list()) == 1
    expected = EventDraft.from_mapping(runtime.wire.response["events"][0]["draft"])
    assert reloaded.get(response["pending"]["id"]).events[0].draft == expected
    assert len(runtime.wire.received) == 2
    runtime.writes.assert_not_awaited()
    runtime.generate.assert_not_awaited()
