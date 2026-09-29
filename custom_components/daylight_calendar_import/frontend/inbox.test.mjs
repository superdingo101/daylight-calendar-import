import assert from "node:assert/strict";
import test from "node:test";
import {loadInbox, loadImport, saveEvent, summarizeImport} from "./inbox.js";

test("edits use the complete stale snapshot and never request destination routing", async () => {
  const original = {id: "event", status: "pending", calendar_entity: "calendar.legacy",
    title: "Meeting", start: "2026-10-01T10:00:00-04:00", end: "2026-10-01T11:00:00-04:00",
    all_day: false, description: "Zoom meeting ID 123, passcode abc"};
  const draft = {...original, description: "x".repeat(12000)};
  let request;
  const saved = await saveEvent({callWS: async (message) => {
    request = message;
    return {response: {pending_id: "one", event: {...original, ...draft}}};
  }}, "one", original, draft);
  assert.equal(saved.description.length, 12000);
  assert.equal(request.service, "edit_pending_event");
  assert.deepEqual(request.service_data.expected_event, original);
  assert.equal(request.service_data.event.description.length, 12000);
  assert.equal(Object.hasOwn(request.service_data, "calendar_entity"), false);
  await assert.rejects(saveEvent({callWS: async () => ({response: {event: original}})},
    "one", original, draft), /unexpected response/);
});

test("loads detail with a scoped pending ID and rejects a mismatched response", async () => {
  let request;
  const hass = {callWS: async (value) => {
    request = value;
    return {response: {pending: {id: "one", events: []}}};
  }};
  assert.deepEqual(await loadImport(hass, "one"), {id: "one", events: []});
  assert.deepEqual(request, {type: "call_service", domain: "daylight_calendar_import",
    service: "get_pending", service_data: {pending_id: "one"}, return_response: true});
  await assert.rejects(loadImport(hass, "two"), /unexpected response/);
});

test("loads summaries through the response-enabled HA action", async () => {
  const calls = [];
  const imports = [{id: "one"}];
  assert.deepEqual(await loadInbox({callWS: async (msg) => {
    calls.push(msg);
    return {response: {imports}};
  }}), imports);
  assert.deepEqual(calls, [{type: "call_service", domain: "daylight_calendar_import",
    service: "list_pending", return_response: true}]);
});

test("empty, invalid, and rejected inbox responses", async () => {
  assert.deepEqual(await loadInbox({callWS: async () => ({response: {imports: []}})}), []);
  await assert.rejects(loadInbox({callWS: async () => ({response: {imports: null}})}), /unexpected response/);
  await assert.rejects(loadInbox({callWS: async () => {throw new Error("Permission denied");}}), /Permission denied/);
});

test("summaries use local time and show concrete attention indicators", () => {
  const item = {source_kind: "pdf", source_title: "schedule.pdf", title: "Soccer",
    created_at: "2026-10-08T17:30:00Z", event_count: 2,
    warnings: ["Event 3 missing a date"], duplicate_events: 1, approval_in_flight: true};
  const summary = summarizeImport(item, "en-US");
  assert.equal(summary.title, "schedule.pdf");
  assert.equal(summary.type, "PDF");
  assert.equal(summary.events, "2 events");
  assert.equal(summary.warnings, 1);
  assert.equal(summary.duplicates, 1);
  assert.equal(summary.uncertain, true);
  assert.notEqual(summary.created, "Unknown time");
  assert.equal(summarizeImport({...item, source_title: null, event_count: 1}, "en-US").events, "1 event");
  assert.equal(summarizeImport({...item, created_at: "invalid", source_kind: "other"}, "en-US").created, "Unknown time");
  assert.equal(summarizeImport({...item, source_title: null, title: ""}, "en-US").title, "Untitled import");
});
