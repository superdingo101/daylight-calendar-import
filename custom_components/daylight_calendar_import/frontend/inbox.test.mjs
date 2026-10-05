import assert from "node:assert/strict";
import test from "node:test";
import {decideEvent, formatDateTime, formatEventRange, loadActivity, loadActivityDetail, loadInbox, loadImport, resolveEvent, saveEvent, summarizeImport} from "./inbox.js";

test("uncertain recovery sends a scoped explicit resolution and validates the response", async () => {
  const requests = [];
  const event = {id: "event", title: "Picnic", status: "write_uncertain"};
  const hass = {callWS: async request => {
    requests.push(request);
    return {response: request.service_data};
  }};
  for (const choice of ["created", "not_created", "discard"]) {
    assert.deepEqual(await resolveEvent(hass, "import", event, choice),
      {pending_id: "import", event_id: "event", resolution: choice, expected_event: event});
  }
  assert.deepEqual(requests.map(request => request.service_data), [
    {pending_id: "import", event_id: "event", resolution: "created", expected_event: event},
    {pending_id: "import", event_id: "event", resolution: "not_created", expected_event: event},
    {pending_id: "import", event_id: "event", resolution: "discard", expected_event: event},
  ]);
  await assert.rejects(resolveEvent(hass, "import", event, "retry"), /Invalid recovery choice/);
  await assert.rejects(resolveEvent({callWS: async () => ({response: {resolution: "created"}})},
    "import", event, "created"), /unexpected response/);
});


test("loads authenticated activity summaries and scoped transitions", async () => {
  const requests = [];
  const hass = {callWS: async request => {
    requests.push(request);
    return request.service === "list_activity" ? {response: {activity: [{id: "one"}]}} :
      {response: {activity: {id: "one", transitions: []}}};
  }};
  assert.deepEqual(await loadActivity(hass), [{id: "one"}]);
  assert.deepEqual(await loadActivityDetail(hass, "one"), {id: "one", transitions: []});
  assert.deepEqual(requests.map(request => request.service), ["list_activity", "get_activity"]);
  assert.deepEqual(requests[1].service_data, {pending_id: "one"});
  await assert.rejects(loadActivity({callWS: async () => ({response: {}})}), /unexpected response/);
  await assert.rejects(loadActivityDetail(hass, "other"), /unexpected response/);
});

test("decisions carry the loaded snapshot and validate the service response", async () => {
  const event = {id: "e", title: "Meeting", status: "pending"};
  const calls = [];
  const hass = {callWS: async request => {
    calls.push(request);
    return {response: {pending_id: "one", event_id: "e",
      [request.service === "approve_pending_event" ? "approved" : "rejected"]: true}};
  }};
  await decideEvent(hass, "one", event, "approve");
  await decideEvent(hass, "one", event, "reject");
  assert.deepEqual(calls.map(call => call.service), ["approve_pending_event", "reject_pending_event"]);
  assert.deepEqual(calls[0].service_data, {pending_id: "one", event_id: "e", expected_event: event});
  await assert.rejects(decideEvent(hass, "one", event, "delete"), /Invalid review action/);
  await assert.rejects(decideEvent({callWS: async () => ({response: {approved: true}})},
    "one", event, "approve"), /unexpected response/);
});

test("edits use the complete stale snapshot and optionally route the destination calendar", async () => {
  const original = {id: "event", status: "pending", calendar_entity: "calendar.legacy",
    title: "Meeting", start: "2026-10-01T10:00:00-04:00", end: "2026-10-01T11:00:00-04:00",
    all_day: false, description: "Zoom meeting ID 123, passcode abc"};
  const draft = {...original, description: "x".repeat(12000)};
  let request;
  const saved = await saveEvent({callWS: async (message) => {
    request = message;
    return {response: {pending_id: "one", event: {...original, ...draft}}};
  }}, "one", original, draft, "calendar.family");
  assert.equal(saved.description.length, 12000);
  assert.equal(request.service, "edit_pending_event");
  assert.deepEqual(request.service_data.expected_event, original);
  assert.equal(request.service_data.event.description.length, 12000);
  assert.equal(request.service_data.calendar_entity, "calendar.family");
  await assert.rejects(saveEvent({callWS: async () => ({response: {event: original}})},
    "one", original, draft), /unexpected response/);

  let unrouted;
  await saveEvent({callWS: async message => {
    unrouted = message;
    return {response: {pending_id: "one", event: original}};
  }}, "one", original, draft);
  assert.equal(Object.hasOwn(unrouted.service_data, "calendar_entity"), false);
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

test("formats received timestamps and timed event ranges with Home Assistant time preferences", () => {
  const twelveHour = {language: "en-US", time_format: "12"};
  const twentyFourHour = {language: "en-US", time_format: "24"};
  const event = {all_day: false, start: "2026-10-07T20:00:00-07:00",
    end: "2026-10-07T21:00:00-07:00"};
  assert.equal(formatDateTime("2026-10-05T01:18:00Z", twelveHour, "America/Los_Angeles"),
    "Oct 4, 2026, 6:18 PM");
  assert.equal(formatDateTime("2026-10-05T01:18:00Z", twentyFourHour, "America/Los_Angeles"),
    "Oct 4, 2026, 18:18");
  assert.equal(formatEventRange(event, twelveHour, "America/Los_Angeles").replace(/\s/g, " "),
    "Oct 7, 2026 · 8–9 PM (PDT)");
  assert.equal(formatEventRange(event, twentyFourHour, "America/Los_Angeles"),
    "Oct 7, 2026 · 20:00–21:00 (PDT)");
});

test("timed ranges show both dates, preserve the event zone, and expose DST changes", () => {
  const locale = {language: "en-US", time_format: "12"};
  assert.equal(formatEventRange({all_day: false,
    start: "2026-10-07T23:00:00-07:00", end: "2026-10-08T01:00:00-07:00"},
  locale, "America/Los_Angeles").replace(/\s/g, " "),
  "Oct 7, 2026, 11 PM – Oct 8, 2026, 1 AM (PDT)");
  assert.equal(formatEventRange({all_day: false,
    start: "2026-10-07T20:00:00-04:00", end: "2026-10-07T21:00:00-04:00"},
  locale, "America/Los_Angeles").replace(/\s/g, " "),
  "Oct 7, 2026 · 8–9 PM (UTC-04:00)");
  assert.equal(formatEventRange({all_day: false,
    start: "2026-11-01T00:30:00-07:00", end: "2026-11-01T02:30:00-08:00"},
  locale, "America/Los_Angeles").replace(/\s/g, " "),
  "Nov 1, 2026 · 12:30 AM (PDT) – 2:30 AM (PST)");
});

test("language time format supports locales that use non-Latin digits", () => {
  const value = formatEventRange({
    all_day: false,
    start: "2026-10-07T20:00:00-07:00",
    end: "2026-10-07T21:00:00-07:00",
  }, {language: "ar-EG", time_format: "language"}, "America/Los_Angeles");
  assert.match(value, /م/);
});

test("remote DST ranges preserve each explicit endpoint offset", () => {
  const locale = {language: "en-US", time_format: "12"};
  assert.equal(formatEventRange({
    all_day: false,
    start: "2026-11-01T00:30:00-07:00",
    end: "2026-11-01T02:30:00-08:00",
  }, locale, "America/New_York").replace(/\s/g, " "),
  "Nov 1, 2026 · 12:30 AM (UTC-07:00) – 2:30 AM (UTC-08:00)");
});

test("12-hour ranges preserve locale-specific day-period ordering", () => {
  const value = formatEventRange({
    all_day: false,
    start: "2026-10-07T20:00:00-07:00",
    end: "2026-10-07T21:00:00-07:00",
  }, {language: "zh-CN", time_format: "12"}, "America/Los_Angeles");
  assert.match(value, /下午8时–9时/);
});
