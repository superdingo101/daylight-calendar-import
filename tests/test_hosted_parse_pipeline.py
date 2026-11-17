"""Hosted selection reaches real local review without a local AI Task."""

import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from aiohttp import FormData
from homeassistant.core import Context
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daylight_calendar_import import _hosted_parse_failure
from custom_components.daylight_calendar_import.const import (
    CONF_PARSER_PROVIDER,
    DOMAIN,
    PARSER_PROVIDER_DAYLIGHT_CLOUD,
)
from custom_components.daylight_calendar_import.hosted_credentials import (
    BoundCredential,
    CredentialCheckpoint,
    HostedCredentialStorageError,
)
from custom_components.daylight_calendar_import.hosted_lifecycle import (
    HostedConnectionResult,
)
from custom_components.daylight_calendar_import.hosted_parse_transport import (
    HostedParseError,
    HostedParseTransport,
)
from custom_components.daylight_calendar_import.hosted_runtime import (
    CONF_HOSTED_SERVICE,
    HostedConnectionRuntime,
    async_setup_hosted_runtime,
)
from custom_components.daylight_calendar_import.hosted_transport import (
    HostedServiceBinding,
)
from custom_components.daylight_calendar_import.models import EventDraft
from custom_components.daylight_calendar_import.parser import ParseOutcome
from custom_components.daylight_calendar_import.providers import ProviderError
from custom_components.daylight_calendar_import.settings_api import (
    WS_UPDATE_CORE_SETTINGS,
)
from custom_components.daylight_calendar_import.source_routing import (
    plan_source_routing,
)
from custom_components.daylight_calendar_import.sources import TextSourceAdapter
from custom_components.daylight_calendar_import.storage import PendingImportStore

REAL_PARSE_SESSION = HostedConnectionRuntime.parse_session

PNG = __import__("base64").b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII="
)
SERVICE = HostedServiceBinding(
    "https://api.example.test", "https://account.example.test"
)
DRAFT = EventDraft(
    "School concert",
    "2026-11-12T19:00:00-08:00",
    "2026-11-12T20:00:00-08:00",
    False,
    "School gym",
    "Bring tickets. https://meet.example.test/join?a=1",
    0.95,
)


async def call(client, service, **data):
    await client.send_json_auto_id(
        {
            "type": "call_service",
            "domain": DOMAIN,
            "service": service,
            "service_data": data,
            "return_response": True,
        }
    )
    return await client.receive_json()


@pytest.fixture
async def hosted_review(hass, monkeypatch, tmp_path):
    state = {"active": True}
    provider = SimpleNamespace(
        async_parse=AsyncMock(return_value=ParseOutcome([DRAFT], []))
    )

    def guard():
        if not state["active"]:
            raise ProviderError(
                "hosted_connection_changed", "Daylight Cloud connection changed."
            )

    factory = Mock(return_value=(provider, guard))
    monkeypatch.setattr(HostedConnectionRuntime, "parse_session", factory)
    generate = AsyncMock(
        side_effect=AssertionError("Cloud must never invoke retained BYO")
    )
    monkeypatch.setattr(
        "homeassistant.components.ai_task.async_generate_data", generate
    )
    monkeypatch.setattr(
        "homeassistant.components.file_upload.TEMP_DIR_NAME", str(tmp_path / "uploads")
    )
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
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return SimpleNamespace(
        entry=entry,
        store=hass.data[DOMAIN][entry.entry_id],
        state=state,
        provider=provider,
        factory=factory,
        generate=generate,
        writes=writes,
    )


@pytest.mark.parametrize("kind", ["text", "image", "text_pdf", "scanned_pdf"])
async def test_hosted_intake_without_ai_or_calendar_reaches_durable_unresolved_review(
    hass,
    hass_ws_client,
    hass_client,
    hosted_review,
    tmp_path,
    monkeypatch,
    kind,
):
    runtime = hosted_review
    # Exercise the actual runtime factory and hosted adapter. Only the external
    # HTTP boundary returns a deterministic canonical ParseResult; M2's distinct
    # suites prove remote credential verification, not this fixture priming.
    monkeypatch.setattr(HostedConnectionRuntime, "parse_session", REAL_PARSE_SESSION)
    connection = runtime.store.hosted_runtime
    credential = BoundCredential(
        "account-identifier-1234",
        "installation-identifier-1234",
        "x" * 60,
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    checkpoint = CredentialCheckpoint(
        processing_consent=True, current=credential, service_binding=SERVICE
    )
    connection._service = SERVICE
    connection._credential_store = SimpleNamespace(snapshot=checkpoint)
    connection._verified_parse_credential = credential
    connection._state = HostedConnectionResult("connected", "current")
    wire = AsyncMock(
        return_value={
            "schema_version": 1,
            "events": [{"draft": DRAFT.as_dict(), "warnings": []}],
            "rejected": [],
            "warnings": [],
            "provider_metadata": None,
        }
    )
    monkeypatch.setattr(HostedParseTransport, "async_parse", wire)
    client = await hass_ws_client(hass)
    data = {
        "text": "School concert November 12, 2026 7–8 PM",
        "source_id": "manual-hosted-" + kind,
    }
    service = "submit_text"
    if kind != "text":
        payload = PNG
        service = "submit_image"
        if kind.endswith("pdf"):
            service = "submit_pdf"
            writer = PdfWriter()
            page = writer.add_blank_page(width=200, height=200)
            stream = DecodedStreamObject()
            if kind == "text_pdf":
                font = writer._add_object(
                    DictionaryObject(
                        {
                            NameObject("/Type"): NameObject("/Font"),
                            NameObject("/Subtype"): NameObject("/Type1"),
                            NameObject("/BaseFont"): NameObject("/Helvetica"),
                        }
                    )
                )
                page[NameObject("/Resources")] = DictionaryObject(
                    {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})}
                )
                stream.set_data(b"BT /F1 12 Tf 10 10 Td (School concert) Tj ET")
            else:
                stream.set_data(b"0 0 100 100 re f")
            page[NameObject("/Contents")] = writer._add_object(stream)
            output = BytesIO()
            writer.write(output)
            payload = output.getvalue()
        http = await hass_client()
        multipart = FormData()
        multipart.add_field(
            "file",
            payload,
            filename="concert.pdf" if service == "submit_pdf" else "concert.png",
            content_type="application/pdf" if service == "submit_pdf" else "image/png",
        )
        uploaded = await http.post("/api/file_upload", data=multipart)
        assert uploaded.status == 200
        data["file_id"] = (await uploaded.json())["file_id"]
        hass.config.media_dirs = {"local": str(tmp_path / "media")}
    result = await call(client, service, **data)
    assert result["success"], result
    response = result["result"]["response"]
    assert response["status"] == "pending_review"
    event = runtime.store.get(response["pending"]["id"]).events[0]
    assert event.calendar_entity is None and event.routing_unresolved
    assert event.draft == DRAFT
    assert "Choose a writable calendar" in response["warnings"][-1]
    wire.assert_awaited_once()
    parsed = wire.await_args
    request, attachments = parsed.args
    assert parsed.kwargs["token"] == credential.token
    assert request["time_zone"] == hass.config.time_zone
    assert datetime.fromisoformat(request["reference_datetime"]).utcoffset() is not None
    assert request["source"]["metadata"] == {}
    assert "content_ref" not in str(request)
    assert request["source"]["kind"] == (
        "manual_text" if kind == "text" else "image" if kind == "image" else "pdf"
    )
    if kind in ("image", "scanned_pdf"):
        assert attachments[0][2] == payload
        assert request["source"]["attachments"][0]["size_bytes"] == len(payload)
    else:
        assert attachments == ()
    runtime.provider.async_parse.assert_not_awaited()
    runtime.writes.assert_not_awaited()
    runtime.generate.assert_not_awaited()
    # Reload uses the actual saved pending snapshot, not a reconstructed draft.
    assert await hass.config_entries.async_reload(runtime.entry.entry_id)
    assert (
        hass.data[DOMAIN][runtime.entry.entry_id]
        .get(response["pending"]["id"])
        .events[0]
        == event
    )
    if kind != "text":
        assert not list((tmp_path / "media").glob("**/*"))
        assert not hass.data["file_upload"].files


async def test_first_calendar_can_be_configured_without_resolving_or_writing_drafts(
    hass,
    hass_ws_client,
    hosted_review,
):
    runtime = hosted_review
    client = await hass_ws_client(hass)
    result = await call(
        client, "submit_text", text="School concert", source_id="manual-first-calendar"
    )
    assert result["success"], result
    pending = result["result"]["response"]["pending"]
    event_id = pending["events"][0]["id"]
    denied = await call(
        client, "approve_pending_event", pending_id=pending["id"], event_id=event_id
    )
    assert not denied["success"]
    await client.send_json_auto_id(
        {
            "type": WS_UPDATE_CORE_SETTINGS,
            "entry_id": runtime.entry.entry_id,
            "calendar_entity": "calendar.family",
            "calendar_entities": ["calendar.family"],
        }
    )
    settings = await client.receive_json()
    assert settings["success"], settings
    store = hass.data[DOMAIN][runtime.entry.entry_id]
    event = store.get(pending["id"]).events[0]
    assert event.calendar_entity is None and event.routing_unresolved
    denied = await call(
        client, "approve_pending_event", pending_id=pending["id"], event_id=event_id
    )
    assert not denied["success"]
    runtime.writes.assert_not_awaited()
    edited = await call(
        client,
        "edit_pending_event",
        pending_id=pending["id"],
        event_id=event_id,
        event=DRAFT.as_dict(),
        calendar_entity="calendar.family",
    )
    assert edited["success"], edited
    accepted = await call(
        client, "approve_pending_event", pending_id=pending["id"], event_id=event_id
    )
    assert accepted["success"], accepted
    runtime.writes.assert_awaited_once()


@pytest.mark.parametrize(
    "error,code",
    [
        (HostedParseError("unauthenticated", status=401), "hosted_authentication"),
        (HostedParseError("forbidden", status=403), "hosted_access"),
        (
            HostedParseError("provider_rate_limited", status=429, retryable=True),
            "hosted_rate_limited",
        ),
        (
            HostedParseError("provider_timeout", uncertain=True),
            "hosted_outcome_uncertain",
        ),
        (
            HostedParseError("provider_invalid_response", status=502),
            "hosted_parse_failed",
        ),
    ],
)
async def test_hosted_errors_are_safe_actionable_and_never_replayed(
    hass, hass_ws_client, hosted_review, error, code
):
    runtime = hosted_review
    client = await hass_ws_client(hass)
    runtime.provider.async_parse.side_effect = error
    result = await call(
        client, "submit_text", text="School concert", source_id="manual-error"
    )
    assert not result["success"]
    assert "Daylight Cloud" in result["error"]["message"]
    assert len(runtime.store.list()) == 0
    assert runtime.store.list_activity()[0]["failure_code"] == code
    runtime.provider.async_parse.assert_awaited_once()
    runtime.generate.assert_not_awaited()
    assert _hosted_parse_failure(error).code == code


async def test_guarded_add_rejects_disconnect_while_waiting_real_store_lock(hass):
    store = PendingImportStore(hass)
    await store.async_load()
    valid = True

    def guard():
        if not valid:
            raise ProviderError(
                "hosted_connection_changed", "Daylight Cloud connection changed."
            )

    async with store._lock:
        task = asyncio.create_task(
            store.async_add(
                source_text="School concert", events=[DRAFT], acceptance_guard=guard
            )
        )
        await asyncio.sleep(0)
        valid = False
    with pytest.raises(ProviderError):
        await task
    assert not store.list()
    reloaded = PendingImportStore(hass)
    await reloaded.async_load()
    assert not reloaded.list()


async def test_guarded_reprocess_rejects_late_result_without_changing_original(hass):
    store = PendingImportStore(hass)
    await store.async_load()
    original = (
        await store.async_add(source_text="School concert", events=[DRAFT])
    ).pending
    started = asyncio.Event()
    release = asyncio.Event()
    valid = True

    async def parser(_):
        started.set()
        await release.wait()
        return (), ()

    def guard():
        if not valid:
            raise ProviderError(
                "hosted_connection_changed", "Daylight Cloud connection changed."
            )

    task = asyncio.create_task(
        store.async_reprocess(original.id, parser, acceptance_guard=guard)
    )
    await started.wait()
    valid = False
    release.set()
    with pytest.raises(ProviderError):
        await task
    assert store.get(original.id) is original


def test_unconfigured_destination_remains_explicitly_unresolved():
    result = plan_source_routing(
        TextSourceAdapter().create("Concert"),
        default_calendar=None,
        allowed_calendars=set(),
        aliases={},
    )
    assert result.calendar_entity is None and result.requires_confirmation
    assert "Choose a writable calendar" in result.warnings[0]
    with pytest.raises(ValueError):
        plan_source_routing(
            TextSourceAdapter().create("Concert"),
            default_calendar=None,
            allowed_calendars={"calendar.family"},
            aliases={},
        )


async def verified_runtime(hass, monkeypatch):
    credential = BoundCredential(
        "account-identifier-1234",
        "installation-identifier-1234",
        "x" * 60,
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    checkpoint = CredentialCheckpoint(
        processing_consent=True, current=credential, service_binding=SERVICE
    )
    store = SimpleNamespace(
        snapshot=checkpoint, async_load=AsyncMock(return_value=checkpoint)
    )
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.hosted_runtime.HostedCredentialStore",
        lambda *_: store,
    )
    entry = SimpleNamespace(
        entry_id="private-parser",
        options={
            CONF_HOSTED_SERVICE: {
                "api_origin": SERVICE.api_origin,
                "account_origin": SERVICE.account_origin,
            }
        },
    )
    runtime = await async_setup_hosted_runtime(hass, entry)
    runtime._state = HostedConnectionResult("connected", "current")
    runtime._verified_parse_credential = checkpoint.current
    runtime.resume()
    return runtime, store


async def test_runtime_parser_session_is_private_and_permanently_generation_fenced(
    hass, monkeypatch
):
    runtime, store = await verified_runtime(hass, monkeypatch)
    provider, guard = runtime.parse_session()
    guard()
    assert provider.capabilities.text
    assert store.snapshot.current.token not in str(runtime.snapshot())
    runtime.pause()
    runtime.resume()
    with pytest.raises(ProviderError):
        guard()
    runtime.parse_session()[1]()
    runtime._perform = AsyncMock(return_value=runtime.snapshot())
    task = runtime._start_work("reconcile", {})
    with pytest.raises(ProviderError):
        runtime.parse_session()
    await task
    await asyncio.sleep(0)
    runtime.parse_session()[1]()
    await runtime.async_stop()


@pytest.mark.parametrize(
    "change",
    [
        "unverified",
        "storage",
        "consent",
        "disconnect",
        "changed",
        "expired",
        "service",
        "no_store",
    ],
)
async def test_runtime_parser_session_fails_closed(hass, monkeypatch, change):
    runtime, store = await verified_runtime(hass, monkeypatch)
    _, guard = runtime.parse_session()
    if change == "unverified":
        runtime._state = HostedConnectionResult("recovery_required")
    elif change == "storage":

        class BrokenStore:
            @property
            def snapshot(self):
                raise HostedCredentialStorageError("private")

        runtime._credential_store = BrokenStore()
    elif change == "consent":
        store.snapshot = CredentialCheckpoint(service_binding=SERVICE)
    elif change == "disconnect":
        store.snapshot = replace(store.snapshot, local_disconnect="unconfirmed")
    elif change == "changed":
        store.snapshot = replace(
            store.snapshot, current=replace(store.snapshot.current, token="y" * 60)
        )
    elif change == "expired":
        store.snapshot = replace(
            store.snapshot,
            current=replace(
                store.snapshot.current, expires_at=datetime.now(UTC).isoformat()
            ),
        )
    elif change == "service":
        runtime._service = None
    else:
        runtime._credential_store = None
    with pytest.raises(ProviderError):
        guard()
    with pytest.raises(ProviderError):
        runtime.parse_session()
    await runtime.async_stop()


async def test_cloud_parse_service_and_explicit_legacy_import_keep_local_write_authority(
    hass,
    hass_ws_client,
    hosted_review,
):
    runtime = hosted_review
    client = await hass_ws_client(hass)
    parsed = await call(client, "parse_text", text="School concert")
    assert parsed["success"], parsed
    assert parsed["result"]["response"]["events"] == [DRAFT.as_dict()]
    runtime.writes.assert_not_awaited()
    await client.send_json_auto_id(
        {
            "type": WS_UPDATE_CORE_SETTINGS,
            "entry_id": runtime.entry.entry_id,
            "calendar_entity": "calendar.family",
            "calendar_entities": ["calendar.family"],
        }
    )
    assert (await client.receive_json())["success"]
    imported = await call(client, "import_text", text="School concert")
    assert imported["success"], imported
    assert imported["result"]["response"]["imported"] == 1
    runtime.writes.assert_awaited_once()
    assert not hass.data[DOMAIN][runtime.entry.entry_id].list()


@pytest.mark.parametrize(
    "change",
    [
        "missing_runtime",
        "stopped_entry",
        "replaced_store",
        "changed_provider",
        "ordinary_error",
    ],
)
async def test_selected_provider_rejects_stale_entry_or_unknown_provider_failure(
    hass,
    hass_ws_client,
    hosted_review,
    change,
):
    runtime = hosted_review
    client = await hass_ws_client(hass)
    if change == "missing_runtime":
        runtime.store.hosted_runtime = None
    else:

        async def parse(*_args, **_kwargs):
            if change == "stopped_entry":
                runtime.store.accepting_services = False
            elif change == "replaced_store":
                hass.data[DOMAIN][runtime.entry.entry_id] = object()
            elif change == "changed_provider":
                hass.config_entries.async_update_entry(
                    runtime.entry, options={CONF_PARSER_PROVIDER: "ai_task"}
                )
            else:
                raise ProviderError("provider_timeout", "Fixed local provider error")
            return ParseOutcome([DRAFT], [])

        runtime.provider.async_parse.side_effect = parse
    try:
        result = await call(client, "submit_text", text="School concert")
        assert not result["success"]
        assert not runtime.store.list()
        runtime.writes.assert_not_awaited()
    finally:
        hass.data[DOMAIN][runtime.entry.entry_id] = runtime.store
        runtime.store.accepting_services = True


@pytest.mark.parametrize("record_fails", [False, True])
async def test_pipeline_storage_guard_rejects_late_acceptance_and_records_safe_failure(
    hass,
    hass_ws_client,
    hosted_review,
    monkeypatch,
    record_fails,
):
    runtime = hosted_review
    client = await hass_ws_client(hass)
    ready = asyncio.Event()

    async def parse(*_args, **_kwargs):
        await runtime.store._lock.acquire()
        ready.set()
        return ParseOutcome([DRAFT], [])

    runtime.provider.async_parse.side_effect = parse
    if record_fails:
        monkeypatch.setattr(
            runtime.store,
            "async_record_parse_failure",
            AsyncMock(side_effect=RuntimeError("disk unavailable")),
        )
    request = asyncio.create_task(call(client, "submit_text", text="School concert"))
    await ready.wait()
    async with asyncio.timeout(2):
        while not runtime.store._lock._waiters:
            await asyncio.sleep(0)
    runtime.state["active"] = False
    runtime.store._lock.release()
    result = await request
    assert not result["success"]
    assert "Daylight Cloud connection changed" in result["error"]["message"]
    assert not runtime.store.list()
    if not record_fails:
        assert (
            runtime.store.list_activity()[0]["failure_code"]
            == "hosted_connection_changed"
        )


async def test_cancellation_while_waiting_acceptance_never_commits_pending(
    hass,
    hass_admin_user,
    hosted_review,
):
    runtime = hosted_review
    ready = asyncio.Event()
    handler_task = None

    async def parse(*_args, **_kwargs):
        nonlocal handler_task
        handler_task = asyncio.current_task()
        await runtime.store._lock.acquire()
        ready.set()
        return ParseOutcome([DRAFT], [])

    runtime.provider.async_parse.side_effect = parse
    request = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            "submit_text",
            {"text": "School concert"},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_admin_user.id),
        )
    )
    await ready.wait()
    async with asyncio.timeout(2):
        while not runtime.store._lock._waiters:
            await asyncio.sleep(0)
    runtime.state["active"] = False
    handler_task.cancel()
    runtime.store._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert not runtime.store.list()


async def test_missing_explicit_provider_does_not_infer_a_byo_fallback(hass):
    from custom_components.daylight_calendar_import import _async_parse_source

    with pytest.raises(ProviderError, match="needs verification"):
        await _async_parse_source(
            hass, TextSourceAdapter().create("School concert"), None
        )


async def test_cloud_selection_pauses_imap_network_and_preserves_mailbox_on_byo_resume(
    hass,
    hass_ws_client,
    hosted_review,
    monkeypatch,
):
    from custom_components.daylight_calendar_import import email_runtime

    runtime = hosted_review
    options = {
        "email_enabled": True,
        "email_host": "imap.example.test",
        "email_username": "mailbox",
        "email_password": "private-local-mailbox",
        "email_mailbox": "Calendar",
        "email_port": 993,
        "email_verify_ssl": True,
    }
    source_factory = Mock(return_value=object())
    polling = SimpleNamespace(
        async_start=AsyncMock(), on_entry_ready=Mock(), async_stop=AsyncMock()
    )
    runtime_factory = Mock(return_value=polling)
    monkeypatch.setattr(email_runtime, "DirectImapSource", source_factory)
    monkeypatch.setattr(email_runtime, "EmailPollingRuntime", runtime_factory)
    hass.config_entries.async_update_entry(runtime.entry, options=options)
    assert await hass.config_entries.async_reload(runtime.entry.entry_id)
    assert hass.data[DOMAIN][runtime.entry.entry_id].email_runtime is None
    assert dict(runtime.entry.options) == options
    source_factory.assert_not_called()
    runtime.provider.async_parse.assert_not_awaited()
    client = await hass_ws_client(hass)
    manual = await call(client, "submit_text", text="School concert")
    assert manual["success"], manual
    # Reject the unassigned draft before deliberately switching to BYO.
    rejected = await call(
        client,
        "reject_pending",
        pending_id=manual["result"]["response"]["pending"]["id"],
    )
    assert rejected["success"], rejected
    await client.send_json_auto_id(
        {
            "type": WS_UPDATE_CORE_SETTINGS,
            "entry_id": runtime.entry.entry_id,
            "parser_provider": "ai_task",
            "ai_task_entity": "ai_task.local",
            "calendar_entity": "calendar.family",
            "calendar_entities": ["calendar.family"],
        }
    )
    switched = await client.receive_json()
    assert switched["success"], switched
    source_factory.assert_called_once()
    runtime_factory.assert_called_once()
    polling.async_start.assert_awaited_once()
    assert all(runtime.entry.options[key] == value for key, value in options.items())


async def test_accidental_email_processor_callback_cannot_start_cloud_parsing(
    hass,
    hosted_review,
    monkeypatch,
):
    processors = []

    async def capture(_hass, _entry, _store, processor):
        processors.append(processor)
        return None

    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.async_setup_email_runtime", capture
    )
    assert await hass.config_entries.async_reload(hosted_review.entry.entry_id)
    with pytest.raises(ProviderError, match="Direct IMAP is paused"):
        await processors[0](TextSourceAdapter().create("School concert"), "unused")
    hosted_review.provider.async_parse.assert_not_awaited()


async def test_runtime_parse_permission_requires_successful_current_verification(
    hass, monkeypatch
):
    runtime, store = await verified_runtime(hass, monkeypatch)
    runtime._verified_parse_credential = None
    with pytest.raises(ProviderError):
        runtime.parse_session()
    runtime._controller = SimpleNamespace(
        reconnect_in_progress=False,
        async_reconcile=AsyncMock(
            return_value=HostedConnectionResult(
                "connected", "current", store.snapshot.current.expires_at
            )
        ),
    )
    await runtime.async_action("reconcile")
    await asyncio.sleep(0)
    runtime.parse_session()[1]()
    await runtime.async_stop()


async def test_hosted_reprocess_retains_existing_drafts_and_original_reference_context(
    hass,
    hass_ws_client,
    hosted_review,
):
    runtime = hosted_review
    client = await hass_ws_client(hass)
    response = await call(client, "submit_text", text="School concert")
    original = runtime.store.get(response["result"]["response"]["pending"]["id"])
    runtime.provider.async_parse.return_value = ParseOutcome(
        [replace(DRAFT, title="Second concert")], []
    )
    response = await call(client, "reprocess_pending", pending_id=original.id)
    assert response["success"], response
    updated = runtime.store.get(original.id)
    assert updated.events[0] == original.events[0]
    assert updated.events[1].draft.title == "Second concert"
    assert datetime.fromisoformat(
        runtime.provider.async_parse.await_args.kwargs["reference_datetime"]
    ) == datetime.fromisoformat(original.created_at)
    runtime.writes.assert_not_awaited()


async def test_hosted_parse_session_accepts_overlap_and_fences_exact_generations(hass, monkeypatch):
    """Only the same generation can accept results; lifecycle work never inherits one."""
    runtime, _ = await verified_runtime(hass, monkeypatch)
    assert runtime._parse_generation == 0
    runtime._state = HostedConnectionResult("connected", "overlap")
    _, first_guard = runtime.parse_session()
    first_guard()
    runtime.pause()
    assert runtime._parse_generation == 1
    runtime.resume()
    with pytest.raises(ProviderError) as expired:
        first_guard()
    assert expired.value.code == "hosted_connection_changed"
    assert str(expired.value) == (
        "Daylight Cloud connection changed or needs verification. "
        "Open Daylight Cloud settings before retrying."
    )
    _, second_guard = runtime.parse_session()
    second_guard()

    created = []
    real_create_task = hass.async_create_task

    def recording_create_task(*args, **kwargs):
        created.append(kwargs.copy())
        return real_create_task(*args, **kwargs)

    monkeypatch.setattr(hass, "async_create_task", recording_create_task)
    runtime._perform = AsyncMock(return_value=runtime.snapshot())
    work = runtime._start_work("reconcile", {})
    assert runtime._parse_generation == 2
    assert created and created[-1]["eager_start"] is False
    with pytest.raises(ProviderError) as invalidated:
        second_guard()
    assert invalidated.value.code == "hosted_connection_changed"
    await work
    await asyncio.sleep(0)
    await runtime.async_stop()


@pytest.mark.parametrize("changed", ["missing", "no_current", "foreign_service"])
async def test_hosted_parse_guard_detects_checkpoint_identity_changes(hass, monkeypatch, changed):
    """Do not let a modified credential checkpoint authorize persistence."""
    runtime, store = await verified_runtime(hass, monkeypatch)
    _, guard = runtime.parse_session()
    if changed == "missing":
        store.snapshot = None
    elif changed == "no_current":
        store.snapshot = replace(store.snapshot, current=None)
    else:
        store.snapshot = replace(
            store.snapshot,
            service_binding=HostedServiceBinding(
                "https://another-api.example.test", SERVICE.account_origin,
            ),
        )
    with pytest.raises(ProviderError) as error:
        guard()
    assert error.value.code == "hosted_connection_changed"
    with pytest.raises(ProviderError) as error:
        runtime.parse_session()
    assert error.value.code == "hosted_connection_changed"
    await runtime.async_stop()


@pytest.mark.parametrize("status,expected_verified", [
    ("overlap", True),
    ("disconnected", False),
])
async def test_hosted_lifecycle_result_sets_only_verified_current_credentials(
    hass, monkeypatch, status, expected_verified,
):
    """Overlap retains legitimate authority; disconnection clears it."""
    runtime, store = await verified_runtime(hass, monkeypatch)
    runtime._controller = SimpleNamespace(
        reconnect_in_progress=False,
        async_reconcile=AsyncMock(return_value=HostedConnectionResult(
            "connected" if expected_verified else "disconnected", status,
        )),
    )
    result = await runtime.async_action("reconcile")
    await asyncio.sleep(0)  # Let the completed lifecycle task release runtime ownership.
    assert result["credential_status"] == status
    assert (runtime._verified_parse_credential == store.snapshot.current) is expected_verified
    if expected_verified:
        runtime.parse_session()[1]()
    else:
        with pytest.raises(ProviderError):
            runtime.parse_session()
    await runtime.async_stop()


async def test_real_hosted_guard_blocks_cancelled_submit_after_storage_lock(
    hass, hass_admin_user, hosted_review, monkeypatch,
):
    """Cancelling the handler alone must veto admission, without disconnecting."""
    from custom_components.daylight_calendar_import.hosted_parser import DaylightHostedParserProvider

    review = hosted_review
    connection = review.store.hosted_runtime
    credential = BoundCredential(
        "account-identifier-1234",
        "installation-identifier-1234",
        "x" * 60,
        (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    )
    checkpoint = CredentialCheckpoint(
        processing_consent=True, current=credential, service_binding=SERVICE,
    )
    connection._service = SERVICE
    connection._credential_store = SimpleNamespace(snapshot=checkpoint)
    connection._verified_parse_credential = credential
    connection._state = HostedConnectionResult("connected", "current")
    monkeypatch.setattr(HostedConnectionRuntime, "parse_session", REAL_PARSE_SESSION)

    ready = asyncio.Event()
    handler = None

    async def parse(*_args, **_kwargs):
        nonlocal handler
        handler = asyncio.current_task()
        await review.store._lock.acquire()
        ready.set()
        return ParseOutcome([DRAFT], [])

    monkeypatch.setattr(DaylightHostedParserProvider, "async_parse", parse)
    request = asyncio.create_task(
        hass.services.async_call(
            DOMAIN,
            "submit_text",
            {"text": "School concert"},
            blocking=True,
            return_response=True,
            context=Context(user_id=hass_admin_user.id),
        )
    )
    await ready.wait()
    async with asyncio.timeout(3):
        while not review.store._lock._waiters:
            await asyncio.sleep(0)
    assert review.state["active"] is True
    assert connection._state.credential_status == "current"
    handler.cancel()
    review.store._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert review.state["active"] is True
    assert connection._state.credential_status == "current"
    assert not review.store.list()


async def test_real_hosted_guard_blocks_cancelled_reprocess_before_acceptance(hass, monkeypatch):
    """Cancelled retained-text reprocessing cannot append anything after its lock wait."""
    runtime, _ = await verified_runtime(hass, monkeypatch)
    store = PendingImportStore(hass)
    await store.async_load()
    original = (await store.async_add(
        source_text="School concert", events=[DRAFT],
    )).pending

    ready = asyncio.Event()

    async def parser(_pending):
        await store._lock.acquire()
        ready.set()
        return (), ("This warning must never commit",)

    async def reprocess():
        _, guard = runtime.parse_session()
        return await store.async_reprocess(
            original.id, parser, acceptance_guard=guard,
        )

    request = asyncio.create_task(reprocess())
    await ready.wait()
    async with asyncio.timeout(3):
        while not store._lock._waiters:
            await asyncio.sleep(0)
    request.cancel()
    await asyncio.sleep(0)
    store._lock.release()
    with pytest.raises(asyncio.CancelledError):
        await request
    assert store.get(original.id) is original
    assert store.get(original.id).warnings == original.warnings
    await runtime.async_stop()


async def test_delivery_pass_recovers_before_claim_and_owns_parser(hass, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_ack_recovery, hosted_text_batch

    runtime, _ = await verified_runtime(hass, monkeypatch)
    order = []
    store = object()

    async def recover(**kwargs):
        order.append("recover")
        assert kwargs["store"] is store
        runtime.parse_session(delivery_owner=True)[1]()
        with pytest.raises(ProviderError):
            runtime.parse_session()

    async def batch(**kwargs):
        order.append("claim")
        result, guard = await kwargs["parse_source"]("source")
        assert result == "parsed"
        guard()
        return ["done"]

    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", recover)
    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", batch)
    parse = AsyncMock(return_value=("parsed", lambda: order.append("guard")))
    runtime.configure_delivery(store=store, route_source=object(), parse_source=parse)
    assert order == []
    timer_cancel = Mock()
    runtime._cancel_timer = timer_cancel
    assert await runtime.async_delivery_pass() == ["done"]
    assert order == ["recover", "claim", "guard"]
    timer_cancel.assert_called_once()
    await runtime.async_stop()


async def test_delivery_pass_stops_on_recovery_failure(hass, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_ack_recovery, hosted_text_batch

    runtime, _ = await verified_runtime(hass, monkeypatch)
    recovery = AsyncMock(side_effect=RuntimeError("uncertain"))
    batch = AsyncMock()
    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", recovery)
    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", batch)
    runtime.configure_delivery(store=object(), route_source=object(), parse_source=object())
    with pytest.raises(RuntimeError, match="uncertain"):
        await runtime.async_delivery_pass()
    batch.assert_not_awaited()
    await runtime.async_stop()


async def test_delivery_admission_and_configuration(hass, monkeypatch):
    from custom_components.daylight_calendar_import.hosted_runtime import HostedRuntimeError

    runtime, _ = await verified_runtime(hass, monkeypatch)
    with pytest.raises(HostedRuntimeError, match="hosted_unavailable"):
        await runtime.async_delivery_pass()
    runtime.configure_delivery(store=object(), route_source=object(), parse_source=object())
    with pytest.raises(HostedRuntimeError, match="hosted_already_configured"):
        runtime.configure_delivery(store=object(), route_source=object(), parse_source=object())
    runtime._verified_parse_credential = None
    with pytest.raises(HostedRuntimeError, match="hosted_unavailable"):
        await runtime.async_delivery_pass()
    runtime.pause()
    with pytest.raises(HostedRuntimeError, match="hosted_unavailable"):
        await runtime.async_delivery_pass()
    await runtime.async_stop()


async def test_delivery_cancellation_keeps_owner_until_task_finishes(hass, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_ack_recovery
    from custom_components.daylight_calendar_import.hosted_runtime import HostedRuntimeError

    runtime, _ = await verified_runtime(hass, monkeypatch)
    started = asyncio.Event()
    release = asyncio.Event()

    async def recovery(**kwargs):
        started.set()
        try:
            await release.wait()
        except asyncio.CancelledError:
            await release.wait()
            raise

    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", recovery)
    runtime.configure_delivery(store=object(), route_source=object(), parse_source=object())
    waiter = asyncio.create_task(runtime.async_delivery_pass())
    await started.wait()
    with pytest.raises(ProviderError):
        runtime.parse_session(delivery_owner=True)
    with pytest.raises(HostedRuntimeError, match="hosted_unavailable"):
        await runtime.async_delivery_pass()
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert runtime.busy
    release.set()
    await asyncio.sleep(0)
    await runtime.async_stop()


async def test_delivery_guard_blocks_acceptance_after_pause(hass, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_ack_recovery, hosted_text_batch
    from custom_components.daylight_calendar_import.hosted_runtime import HostedRuntimeError

    runtime, _ = await verified_runtime(hass, monkeypatch)
    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", AsyncMock())

    async def batch(**kwargs):
        _, guard = await kwargs["parse_source"]("source")
        guard()  # No provider guard is valid for a BYO parser.
        runtime.pause()
        guard()

    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", batch)
    runtime.configure_delivery(
        store=object(), route_source=object(), parse_source=AsyncMock(return_value=(None, None)),
    )
    with pytest.raises(HostedRuntimeError, match="hosted_unavailable"):
        await runtime.async_delivery_pass()
    await runtime.async_stop()


async def test_delivery_pass_runs_real_acceptance_and_ack(hass, hosted_review, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_runtime, hosted_source_transport
    from tests.test_hosted_ack_dispatch import Transport, checkpoint
    from tests.test_hosted_text_acceptance import page

    review = hosted_review
    runtime = review.store.hosted_runtime
    credential = checkpoint()
    runtime._service = SERVICE
    runtime._credential_store = SimpleNamespace(snapshot=credential)
    runtime._controller = object()
    runtime._verified_parse_credential = credential.current
    runtime._state = HostedConnectionResult("connected", "current")
    monkeypatch.setattr(HostedConnectionRuntime, "parse_session", REAL_PARSE_SESSION)
    wire = AsyncMock(return_value={
        "schema_version": 1,
        "events": [{"draft": DRAFT.as_dict(), "warnings": []}],
        "rejected": [], "warnings": [], "provider_metadata": None,
    })
    monkeypatch.setattr(HostedParseTransport, "async_parse", wire)
    ack = Transport()
    claim = SimpleNamespace(service_binding=SERVICE, async_claim=AsyncMock(return_value=page()))
    monkeypatch.setattr(hosted_source_transport, "HostedSourceTransport", lambda *_: claim)
    monkeypatch.setattr(hosted_runtime, "HostedLifecycleTransport", lambda *_, **__: ack)
    results = await runtime.async_delivery_pass()
    assert len(results) == 1
    assert wire.await_count == 1
    assert len(ack.calls) == 1
    assert review.store.hosted_ack_journal()[0].ack_status == "confirmed"
    await runtime.async_stop()


async def test_delivery_pass_propagates_exact_owned_dependencies_and_limit(hass, monkeypatch):
    """ACK recovery and claims share the same credential, transport, entry and bounds."""
    from custom_components.daylight_calendar_import import (
        hosted_ack_recovery, hosted_runtime, hosted_source_transport, hosted_text_batch,
    )

    runtime, credentials = await verified_runtime(hass, monkeypatch)
    store = object()
    route = object()
    parse = AsyncMock()
    session = object()
    claim = object()
    ack = object()
    order = []

    async def recover(**kwargs):
        order.append("recover")
        assert kwargs == {
            "store": store, "credentials": credentials, "transport": ack,
            "local_config_entry_id": runtime._entry_id, "limit": 7,
        }

    async def batch(**kwargs):
        order.append("claim")
        assert kwargs["store"] is store
        assert kwargs["credentials"] is credentials
        assert kwargs["claim_transport"] is claim
        assert kwargs["ack_transport"] is ack
        assert kwargs["local_config_entry_id"] == runtime._entry_id
        assert kwargs["route_source"] is route
        assert callable(kwargs["parse_source"])
        assert kwargs["limit"] == 7
        return ["result"]

    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", recover)
    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", batch)
    monkeypatch.setattr(hosted_runtime, "async_get_clientsession", lambda _: session)
    source_factory = Mock(return_value=claim)
    ack_factory = Mock(return_value=ack)
    monkeypatch.setattr(hosted_source_transport, "HostedSourceTransport", source_factory)
    monkeypatch.setattr(hosted_runtime, "HostedLifecycleTransport", ack_factory)
    # Patch the class, not the HA instance: HomeAssistant may use slots.
    original_create = type(hass).async_create_task
    created = []

    def create_task(instance, coro, *args, **kwargs):
        if instance is hass:
            created.append((args, kwargs))
        return original_create(instance, coro, *args, **kwargs)

    monkeypatch.setattr(type(hass), "async_create_task", create_task)
    runtime.configure_delivery(store=store, route_source=route, parse_source=parse)
    generation = runtime._parse_generation
    assert await runtime.async_delivery_pass(limit=7) == ["result"]
    assert order == ["recover", "claim"]
    source_factory.assert_called_once_with(session, SERVICE)
    ack_factory.assert_called_once_with(
        session, api_origin=SERVICE.api_origin, account_origin=SERVICE.account_origin,
    )
    assert created == [(("Daylight hosted delivery",), {"eager_start": False})]
    assert runtime._parse_generation == generation + 1
    assert runtime._delivery_owner is None
    assert runtime._task.done()
    parse.assert_not_awaited()
    await runtime.async_stop()


async def test_delivery_pass_uses_default_limit_of_twenty(hass, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_ack_recovery, hosted_text_batch

    runtime, _ = await verified_runtime(hass, monkeypatch)
    recover = AsyncMock()
    batch = AsyncMock(return_value=[])
    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", recover)
    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", batch)
    runtime.configure_delivery(store=object(), route_source=object(), parse_source=AsyncMock())
    assert await runtime.async_delivery_pass() == []
    assert recover.await_args.kwargs["limit"] == 20
    assert batch.await_args.kwargs["limit"] == 20
    await runtime.async_stop()


@pytest.mark.parametrize(
    "missing",
    ["controller", "credentials", "service", "handlers", "status", "current", "unverified"],
)
async def test_delivery_pass_requires_each_verified_dependency(hass, monkeypatch, missing):
    from custom_components.daylight_calendar_import.hosted_runtime import HostedRuntimeError

    runtime, credentials = await verified_runtime(hass, monkeypatch)
    if missing != "handlers":
        runtime.configure_delivery(
            store=object(), route_source=object(), parse_source=AsyncMock(),
        )
    if missing == "controller":
        runtime._controller = None
    elif missing == "credentials":
        runtime._credential_store = None
    elif missing == "service":
        runtime._service = None
    elif missing == "status":
        runtime._state = HostedConnectionResult("connected", "not_current")
    elif missing == "current":
        credentials.snapshot = replace(credentials.snapshot, current=None)
    elif missing == "unverified":
        runtime._verified_parse_credential = None

    with pytest.raises(HostedRuntimeError) as failure:
        await runtime.async_delivery_pass()
    assert str(failure.value) == "hosted_unavailable"
    assert not runtime.busy
    await runtime.async_stop()


async def test_delivery_pass_accepts_verified_overlap_credentials(hass, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_ack_recovery, hosted_text_batch

    runtime, _ = await verified_runtime(hass, monkeypatch)
    runtime._state = HostedConnectionResult("connected", "overlap")
    recovery = AsyncMock()
    batch = AsyncMock(return_value=["ok"])
    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", recovery)
    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", batch)
    runtime.configure_delivery(store=object(), route_source=object(), parse_source=AsyncMock())
    assert await runtime.async_delivery_pass() == ["ok"]
    recovery.assert_awaited_once()
    batch.assert_awaited_once()
    await runtime.async_stop()


@pytest.mark.parametrize("invalidation", ["pause", "owner", "extra_task", "generation"])
async def test_delivery_pass_rechecks_owner_after_ack_recovery(
    hass, monkeypatch, invalidation,
):
    """Do not claim work when admission or exclusive ownership changes during I/O."""
    from custom_components.daylight_calendar_import import hosted_ack_recovery, hosted_text_batch
    from custom_components.daylight_calendar_import.hosted_runtime import HostedRuntimeError

    runtime, _ = await verified_runtime(hass, monkeypatch)
    extra_task = None

    async def recover(**kwargs):
        nonlocal extra_task
        assert runtime._delivery_owner is asyncio.current_task()
        if invalidation == "pause":
            runtime.pause()
        elif invalidation == "owner":
            runtime._delivery_owner = None
        elif invalidation == "extra_task":
            extra_task = asyncio.create_task(asyncio.sleep(60))
            runtime._tasks.add(extra_task)
        elif invalidation == "generation":
            runtime._parse_generation += 1

    batch = AsyncMock()
    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", recover)
    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", batch)
    runtime.configure_delivery(store=object(), route_source=object(), parse_source=AsyncMock())
    try:
        with pytest.raises(HostedRuntimeError) as failure:
            await runtime.async_delivery_pass()
        assert str(failure.value) == "hosted_unavailable"
        batch.assert_not_awaited()
    finally:
        if extra_task is not None:
            runtime._tasks.discard(extra_task)
            extra_task.cancel()
            await asyncio.gather(extra_task, return_exceptions=True)
        await runtime.async_stop()


async def test_delivery_parser_cannot_be_called_from_an_unowned_task(hass, monkeypatch):
    from custom_components.daylight_calendar_import import hosted_ack_recovery, hosted_text_batch
    from custom_components.daylight_calendar_import.hosted_runtime import HostedRuntimeError

    runtime, _ = await verified_runtime(hass, monkeypatch)
    parser = AsyncMock(return_value=("parsed", None))
    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", AsyncMock())

    async def batch(**kwargs):
        with pytest.raises(HostedRuntimeError, match="hosted_unavailable"):
            await asyncio.create_task(kwargs["parse_source"]("unowned"))
        parser.assert_not_awaited()
        result, guard = await kwargs["parse_source"]("owned")
        assert result == "parsed"
        guard()
        return ["accepted"]

    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", batch)
    runtime.configure_delivery(store=object(), route_source=object(), parse_source=parser)
    assert await runtime.async_delivery_pass() == ["accepted"]
    parser.assert_awaited_once_with("owned")
    await runtime.async_stop()


@pytest.mark.parametrize("invalidation", ["generation", "owner", "extra_task", "pause"])
async def test_delivery_acceptance_guard_rechecks_fence_after_parse(
    hass, monkeypatch, invalidation,
):
    from custom_components.daylight_calendar_import import hosted_ack_recovery, hosted_text_batch
    from custom_components.daylight_calendar_import.hosted_runtime import HostedRuntimeError

    runtime, _ = await verified_runtime(hass, monkeypatch)
    additional = None
    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", AsyncMock())

    async def batch(**kwargs):
        nonlocal additional
        _, guard = await kwargs["parse_source"]("source")
        if invalidation == "generation":
            runtime._parse_generation += 1
        elif invalidation == "owner":
            runtime._delivery_owner = None
        elif invalidation == "extra_task":
            additional = asyncio.create_task(asyncio.sleep(60))
            runtime._tasks.add(additional)
        elif invalidation == "pause":
            runtime.pause()
        with pytest.raises(HostedRuntimeError) as failure:
            guard()
        assert str(failure.value) == "hosted_unavailable"
        return []

    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", batch)
    runtime.configure_delivery(
        store=object(), route_source=object(), parse_source=AsyncMock(return_value=(None, None)),
    )
    try:
        assert await runtime.async_delivery_pass() == []
    finally:
        if additional is not None:
            runtime._tasks.discard(additional)
            additional.cancel()
            await asyncio.gather(additional, return_exceptions=True)
        await runtime.async_stop()


async def test_delivery_pass_surfaces_expired_ack_without_reparse(hass, monkeypatch):
    from custom_components.daylight_calendar_import import (
        hosted_ack_recovery, hosted_runtime, hosted_text_batch,
    )

    runtime, _ = await verified_runtime(hass, monkeypatch)
    recovery = AsyncMock(return_value=[SimpleNamespace(ack_status="expired")])
    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", recovery)
    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", AsyncMock(return_value=[]))
    repair = Mock()
    monkeypatch.setattr(hosted_runtime.ir, "async_create_issue", repair)
    parser = AsyncMock()
    runtime.configure_delivery(store=object(), route_source=object(), parse_source=parser)
    assert await runtime.async_delivery_pass() == []
    assert repair.call_args.kwargs["translation_key"] == "hosted_delivery_expired"
    assert repair.call_args.kwargs["is_persistent"] is True
    parser.assert_not_awaited()
    await runtime.async_stop()


@pytest.mark.parametrize("failure", [
    "uncertain", "rate_limited", "invalid_local", "success", "bad_request",
    "unauthorized", "forbidden", "payload_too_large", "not_definitive",
    "uncertain_bad_request",
])
async def test_managed_delivery_parse_fences_attempt_before_io(hass, monkeypatch, failure):
    from custom_components.daylight_calendar_import import hosted_ack_recovery, hosted_text_batch
    from custom_components.daylight_calendar_import.hosted_parse_transport import HostedParseError
    from custom_components.daylight_calendar_import.providers import SourceValidationError
    from tests.test_storage import FakeStoreBackend, make_store

    runtime, _ = await verified_runtime(hass, monkeypatch)
    backend = FakeStoreBackend()
    store = make_store(monkeypatch, backend)
    source = SimpleNamespace(upstream_source_id="hosted:entry:delivery")
    await_called = []
    error = {
        "uncertain": HostedParseError("provider_timeout", uncertain=True),
        "rate_limited": HostedParseError("rate_limited", status=429, retry_after=300),
        "invalid_local": SourceValidationError("invalid_source", "Invalid source"),
        "bad_request": HostedParseError("bad_request", status=400),
        "unauthorized": HostedParseError("unauthorized", status=401),
        "forbidden": HostedParseError("forbidden", status=403),
        "payload_too_large": HostedParseError("payload_too_large", status=413),
        "not_definitive": HostedParseError("not_found", status=404),
        "uncertain_bad_request": HostedParseError("provider_timeout", status=400, uncertain=True),
    }.get(failure)

    async def parse(got):
        assert got is source
        assert store.hosted_parse_uncertain
        assert backend.saved[-1]["hosted_parse_attempt"].startswith("v1:source:")
        await_called.append(True)
        if error:
            raise error
        return "success", None

    async def batch(**kwargs):
        return await kwargs["parse_source"](source)

    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", AsyncMock())
    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", batch)
    runtime.configure_delivery(store=store, route_source=object(), parse_source=parse)
    runtime._delivery_poll_running = True
    await runtime._async_delivery_poll()
    assert await_called == [True]
    assert store.hosted_parse_uncertain == (failure in {
        "uncertain", "success", "not_definitive", "uncertain_bad_request",
    })
    if failure == "rate_limited":
        assert (runtime._delivery_due_at - datetime.now(UTC)).total_seconds() > 295
    restarted = make_store(monkeypatch, FakeStoreBackend(load_result=backend.saved[-1]))
    await restarted.async_load()
    assert restarted.hosted_parse_uncertain == store.hosted_parse_uncertain
    await runtime.async_stop()


@pytest.mark.parametrize("rate_limited", [False, True])
async def test_real_delivery_retry_preserves_parse_transport_evidence(hosted_review, monkeypatch, rate_limited):
    from custom_components.daylight_calendar_import import hosted_runtime, hosted_source_transport
    from tests.test_hosted_ack_dispatch import Transport, checkpoint
    from tests.test_hosted_text_acceptance import page

    store = hosted_review.store
    runtime = store.hosted_runtime
    credential = checkpoint()
    runtime._service = SERVICE
    runtime._credential_store = SimpleNamespace(snapshot=credential)
    runtime._controller = object()
    runtime._verified_parse_credential = credential.current
    runtime._state = HostedConnectionResult("connected", "current")
    monkeypatch.setattr(HostedConnectionRuntime, "parse_session", REAL_PARSE_SESSION)
    error = (HostedParseError("rate_limited", status=429, retry_after=300) if rate_limited
             else HostedParseError("provider_timeout", uncertain=True))
    wire = AsyncMock(side_effect=error)
    monkeypatch.setattr(HostedParseTransport, "async_parse", wire)
    ack = Transport()
    claim = SimpleNamespace(service_binding=SERVICE, async_claim=AsyncMock(return_value=page()))
    monkeypatch.setattr(hosted_source_transport, "HostedSourceTransport", lambda *_: claim)
    monkeypatch.setattr(hosted_runtime, "HostedLifecycleTransport", lambda *_, **__: ack)
    runtime._delivery_poll_running = True
    await runtime._async_delivery_poll()
    assert wire.await_count == 1
    assert not ack.calls and not store.hosted_ack_journal()
    assert store.hosted_parse_uncertain is not rate_limited
    if rate_limited:
        assert (runtime._delivery_due_at - datetime.now(UTC)).total_seconds() > 295
    await runtime.async_stop()


async def test_fresh_delivery_batch_surfaces_its_terminal_ack_expiry(hass, hosted_review, monkeypatch):
    from custom_components.daylight_calendar_import import (
        hosted_ack_recovery, hosted_runtime, hosted_text_batch,
    )
    from tests.test_hosted_delivery_journal import evidence

    runtime, _ = await verified_runtime(hass, monkeypatch)
    store = hosted_review.store
    item = replace(evidence(), local_config_entry_id=runtime._entry_id)
    await store.async_add(source_text="Private source", events=[], source_id=item.source_id,
                          hosted_evidence=item)
    entry = store.hosted_ack_journal()[0]

    async def batch(**_kwargs):
        store._hosted_delivery_journal = (replace(entry, ack_status="expired"),)
        return []

    monkeypatch.setattr(hosted_ack_recovery, "async_recover_pending_acks", AsyncMock(return_value=[]))
    monkeypatch.setattr(hosted_text_batch, "async_process_text_batch", batch)
    repair = Mock()
    monkeypatch.setattr(hosted_runtime.ir, "async_create_issue", repair)
    runtime.configure_delivery(store=store, route_source=object(), parse_source=AsyncMock())
    assert await runtime.async_delivery_pass() == []
    assert repair.call_args.kwargs["translation_key"] == "hosted_delivery_expired"
    await runtime.async_stop()
