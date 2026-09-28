import assert from "node:assert/strict";
import test from "node:test";
import {loadInbox, summarizeImport} from "./inbox.js";

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
