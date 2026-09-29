import assert from "node:assert/strict";
import test from "node:test";

class FakeNode {
  constructor(tag = "fragment") {
    this.tag = tag;
    this.children = [];
    this.attributes = {};
    this.dataset = {};
  }
  append(...children) { this.children.push(...children); }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  querySelector(tag) { return this.querySelectorAll(tag)[0] || null; }
  querySelectorAll(tag) {
    if (tag.includes(" ")) {
      const [parent, child] = tag.split(" ");
      return this.querySelectorAll(parent).flatMap(node => node.querySelectorAll(child));
    }
    return [this, ...this.children.flatMap(child => child.querySelectorAll(tag))]
      .filter(node => tag.startsWith(".") ? node.className === tag.slice(1) : node.tag === tag);
  }
  get elements() { return {namedItem: name => this.querySelectorAll("input").concat(this.querySelectorAll("textarea"))
    .find(node => node.name === name)}; }
  focus() { globalThis.focusedNode = this; }
  addEventListener(name, callback) { this[name] = callback; }
  attachShadow() { this.shadowRoot = new FakeNode("shadow"); return this.shadowRoot; }
}
globalThis.HTMLElement = FakeNode;
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createDocumentFragment: () => new FakeNode(),
};
globalThis.customElements = {define: () => {}};
const {DaylightImportPanel} = await import("./panel.js");
const find = (node, tag) => node.tag === tag ? node : node.children.map(child => find(child, tag)).find(Boolean);
const flush = () => new Promise(resolve => setImmediate(resolve));

test("refresh keeps its button and announces loading and errors", async () => {
  const panel = new DaylightImportPanel();
  const button = find(panel.shadowRoot, "button");
  let reject;
  panel.hass = {callWS: () => new Promise((_resolve, fail) => {reject = fail;})};
  assert.equal(find(panel._announcement, "span").textContent, "Loading imports…");
  reject(new Error("Permission denied"));
  await flush();
  assert.equal(find(panel._announcement, "span").textContent, "Permission denied");
  assert.equal(find(panel._content, "p").textContent, "Permission denied");
  assert.equal(find(panel.shadowRoot, "button"), button);
});

test("detail errors and absent inbox items retain useful keyboard focus", async () => {
  const panel = new DaylightImportPanel();
  let fail = false;
  panel.hass = {callWS: async (message) => {
    if (message.service === "get_pending") throw new Error("Permission denied");
    return {response: {imports: fail ? [] : [{id: "one", title: "Picnic", event_count: 1,
      created_at: "2026-10-01T12:00:00Z"}]}};
  }};
  await flush();
  await panel.showImport("one");
  assert.equal(globalThis.focusedNode.textContent, "Back to inbox");
  fail = true;
  panel.showInbox();
  await flush();
  assert.equal(globalThis.focusedNode.textContent, "Refresh");
});

test("a superseded refresh cannot replace newer data or a newer error", async () => {
  const panel = new DaylightImportPanel();
  const requests = [];
  panel.hass = {callWS: () => new Promise((resolve, reject) => requests.push({resolve, reject}))};
  const second = panel.refresh();
  requests[1].resolve({response: {imports: [{title: "Latest", source_kind: "pdf", event_count: 1,
    created_at: "2026-10-08T17:30:00Z"}]}});
  await second;
  requests[0].resolve({response: {imports: []}});
  await flush();
  assert.equal(find(panel._content, "h2").children[0].textContent, "Latest");

  const third = panel.refresh();
  const fourth = panel.refresh();
  requests[3].reject(new Error("Latest failure"));
  await fourth;
  requests[2].resolve({response: {imports: []}});
  await third;
  assert.equal(find(panel._content, "p").textContent, "Latest failure");
});

test("opens detail, renders source and events as text, and returns to inbox", async () => {
  const panel = new DaylightImportPanel();
  const requests = [];
  panel.hass = {callWS: async (message) => {
    requests.push(message);
    if (message.service === "get_pending") return {response: {pending: {
      id: "one", source_title: "Flyer", source_kind: "pdf", source_text: "Meet at noon",
      warnings: ["Check the time"], duplicate_events: 1,
      events: [{id: "event", title: "Picnic", start: "2026-10-01", end: "2026-10-02",
        all_day: true, status: "pending", calendar_entity: "calendar.family", location: "Park",
        confidence: 0}],
    }}};
    return {response: {imports: [{id: "one", title: "Picnic", event_count: 1,
      created_at: "2026-10-01T12:00:00Z"}]}};
  }};
  await flush();
  await panel.showImport("one");
  assert.equal(requests[1].service_data.pending_id, "one");
  assert.equal(find(panel._content, "h3").textContent, "Picnic");
  assert.equal(find(panel._content, "section").children[1].textContent,
    "2026-10-01 · All day");
  assert.equal(globalThis.focusedNode.tag, "h2");
  assert.equal(panel._announcement.textContent, undefined);
  assert.equal(find(panel._content, "p").attributes.role, undefined);
  assert.equal(find(panel._content, "section").children[2].textContent,
    "Calendar: calendar.family · Status: pending");
  assert.equal(find(panel._content, "section").children[3].textContent,
    "AI extraction confidence: 0% (estimate)");
  find(panel._content, "button").click();
  await flush();
  assert.equal(find(panel._content, "h2").children[0].textContent, "Picnic");
  assert.equal(globalThis.focusedNode.textContent, "Picnic");
});

test("editor preserves long meeting descriptions and retains a stale edit on failure", async () => {
  const panel = new DaylightImportPanel();
  const description = `Zoom: https://zoom.us/j/123 passcode abc ${"bring cupcakes ".repeat(900)}`;
  let event = {id: "event", title: "Meeting", start: "2026-10-01T10:00:00-04:00",
    end: "2026-10-01T11:00:00-04:00", all_day: false, status: "pending",
    calendar_entity: "calendar.legacy", confidence: 0.8, description};
  const sibling = {...event, id: "sibling", title: "Other meeting"};
  const requests = [];
  let stale = true;
  panel.hass = {callWS: async request => {
    requests.push(request);
    if (request.service === "list_pending") return {response: {imports: []}};
    if (request.service === "get_pending") return {response: {pending: {id: "one",
      default_calendar: "calendar.family", events: [event, sibling]}}};
    if (stale) throw {message: "Event changed since it was loaded; refresh before editing"};
    event = {...event, ...request.service_data.event};
    return {response: {pending_id: "one", event}};
  }};
  await flush();
  await panel.showImport("one");
  const siblingEdit = panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "sibling");
  const edit = find(panel._content, "section").querySelector("button");
  edit.click();
  const form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("description").value, description);
  assert.equal(form.elements.namedItem("start").value, event.start);
  assert.equal(form.querySelectorAll("select").length, 0);
  assert.equal(panel._content.querySelectorAll("button")[0].disabled, true);
  assert.equal(panel._content.querySelectorAll("button").some(button => button.dataset.eventId === "sibling"), false);
  siblingEdit.click();
  assert.equal(panel._editingId, "event");
  const cancel = form.querySelectorAll("button")[1];
  cancel.click();
  assert.equal(globalThis.focusedNode.dataset.eventId, "event");
  globalThis.focusedNode.click();
  const activeForm = find(panel._content, "form");
  await panel.saveEdit(event, activeForm);
  assert.match(find(activeForm, "p").textContent, /refresh before editing/);
  assert.equal(globalThis.focusedNode, find(activeForm, "p"));
  assert.equal(panel._content.querySelectorAll("button")[0].disabled, true);
  assert.equal(panel._editingId, "event");
  assert.deepEqual(requests.at(-1).service_data.expected_event, event);
  assert.equal(Object.hasOwn(requests.at(-1).service_data, "calendar_entity"), false);
  stale = false;
  let release;
  const realCallWS = panel._hass.callWS;
  panel._hass.callWS = request => request.service === "edit_pending_event" ?
    new Promise(resolve => {release = () => resolve(realCallWS(request));}) : realCallWS(request);
  const saving = panel.saveEdit(event, activeForm);
  assert.equal(activeForm.elements.namedItem("description").disabled, true);
  assert.equal(panel._refreshButton.disabled, true);
  siblingEdit.click();
  assert.equal(panel._editingId, "event");
  release();
  await saving;
  assert.equal(panel._editingId, null);
  assert.equal(panel._detail.events[0].description, description);
  assert.equal(requests.at(-1).service, "get_pending");
  assert.equal(globalThis.focusedNode.dataset.eventId, "event");
});

test("focus falls back to the detail heading if the saved event disappears", async () => {
  const panel = new DaylightImportPanel();
  const event = {id: "event", title: "Meeting", start: "2026-10-01", end: "2026-10-02",
    all_day: true, status: "pending", confidence: 0};
  let saved = false;
  panel.hass = {callWS: async request => {
    if (request.service === "list_pending") return {response: {imports: []}};
    if (request.service === "get_pending") return {response: {pending: {id: "one",
      events: saved ? [{...event, id: "sibling"}] : [event]}}};
    saved = true;
    return {response: {pending_id: "one", event}};
  }};
  await flush();
  await panel.showImport("one");
  find(panel._content, "section").querySelector("button").click();
  await panel.saveEdit(event, find(panel._content, "form"));
  assert.equal(globalThis.focusedNode.tag, "h2");
});

test("a successful save with a failed detail reload offers refresh and restores focus", async () => {
  const panel = new DaylightImportPanel();
  const event = {id: "event", title: "Meeting", start: "2026-10-01", end: "2026-10-02",
    all_day: true, status: "pending", confidence: 0};
  let reads = 0;
  panel.hass = {callWS: async request => {
    if (request.service === "list_pending") return {response: {imports: []}};
    if (request.service === "get_pending") {
      if (++reads === 2) throw {message: "Offline"};
      return {response: {pending: {id: "one", events: [event]}}};
    }
    return {response: {pending_id: "one", event}};
  }};
  await flush();
  await panel.showImport("one");
  find(panel._content, "section").querySelector("button").click();
  await panel.saveEdit(event, find(panel._content, "form"));
  assert.match(find(panel._content, "p").textContent, /saved, but the detail could not be reloaded: Offline/);
  assert.equal(globalThis.focusedNode.textContent, "Back to inbox");
});

test("approval requires explicit confirmation and returns to the inbox", async () => {
  const panel = new DaylightImportPanel();
  const event = {id: "event", title: "Picnic", start: "2026-10-01", end: "2026-10-02",
    all_day: true, status: "pending", confidence: 0};
  const calls = [];
  panel.hass = {callWS: async request => {
    calls.push(request);
    if (request.service === "get_pending") return {response: {pending: {id: "one", events: [event]}}};
    if (request.service === "list_pending") return {response: {imports: []}};
    return {response: {pending_id: "one", event_id: "event", approved: true}};
  }};
  await flush();
  await panel.showImport("one");
  const approve = find(panel._content, "section").querySelectorAll("button")
    .find(button => button.textContent === "Approve Picnic");
  approve.click();
  assert.equal(calls.filter(call => call.service === "approve_pending_event").length, 0);
  find(panel._content, "section").querySelectorAll("button")[1].click();
  assert.equal(globalThis.focusedNode.dataset.action, "approve");
  globalThis.focusedNode.click();
  const confirm = find(panel._content, "section").querySelector("button");
  assert.match(confirm.textContent, /Confirm approve/);
  await panel.runDecision(event, "approve");
  assert.equal(calls.at(-2).service, "approve_pending_event");
  assert.deepEqual(calls.at(-2).service_data.expected_event, event);
  assert.equal(panel._selectedId, null);
  assert.equal(find(panel._announcement, "span").textContent, "Event approved");
  assert.equal(globalThis.focusedNode, panel._refreshButton);
});

test("rejected decision keeps the import visible with the backend error", async () => {
  const panel = new DaylightImportPanel();
  const event = {id: "event", title: "Picnic", start: "2026-10-01", end: "2026-10-02",
    all_day: true, status: "pending", confidence: 0};
  let reads = 0;
  panel.hass = {callWS: async request => {
    if (request.service === "list_pending") return {response: {imports: []}};
    if (request.service === "get_pending") {
      reads++;
      return {response: {pending: {id: "one", events: [event]}}};
    }
    throw {message: "Event changed since it was loaded; refresh before deciding"};
  }};
  await flush();
  await panel.showImport("one");
  find(panel._content, "section").querySelectorAll("button")
    .find(button => button.textContent === "Reject Picnic").click();
  await panel.runDecision(event, "reject");
  assert.equal(reads, 2);
  assert.match(find(panel._content, "p").textContent, /refresh before deciding/);
  assert.equal(globalThis.focusedNode.attributes.role, "alert");
  find(panel._content, "section").querySelectorAll("button")
    .find(button => button.textContent === "Edit Picnic").click();
  assert.equal(panel._decisionError, null);
});

test("bulk approval reports each result and leaves failed events in review", async () => {
  const panel = new DaylightImportPanel();
  const first = {id: "first", title: "Practice", start: "2026-10-01",
    end: "2026-10-02", all_day: true, status: "pending", confidence: 0};
  const second = {...first, id: "second"};
  const calls = [];
  let remaining = [first, second];
  panel.hass = {callWS: async request => {
    calls.push(request);
    if (request.service === "list_pending") return {response: {imports: [{id: "one"}]}};
    if (request.service === "get_pending") return {response: {pending: {id: "one", events: remaining}}};
    if (request.service === "approve_pending_event" && request.service_data.event_id === "second") {
      throw {message: "Calendar write uncertain; verify before retrying"};
    }
    remaining = [second];
    return {response: {pending_id: "one", event_id: "first", approved: true}};
  }};
  await flush();
  await panel.showImport("one");
  const bulk = panel._content.querySelectorAll("button")
    .find(button => button.dataset.batchAction === "approve");
  bulk.click();
  assert.equal(calls.filter(call => call.service === "approve_pending_event").length, 0);
  assert.equal(panel._content.querySelectorAll("button")
    .some(button => button.textContent === "Edit Practice"), false);
  await panel.runBatch("approve");
  const decisions = calls.filter(call => call.service === "approve_pending_event");
  assert.deepEqual(decisions.map(call => call.service_data.event_id), ["first", "second"]);
  assert.deepEqual(decisions.map(call => call.service_data.expected_event), [first, second]);
  assert.deepEqual(panel._batchResults, [
    {id: "first", title: "Practice", range: "2026-10-01", outcome: "success"},
    {id: "second", title: "Practice", range: "2026-10-01",
      outcome: "Approval outcome unknown; check the calendar before retrying. Calendar write uncertain; verify before retrying"},
  ]);
  assert.equal(panel._detail.events[0].id, "second");
  assert.equal(globalThis.focusedNode.className, "batch-results");
  assert.match(panel._content.querySelector(".batch-results").querySelectorAll("p")[0].textContent,
    /event ID first/);
  assert.match(panel._content.querySelector(".batch-results").querySelectorAll("p")[1].textContent,
    /event ID second/);
  assert.match(panel._content.querySelector(".batch-results").querySelector("h2").textContent,
    /Bulk approve results for Import/);
  panel.showInbox();
  await flush();
  assert.equal(panel._batchResults.length, 0);
});

test("bulk confirmation can be canceled without calling a review action", async () => {
  const panel = new DaylightImportPanel();
  const event = {id: "one", title: "One", start: "2026-10-01", end: "2026-10-02",
    all_day: true, status: "pending", confidence: 0};
  const calls = [];
  panel.hass = {callWS: async request => {
    calls.push(request.service);
    return request.service === "get_pending" ? {response: {pending: {id: "import",
      events: [event, {...event, id: "two", title: "Two"}]}}} : {response: {imports: []}};
  }};
  await flush();
  await panel.showImport("import");
  panel._content.querySelectorAll("button")
    .find(button => button.dataset.batchAction === "reject").click();
  panel._content.querySelectorAll("button")
    .find(button => button.textContent === "Cancel bulk review").click();
  assert.equal(panel._batchAction, null);
  assert.equal(globalThis.focusedNode.dataset.batchAction, "reject");
  assert.deepEqual(calls, ["list_pending", "get_pending"]);
});
