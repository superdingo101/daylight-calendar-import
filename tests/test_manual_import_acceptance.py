"""Cross-layer manual intake acceptance with IMAP absent and real HA storage.

Requests come from the shipped browser transport functions, are dispatched over
HA's authenticated WebSocket API, and responses are checked by the shipped
frontend validator. Only the external AI and calendar boundaries are simulated.
"""

import asyncio
import base64
from io import BytesIO
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from aiohttp import FormData
from homeassistant.components.ai_task.const import AITaskEntityFeature
from homeassistant.components.file_upload import FileUploadData
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
try:
    from pytest_homeassistant.common import MockConfigEntry
except ModuleNotFoundError:
    from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.daylight_calendar_import.const import (
    CONF_AI_TASK_ENTITY, CONF_CALENDAR_ALIASES, CONF_CALENDAR_ENTITIES,
    CONF_CALENDAR_ENTITY, DOMAIN,
)

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+aX1sAAAAASUVORK5CYII=")

FRONTEND = Path(__file__).resolve().parents[1] / "custom_components/daylight_calendar_import/frontend"
TEXT = "School concert November 12, 2026, 7:00–8:00 PM at School gym. Bring tickets."
DST_SOURCE = """Calendar: ReleaseTest

Daylight Release Test - DST Spring Forward 8D
Date: March 14, 2027
Time: 2:15 AM to 2:45 AM local Pacific time (America/Los_Angeles)
Location: Test Conference Room
Description: Synthetic event during the spring daylight-saving transition. Do not substitute a different time."""
DRAFT = {
    "title": "School concert", "start": "2026-11-12T19:00:00-08:00",
    "end": "2026-11-12T20:00:00-08:00", "all_day": False,
    "location": "School gym", "description": "Bring tickets", "confidence": 0.95,
    "assumptions": [],
}


async def _frontend(script, payload):
    """Run the actual transport without reimplementing its request contract."""
    process = await asyncio.create_subprocess_exec(
        "node", "--input-type=module", "-e", script,
        cwd=FRONTEND, stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await process.communicate(json.dumps(payload).encode())
    assert process.returncode == 0, stderr.decode()
    return json.loads(stdout)


async def _request(kind="text", *, file_id=None, data=b"", source_id="manual-acceptance", text=TEXT, filename=None):
    return await _frontend('''
      import {submitText} from './manual_submission.js';
      import {submitFile} from './upload_transport.js';
      let input = ''; for await (const chunk of process.stdin) input += chunk;
      const p = JSON.parse(input); let request;
      const hass = {
        callWS: async r => {request = r; return {response: {
          pending: null, duplicate: false, duplicate_source: false,
          duplicate_events: 0, warnings: []}};},
        fetchWithAuth: async () => ({status: 200, json: async () => ({file_id: p.file_id})}),
      };
      if (p.kind === 'text') await submitText(hass, p.text, {sourceId: p.source_id});
      else await submitFile(hass, new File([Buffer.from(p.data, 'base64')],
        p.filename ?? (p.kind === 'pdf' ? 'concert.pdf' : 'concert.png')),
        {text: p.text, sourceId: p.source_id});
      process.stdout.write(JSON.stringify(request));
    ''', {"kind": kind, "file_id": file_id, "data": base64.b64encode(data).decode(),
          "text": text, "source_id": source_id, "filename": filename})


async def _call(client, request):
    await client.send_json_auto_id(request)
    return await client.receive_json()


async def _response(client, request):
    result = await _call(client, request)
    assert result["success"], result
    response = result["result"]["response"]
    assert await _frontend('''
      import {isSubmissionResponse} from './submission_response.js';
      let input = ''; for await (const chunk of process.stdin) input += chunk;
      process.stdout.write(JSON.stringify(isSubmissionResponse(JSON.parse(input))));
    ''', response)
    return response


@pytest.fixture
def manual_options():
    return {}


@pytest.fixture
async def manual_runtime(hass, monkeypatch, tmp_path, manual_options):
    # HA defaults to one process-global /tmp upload path. Isolate real HTTP
    # uploads so concurrent test environments cannot remove each other's files.
    monkeypatch.setattr("homeassistant.components.file_upload.TEMP_DIR_NAME", str(tmp_path / "ha-uploads"))
    generate = AsyncMock(return_value=SimpleNamespace(data={"unsupported_recurrence": False, "events": [DRAFT]}))
    monkeypatch.setattr("homeassistant.components.ai_task.async_generate_data", generate)
    entity = SimpleNamespace(supported_features=AITaskEntityFeature.SUPPORT_ATTACHMENTS, available=True)
    monkeypatch.setattr(
        "custom_components.daylight_calendar_import.providers.AITaskParserProvider._entity",
        lambda self: entity,
    )
    writes = AsyncMock()
    hass.services.async_register("calendar", "create_event", writes)
    monkeypatch.setattr("custom_components.daylight_calendar_import.async_classify_conflicts", AsyncMock(return_value=[]))
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_AI_TASK_ENTITY: "ai_task.test", CONF_CALENDAR_ENTITY: "calendar.family"},
                            options=manual_options)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    # No IMAP options, credentials or mailbox integration are configured.
    assert entry.options == manual_options
    return SimpleNamespace(entry=entry, store=hass.data[DOMAIN][entry.entry_id], generate=generate,
                           writes=writes, entity=entity)


@pytest.mark.parametrize(("kind", "original_filename"), [
    ("text", None),
    ("image", "Maple Grove December Family Events.png"),
    ("image", "Weekly meeting.png"),
    ("pdf", "Screenshot_20261024-123658~2.pdf"),
    ("pdf", "Weekly meeting.pdf"),
])
async def test_browser_intake_review_approve_without_email(
    hass, hass_ws_client, hass_client, manual_runtime, tmp_path, kind, original_filename,
):
    runtime = manual_runtime
    client = await hass_ws_client(hass)
    file_id = "a" * 32
    data = b""
    if kind != "text":
        if kind == "pdf":
            writer = PdfWriter()
            page = writer.add_blank_page(width=200, height=200)
            stream = DecodedStreamObject()
            stream.set_data(b"0 0 100 100 re f")
            page[NameObject("/Contents")] = writer._add_object(stream)
            output = BytesIO()
            writer.write(output)
            data = output.getvalue()
        else:
            data = PNG
        http = await hass_client()
        multipart = FormData()
        multipart.add_field("file", data, filename="daylight-upload.pdf" if kind == "pdf" else "daylight-upload.png",
                            content_type="application/pdf" if kind == "pdf" else "image/png")
        uploaded = await http.post("/api/file_upload", data=multipart)
        assert uploaded.status == 200
        file_id = (await uploaded.json())["file_id"]
        upload_dir = hass.data["file_upload"].file_dir(file_id)
        media_dir = tmp_path / "media"
        hass.config.media_dirs = {"local": str(media_dir)}
    response = await _response(client, await _request(kind, file_id=file_id, data=data, filename=original_filename))
    assert response["status"] == "pending_review"
    runtime.writes.assert_not_awaited()
    runtime.generate.assert_awaited_once()
    pending_id = response["pending"]["id"]
    assert runtime.store.get(pending_id).events[0].draft.title == DRAFT["title"]
    if kind != "text":
        assert response["pending"]["source_title"] == original_filename
        assert runtime.store.get(pending_id).source_title == original_filename
        assert any(item["source_title"] == original_filename for item in runtime.store.list_activity())
        assert not upload_dir.exists()
        assert not hass.data["file_upload"].files
        assert not list(media_dir.glob("**/*"))
        assert runtime.generate.await_args.kwargs["attachments"][0]["media_content_type"] == (
            "application/pdf" if kind == "pdf" else "image/png")
        # A filename that looks like a recurrence is display metadata only.
        # The recurrence guard must receive the safe upload title instead.
        instructions = runtime.generate.await_args.kwargs["instructions"]
        safe_filename = "daylight-upload.pdf" if kind == "pdf" else "daylight-upload.png"
        assert f"Source title: {safe_filename}" in instructions
        assert original_filename not in instructions

    review = await _call(client, {"type": "call_service", "domain": DOMAIN, "service": "get_pending",
                                 "service_data": {"pending_id": pending_id}, "return_response": True})
    assert review["success"], review
    assert review["result"]["response"]["pending"]["events"][0]["title"] == DRAFT["title"]
    if kind != "text":
        assert review["result"]["response"]["pending"]["source_title"] == original_filename
    event_id = response["pending"]["events"][0]["id"]
    approval = await _call(client, {"type": "call_service", "domain": DOMAIN, "service": "approve_pending_event",
                                   "service_data": {"pending_id": pending_id, "event_id": event_id}, "return_response": True})
    assert approval["success"], approval
    assert approval["result"]["response"]["approved"] is True
    runtime.writes.assert_awaited_once()
    assert runtime.writes.await_args.args[0].data["summary"] == DRAFT["title"]
    assert runtime.writes.await_args.args[0].data["entity_id"] == "calendar.family"

    # Repeated same source cannot write or parse another calendar event.
    duplicate = await _response(client, await _request(source_id="manual-acceptance"))
    assert duplicate["status"] == "duplicate"
    assert duplicate["duplicate_source"] is True
    assert duplicate["pending"] is None
    runtime.generate.assert_awaited_once()
    runtime.writes.assert_awaited_once()


async def test_manual_provider_failure_is_recorded_and_explicit_retry_recovers(hass, hass_ws_client, manual_runtime):
    runtime = manual_runtime
    client = await hass_ws_client(hass)
    runtime.generate.side_effect = TimeoutError("provider timeout")
    saved = await _response(client, await _request())
    assert saved["status"] == "saved_for_retry"
    job_id = saved["retry_job"]["id"]
    assert saved["retry_job"]["state"] == "waiting"
    assert runtime.store.get_retry_job(job_id).source_text == TEXT
    assert runtime.store.list() == ()
    runtime.writes.assert_not_awaited()

    # A second submission with the same source cannot create a second job.
    duplicate = await _response(client, await _request())
    assert duplicate["status"] == "duplicate"
    runtime.generate.assert_awaited_once()

    runtime.generate.side_effect = None
    retried = await _call(client, {
        "type": "call_service", "domain": DOMAIN, "service": "retry_job",
        "service_data": {"job_id": job_id}, "return_response": True,
    })
    assert retried["success"], retried
    response = retried["result"]["response"]
    assert response["status"] == "pending_review"
    assert response["pending"]["id"] == job_id
    assert runtime.store.get_retry_job(job_id) is None
    assert runtime.generate.await_count == 2
    runtime.writes.assert_not_awaited()


async def test_cancelled_source_can_be_retried_manually(hass, hass_ws_client, manual_runtime):
    runtime = manual_runtime
    client = await hass_ws_client(hass)
    runtime.generate.side_effect = TimeoutError("temporarily unavailable")
    saved = await _response(client, await _request())
    job_id = saved["retry_job"]["id"]
    cancelled = await _call(client, {
        "type": "call_service", "domain": DOMAIN, "service": "cancel_retry_job",
        "service_data": {"job_id": job_id}, "return_response": True,
    })
    assert cancelled["success"], cancelled
    assert cancelled["result"]["response"]["job"]["state"] == "cancelled"
    runtime.generate.side_effect = None
    resumed = await _call(client, {
        "type": "call_service", "domain": DOMAIN, "service": "retry_job",
        "service_data": {"job_id": job_id}, "return_response": True,
    })
    assert resumed["success"], resumed
    assert resumed["result"]["response"]["status"] == "pending_review"


async def test_read_only_user_cannot_submit_or_read_private_drafts(
    hass, hass_ws_client, hass_read_only_access_token, manual_runtime,
):
    client = await hass_ws_client(hass, access_token=hass_read_only_access_token)
    result = await _call(client, await _request())
    assert result["success"] is False
    assert result["error"]["code"] in {"unauthorized", "home_assistant_error"}
    manual_runtime.generate.assert_not_awaited()
    manual_runtime.writes.assert_not_awaited()
    assert manual_runtime.store.list() == ()
    assert manual_runtime.store.list_activity() == ()
    owner = await hass_ws_client(hass)
    response = await _response(owner, await _request())
    denied = await _call(client, {"type": "call_service", "domain": DOMAIN, "service": "get_pending",
                                 "service_data": {"pending_id": response["pending"]["id"]}, "return_response": True})
    assert denied["success"] is False
    refused = await _call(client, {"type": "call_service", "domain": DOMAIN, "service": "approve_pending_event",
                                  "service_data": {"pending_id": response["pending"]["id"],
                                                   "event_id": response["pending"]["events"][0]["id"]},
                                  "return_response": True})
    assert refused["success"] is False
    manual_runtime.writes.assert_not_awaited()


async def test_text_only_provider_accepts_text_but_rejects_visual_upload(
    hass, hass_ws_client, manual_runtime, tmp_path,
):
    runtime = manual_runtime
    runtime.entity.supported_features = 0
    client = await hass_ws_client(hass)
    response = await _response(client, await _request())
    assert response["status"] == "pending_review"
    file_id = "b" * 32
    image = PNG
    (tmp_path / file_id).mkdir()
    (tmp_path / file_id / "concert.png").write_bytes(image)
    hass.data["file_upload"] = FileUploadData(tmp_path, {file_id: "concert.png"})
    hass.config.media_dirs = {"local": str(tmp_path / "media")}
    rejected = await _call(client, await _request("image", file_id=file_id, data=image, source_id="visual-input"))
    assert rejected["success"] is False
    assert runtime.store.list_activity()[0]["status"] == "failed"
    assert not (tmp_path / file_id).exists()
    assert len(runtime.store.list()) == 1
    runtime.generate.assert_awaited_once()
    runtime.writes.assert_not_awaited()


async def test_no_events_and_event_duplicate_results_never_write_calendar(hass, hass_ws_client, manual_runtime):
    runtime = manual_runtime
    client = await hass_ws_client(hass)
    runtime.generate.return_value = SimpleNamespace(data={"unsupported_recurrence": False, "events": []})
    empty = await _response(client, await _request(source_id="no-events"))
    assert empty["status"] == "no_events"
    assert empty["pending"] is None
    assert runtime.store.list() == ()
    runtime.generate.return_value = SimpleNamespace(data={"unsupported_recurrence": False, "events": [DRAFT]})
    first = await _response(client, await _request(source_id="first-source"))
    assert first["status"] == "pending_review"
    repeated = await _response(client, await _request(source_id="another-source"))
    assert repeated["status"] == "duplicate"
    assert repeated["duplicate_events"] == 1
    assert repeated["duplicate_source"] is False
    assert repeated["pending"] is None
    assert len(runtime.store.list()) == 1
    runtime.writes.assert_not_awaited()


@pytest.mark.parametrize("manual_options", [{
    CONF_CALENDAR_ENTITIES: ["calendar.family", "calendar.mike_test"],
    CONF_CALENDAR_ALIASES: {"ReleaseTest": "calendar.mike_test"},
}])
async def test_manual_named_zone_spring_gap_is_rejected(hass, hass_ws_client, manual_runtime):
    hass.config.time_zone = "America/Los_Angeles"
    runtime = manual_runtime
    client = await hass_ws_client(hass)
    # Reconstructed from the reported display and the beta prompt, without
    # assuming access to the original Gemini response.
    runtime.generate.return_value = SimpleNamespace(data={
        "unsupported_recurrence": False,
        "events": [{
            "title": "Daylight Release Test - DST Spring Forward 8D",
            "start": "2027-03-14T02:15:00-07:00",
            "end": "2027-03-14T02:45:00-07:00",
            "all_day": False,
            "uses_reference_time_zone": False,
            "location": "Test Conference Room",
            "description": "Synthetic event during the spring daylight-saving transition.",
            "confidence": 1,
            "assumptions": [],
        }],
    })
    response = await _response(client, await _request(text=DST_SOURCE))
    runtime.generate.assert_awaited_once()
    assert response["status"] == "no_events"
    assert response["pending"] is None
    assert len(response["warnings"]) == 1
    assert "does not exist" in response["warnings"][0]
    assert "America/Los_Angeles" in response["warnings"][0]
    assert "valid local time" in response["warnings"][0]
    assert runtime.store.list() == ()
    activity = await _call(client, {
        "type": "call_service", "domain": DOMAIN, "service": "get_activity",
        "service_data": {"pending_id": response["activity_id"]}, "return_response": True,
    })
    assert activity["success"], activity
    record = activity["result"]["response"]["activity"]
    assert record["guidance"] == response["warnings"][0]
    assert record["manual_source_text"] == DST_SOURCE
    runtime.writes.assert_not_awaited()


async def test_text_layer_pdf_works_with_text_only_provider(
    hass, hass_ws_client, hass_client, manual_runtime,
):
    runtime = manual_runtime
    runtime.entity.supported_features = 0
    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    page[NameObject("/Resources")] = DictionaryObject({
        NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
    })
    content = DecodedStreamObject()
    content.set_data(b"BT /F1 12 Tf 20 200 Td (School concert November 12, 2026, 7-8 PM) Tj ET")
    page[NameObject("/Contents")] = writer._add_object(content)
    output = BytesIO()
    writer.write(output)
    data = output.getvalue()
    http = await hass_client()
    multipart = FormData()
    multipart.add_field("file", data, filename="concert.pdf", content_type="application/pdf")
    uploaded = await http.post("/api/file_upload", data=multipart)
    assert uploaded.status == 200
    file_id = (await uploaded.json())["file_id"]
    directory = hass.data["file_upload"].file_dir(file_id)
    client = await hass_ws_client(hass)
    response = await _response(client, await _request("pdf", file_id=file_id, data=data))
    assert response["status"] == "pending_review"
    assert "attachments" not in runtime.generate.await_args.kwargs
    assert "School concert November 12, 2026, 7-8 PM" in runtime.generate.await_args.kwargs["instructions"]
    assert not directory.exists()
    runtime.writes.assert_not_awaited()



async def test_saved_retry_activity_can_be_cancelled_and_deleted(
    hass, hass_ws_client, manual_runtime,
):
    runtime = manual_runtime
    client = await hass_ws_client(hass)
    runtime.generate.side_effect = TimeoutError("private provider outage")
    saved = await _response(client, await _request())
    job_id = saved["retry_job"]["id"]

    listing = await _call(client, {
        "type": "call_service", "domain": DOMAIN, "service": "list_retry_jobs",
        "service_data": {}, "return_response": True,
    })
    assert listing["success"], listing
    assert listing["result"]["response"]["jobs"][0]["id"] == job_id
    assert "source_text" not in listing["result"]["response"]["jobs"][0]
    activity = await _call(client, {
        "type": "call_service", "domain": DOMAIN, "service": "get_activity",
        "service_data": {"pending_id": job_id}, "return_response": True,
    })
    assert activity["success"], activity
    assert activity["result"]["response"]["activity"]["retry_job"]["state"] == "waiting"

    cancel = {"type": "call_service", "domain": DOMAIN,
              "service": "cancel_retry_job", "service_data": {"job_id": job_id},
              "return_response": True}
    cancelled = await _call(client, cancel)
    assert cancelled["success"], cancelled
    assert cancelled["result"]["response"]["job"]["state"] == "cancelled"
    duplicate_cancel = await _call(client, cancel)
    assert duplicate_cancel["success"], duplicate_cancel
    assert duplicate_cancel["result"]["response"]["job"]["state"] == "cancelled"
    deleted = await _call(client, {
        "type": "call_service", "domain": DOMAIN, "service": "delete_retry_job",
        "service_data": {"job_id": job_id}, "return_response": True,
    })
    assert deleted["success"], deleted
    assert deleted["result"]["response"] == {"job_id": job_id, "deleted": True}
    assert runtime.store.get_retry_job(job_id) is None


async def test_provider_response_after_cancel_never_creates_pending_import(
    hass, hass_ws_client, manual_runtime,
):
    runtime = manual_runtime
    client = await hass_ws_client(hass)
    control_client = await hass_ws_client(hass)
    started = asyncio.Event()
    release = asyncio.Event()

    async def late_response(*_args, **_kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(data={"unsupported_recurrence": False, "events": [DRAFT]})

    runtime.generate.side_effect = late_response
    submission = asyncio.create_task(_call(client, await _request()))
    await started.wait()
    jobs = runtime.store.list_retry_jobs()
    assert len(jobs) == 1
    job_id = jobs[0].id
    cancelled = await _call(control_client, {
        "type": "call_service", "domain": DOMAIN, "service": "cancel_retry_job",
        "service_data": {"job_id": job_id}, "return_response": True,
    })
    assert cancelled["success"], cancelled
    release.set()
    result = await submission
    assert not result["success"], result
    assert runtime.store.get(job_id) is None
    assert runtime.store.get_retry_job(job_id).state == "cancelled"
    runtime.writes.assert_not_awaited()
