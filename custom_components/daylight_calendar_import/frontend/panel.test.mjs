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
  querySelectorAll(tag) { return [this, ...this.children.flatMap(child => child.querySelectorAll(tag))].filter(node => node.tag === tag); }
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
  const edit = find(panel._content, "section").querySelector("button");
  edit.click();
  const form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("description").value, description);
  assert.equal(form.elements.namedItem("start").value, event.start);
  assert.equal(form.querySelectorAll("select").length, 0);
  const cancel = form.querySelectorAll("button")[1];
  cancel.click();
  assert.equal(globalThis.focusedNode.dataset.eventId, "event");
  globalThis.focusedNode.click();
  const activeForm = find(panel._content, "form");
  await panel.saveEdit(event, activeForm);
  assert.match(find(activeForm, "p").textContent, /refresh before editing/);
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
  const siblingEdit = panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "sibling");
  assert.equal(siblingEdit.disabled, true);
  siblingEdit.click();
  assert.equal(panel._editingId, "event");
  release();
  await saving;
  assert.equal(panel._editingId, null);
  assert.equal(panel._detail.events[0].description, description);
  assert.equal(requests.at(-1).service, "get_pending");
  assert.equal(globalThis.focusedNode.dataset.eventId, "event");
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
