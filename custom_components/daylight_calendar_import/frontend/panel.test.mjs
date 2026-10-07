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
  appendChild(child) { this.children.push(child); return child; }
  prepend(...children) { this.children.unshift(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  removeAttribute(name) { delete this.attributes[name]; }
  querySelector(tag) { return this.querySelectorAll(tag)[0] || null; }
  querySelectorAll(tag) {
    if (tag.includes(" ")) {
      const [parent, child] = tag.split(" ");
      return this.querySelectorAll(parent).flatMap(node => node.querySelectorAll(child));
    }
    const nodes = [this, ...this.children.flatMap(child => child.querySelectorAll(tag))];
    if (tag.startsWith("[") && tag.endsWith("]")) {
      const attribute = tag.slice(1, -1);
      return nodes.filter(node => attribute in node.attributes);
    }
    return nodes.filter(node =>
      tag.startsWith(".") ? node.className === tag.slice(1) : node.tag === tag);
  }
  get elements() { return {namedItem: name => this.querySelectorAll("input")
    .concat(this.querySelectorAll("textarea"), this.querySelectorAll("select"))
    .find(node => node.name === name)}; }
  focus() { globalThis.focusedNode = this; }
  addEventListener(name, callback) { this[name] = callback; }
  dispatchEvent(event) { globalThis.dispatchedEvents.push({node: this, event}); return true; }
  attachShadow() { this.shadowRoot = new FakeNode("shadow"); return this.shadowRoot; }
}
globalThis.HTMLElement = FakeNode;
globalThis.dispatchedEvents = [];
globalThis.document = {
  createElement: (tag) => new FakeNode(tag),
  createElementNS: (namespace, tag) => {
    const node = new FakeNode(tag);
    node.namespace = namespace;
    return node;
  },
  createDocumentFragment: () => new FakeNode(),
};
globalThis.customElements = {define: () => {}};
const {DaylightImportPanel} = await import("./panel.js");
const find = (node, tag) => node.tag === tag ? node : node.children.map(child => find(child, tag)).find(Boolean);
const flush = () => new Promise(resolve => setImmediate(resolve));

test("mobile toolbar exposes Home Assistant sidebar navigation", async () => {
  globalThis.dispatchedEvents = [];
  const panel = new DaylightImportPanel();
  panel.narrow = true;
  const hass = {
    kioskMode: false,
    dockedSidebar: "auto",
    config: {version: "2026.7.4"},
    localize: key => key === "ui.sidebar.sidebar_toggle" ? "Open sidebar" : key,
    callWS: async () => ({response: {imports: []}}),
  };
  panel.hass = hass;
  await flush();

  const header = find(panel.shadowRoot, "header");
  const menu = panel._menuButton;
  assert.equal(header.className, "topbar");
  assert.equal(find(header, "h1").textContent, "Daylight imports");
  assert.equal(panel._refreshButton.className, "toolbar-refresh");
  assert.equal(menu.tag, "button");
  assert.equal(menu.hidden, false);
  assert.equal(menu.attributes["aria-label"], "Open sidebar");
  assert.equal(find(menu, "svg").attributes["aria-hidden"], "true");
  assert.equal(find(menu, "svg").namespace, "http://www.w3.org/2000/svg");
  assert.equal(find(menu, "path").namespace, "http://www.w3.org/2000/svg");
  assert.equal(find(menu, "path").attributes.d,
    "M3,6H21V8H3V6M3,11H21V13H3V11M3,16H21V18H3V16Z");

  menu.click();
  const [{node, event}] = globalThis.dispatchedEvents;
  assert.equal(node, panel);
  assert.equal(event.type, "hass-toggle-menu");
  assert.equal(event.bubbles, true);
  assert.equal(event.composed, true);

  panel.narrow = false;
  assert.equal(panel.narrow, false);
  assert.equal(menu.hidden, true);

  panel.hass = {...hass, dockedSidebar: "always_hidden"};
  assert.equal(menu.hidden, false);

  panel.hass = {...hass, kioskMode: true, dockedSidebar: "always_hidden"};
  assert.equal(menu.hidden, true);

  panel.narrow = true;
  panel.hass = {...hass, auth: {external: {config: {hasSidebar: true}}}};
  assert.equal(menu.hidden, false);
  assert.equal("data-own-safe-area" in panel.attributes, true);

  for (const version of ["2026.8.0", "2026.8.1", "2026.8.2", "2026.9.0"]) {
    panel.hass = {
      ...hass,
      config: {version},
      auth: {external: {config: {hasSidebar: true}}},
    };
    assert.equal(menu.hidden, false);
    assert.equal("data-own-safe-area" in panel.attributes, false);
  }

  panel.hass = {
    ...hass,
    config: {version: "2026.10.0"},
    auth: {external: {config: {hasSidebar: true}}},
  };
  assert.equal(menu.hidden, true);

  const styles = find(panel.shadowRoot, "style").textContent;
  assert.match(styles, /--safe-area-content-inset-left/);
  assert.match(styles, /--safe-area-content-inset-right/);
  assert.match(styles, /\.topbar button:focus-visible \{ outline-color: currentColor; \}/);
  assert.match(styles, /\.topbar \.menu-button[\s\S]*border: 0/);
});

test("uncertain recovery requires confirmation, retains errors, and restores review", async () => {
  const panel = new DaylightImportPanel();
  const uncertain = {id: "event", title: "Picnic", start: "2026-10-01", end: "2026-10-02",
    all_day: true, status: "write_uncertain"};
  let current = uncertain;
  let fail = true;
  const calls = [];
  panel.hass = {callWS: async request => {
    calls.push(request);
    if (request.service === "list_pending") return {response: {imports: [{id: "import"}]}};
    if (request.service === "get_pending") return {response: {pending: {id: "import", events: [current]}}};
    if (request.service === "resolve_pending_event") {
      if (fail) throw new Error("Could not persist resolution");
      current = {...uncertain, status: "pending"};
      return {response: request.service_data};
    }
  }};
  await flush();
  await panel.showImport("import");
  assert.equal(panel._content.querySelectorAll("p")
    .some(node => /Calendar write could not be confirmed/.test(node.textContent)), true);
  const choice = panel._content.querySelectorAll("button")
    .find(button => button.dataset.resolution === "not_created");
  choice.click();
  assert.equal(calls.filter(call => call.service === "resolve_pending_event").length, 0);
  panel._content.querySelectorAll("button")
    .find(button => button.textContent === "Cancel recovery").click();
  assert.equal(globalThis.focusedNode.dataset.resolution, "not_created");
  choice.click();
  assert.equal(panel._refreshButton.disabled, true);
  assert.equal(panel._content.querySelectorAll("button")
    .find(button => button.textContent === "Recent activity").disabled, true);
  await panel.runResolution(uncertain, "not_created");
  assert.match(panel._decisionError, /Could not persist resolution/);
  assert.equal(globalThis.focusedNode.attributes.role, "alert");
  assert.equal(panel._detail.events[0].status, "write_uncertain");
  fail = false;
  panel._batchResults = [{id: "event", title: "Picnic", outcome: "Approval outcome unknown"}];
  panel._batchContext = {pendingId: "import", title: "Import", action: "approve"};
  panel._content.querySelectorAll("button")
    .find(button => button.dataset.resolution === "not_created").click();
  await panel.runResolution(uncertain, "not_created");
  assert.equal(panel._detail.events[0].status, "pending");
  assert.equal(panel._decisionError, null);
  assert.deepEqual(panel._batchResults, []);
  assert.deepEqual(calls.filter(call => call.service === "resolve_pending_event")
    .map(call => call.service_data.resolution), ["not_created", "not_created"]);
});

test("confirmed recovery that cannot reload reports its saved outcome", async () => {
  const panel = new DaylightImportPanel();
  const event = {id: "event", title: "Picnic", start: "2026-10-01", end: "2026-10-02",
    all_day: true, status: "write_uncertain"};
  let saved = false;
  panel.hass = {callWS: async request => {
    if (request.service === "list_pending") {
      if (saved) throw new Error("Disconnected");
      return {response: {imports: [{id: "import"}]}};
    }
    if (request.service === "get_pending") return {response: {pending: {id: "import", events: [event]}}};
    saved = true;
    return {response: request.service_data};
  }};
  await flush();
  await panel.showImport("import");
  panel._content.querySelectorAll("button")
    .find(button => button.dataset.resolution === "created").click();
  await panel.runResolution(event, "created");
  assert.equal(saved, true);
  assert.match(panel._status, /Recovery choice saved, but the view could not reload/);
  assert.equal(panel._resolution, null);
  assert.equal(panel._selectedId, null);
  assert.equal(globalThis.focusedNode, panel._refreshButton);
});

test("lost recovery response reconciles a completed import with the inbox", async () => {
  const panel = new DaylightImportPanel();
  const event = {id: "event", title: "Picnic", start: "2026-10-01", end: "2026-10-02",
    all_day: true, status: "write_uncertain", write_attempt: "attempt-a"};
  let removed = false;
  panel.hass = {callWS: async request => {
    if (request.service === "list_pending") {
      return {response: {imports: removed ? [] : [{id: "import"}]}};
    }
    if (request.service === "get_pending") return {response: {pending: {id: "import", events: [event]}}};
    assert.deepEqual(request.service_data.expected_event, event);
    removed = true;
    throw new Error("Disconnected");
  }};
  await flush();
  await panel.showImport("import");
  panel._content.querySelectorAll("button").find(button => button.dataset.resolution === "created").click();
  await panel.runResolution(event, "created");
  assert.equal(panel._selectedId, null);
  assert.match(panel._status, /Recovery outcome unknown/);
  await panel.refresh();
  assert.equal(panel._status, "ready");
  assert.equal(panel._selectedId, null);
});

test("activity includes completed items, bounded transitions and useful empty/error views", async () => {
  const panel = new DaylightImportPanel();
  const requests = [];
  let fail = false;
  panel.hass = {callWS: async request => {
    requests.push(request.service);
    if (request.service === "list_pending") return {response: {imports: []}};
    if (request.service === "list_activity") {
      if (fail) throw new Error("Activity unavailable");
      return {response: {activity: [{id: "old", source_title: "School.pdf",
        created_at: "2026-10-01T12:00:00Z", status: "mixed", created_count: 3, rejected_count: 2}]}};
    }
    return {response: {activity: {id: "old", source_title: "School.pdf", status: "mixed",
      created_count: 3, rejected_count: 2,
      transitions: [{type: "review_ready", at: "2026-10-01T12:00:00Z", event_id: null},
        {type: "calendar_created", at: "2026-10-01T12:10:00Z", event_id: "event"}]}}};
  }};
  await flush();
  panel._content.querySelectorAll("button").find(button => button.textContent === "Recent activity").click();
  await flush();
  assert.equal(find(panel._content, "h2").textContent, "Recent activity");
  assert.match(find(find(panel._content, "li"), "p").textContent, /Calendar created: 3 · Rejected: 2/);
  assert.equal(panel._content.querySelector("button").disabled, false);
  panel._content.querySelectorAll("button").find(button => button.dataset.activityId === "old").click();
  await flush();
  assert.match(panel._content.querySelectorAll("p")[1].textContent, /Calendar created: 3 · Rejected: 2/);
  assert.match(panel._content.querySelectorAll("li")[1].textContent, /Event ID event/);
  assert.equal(globalThis.focusedNode.textContent, "School.pdf");
  panel._refreshButton.click();
  await flush();
  assert.equal(requests.at(-1), "get_activity");
  panel._content.querySelectorAll("button").find(button => button.textContent === "Back to recent activity").click();
  await flush();
  fail = true;
  await panel.showActivity();
  assert.match(find(panel._content, "p").textContent, /Activity unavailable/);
  fail = false;
  panel.showReview();
  await flush();
  assert.match(find(panel._content, "p").textContent, /No imports awaiting review/);
});

test("stale activity detail cannot overwrite a newer list", async () => {
  const panel = new DaylightImportPanel();
  let releaseDetail;
  panel.hass = {callWS: request => {
    if (request.service === "list_pending") return Promise.resolve({response: {imports: []}});
    if (request.service === "list_activity") return Promise.resolve({response: {activity: [{id: "one", title: "New",
      status: "review_ready", created_at: "2026-10-01T12:00:00Z"}]}});
    return new Promise(resolve => {releaseDetail = () => resolve({response: {activity: {
      id: "one", title: "Old", status: "failed", transitions: [],
    }}});});
  }};
  await flush();
  await panel.showActivity();
  const stale = panel.showActivity("one");
  const current = panel.showActivity();
  releaseDetail();
  await Promise.all([stale, current]);
  assert.equal(panel._activityDetail, null);
  assert.equal(panel._activityId, null);
  assert.equal(find(panel._content, "h2").textContent, "Recent activity");
});

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

test("detail renders timed events with the Home Assistant time preference", async () => {
  const panel = new DaylightImportPanel();
  const event = {id: "event", title: "Soccer Practice",
    start: "2026-10-07T20:00:00-07:00", end: "2026-10-07T21:00:00-07:00",
    all_day: false, status: "pending", confidence: 1};
  panel.hass = {
    locale: {language: "en-US", time_format: "12"},
    config: {time_zone: "America/Los_Angeles"},
    callWS: async request => request.service === "list_pending" ?
      {response: {imports: []}} : {response: {pending: {id: "one", events: [event]}}},
  };
  await flush();
  await panel.showImport("one");
  assert.equal(find(panel._content, "section").children[1].textContent.replace(/\s/g, " "),
    "Oct 7, 2026 · 8–9 PM (PDT)");
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
      default_calendar: "calendar.family",
      allowed_calendars: ["calendar.legacy", "calendar.family"],
      events: [event, sibling]}}};
    if (stale) throw {message: "Event changed since it was loaded; refresh before editing"};
    event = {...event, ...request.service_data.event,
      calendar_entity: request.service_data.calendar_entity ?? event.calendar_entity};
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
  assert.equal(form.querySelectorAll("select").length, 1);
  const calendar = form.elements.namedItem("calendar_entity");
  assert.equal(calendar.value, "calendar.legacy");
  assert.deepEqual(calendar.children.map(option => option.value),
    ["calendar.legacy", "calendar.family"]);
  calendar.value = "calendar.family";
  assert.equal(panel._content.querySelectorAll("button")[0].disabled, true);
  assert.equal(panel._content.querySelectorAll("button").some(button => button.dataset.eventId === "sibling"), false);
  siblingEdit.click();
  assert.equal(panel._editingId, "event");
  const cancel = form.querySelectorAll("button")[1];
  cancel.click();
  assert.equal(globalThis.focusedNode.dataset.eventId, "event");
  globalThis.focusedNode.click();
  const activeForm = find(panel._content, "form");
  activeForm.elements.namedItem("calendar_entity").value = "calendar.family";
  await panel.saveEdit(event, activeForm);
  assert.match(find(activeForm, "p").textContent, /refresh before editing/);
  assert.equal(globalThis.focusedNode, find(activeForm, "p"));
  assert.equal(panel._content.querySelectorAll("button")[0].disabled, true);
  assert.equal(panel._editingId, "event");
  assert.deepEqual(requests.at(-1).service_data.expected_event, event);
  assert.equal(requests.at(-1).service_data.calendar_entity, "calendar.family");
  stale = false;
  let release;
  const realCallWS = panel._hass.callWS;
  panel._hass.callWS = request => request.service === "edit_pending_event" ?
    new Promise(resolve => {release = () => resolve(realCallWS(request));}) : realCallWS(request);
  const saving = panel.saveEdit(event, activeForm);
  assert.equal(activeForm.elements.namedItem("description").disabled, true);
  assert.equal(activeForm.elements.namedItem("calendar_entity").disabled, true);
  assert.equal(panel._refreshButton.disabled, true);
  siblingEdit.click();
  assert.equal(panel._editingId, "event");
  release();
  await saving;
  assert.equal(panel._editingId, null);
  assert.equal(panel._detail.events[0].description, description);
  assert.equal(panel._detail.events[0].calendar_entity, "calendar.family");
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

test("view navigation exposes the current page and remains keyboard reachable", async () => {
  const panel = new DaylightImportPanel();
  panel.hass = {callWS: async request => request.service === "list_pending" ?
    {response: {imports: []}} : {response: {activity: []}}};
  await flush();
  const navigation = find(panel._content, "nav");
  assert.equal(navigation.attributes["aria-label"], "Daylight views");
  assert.equal(navigation.querySelectorAll("button")[0].attributes["aria-current"], "page");
  assert.equal(navigation.querySelectorAll("button")[1].disabled, false);
  navigation.querySelectorAll("button")[1].click();
  await flush();
  assert.equal(find(panel._content, "nav").querySelectorAll("button")[1].attributes["aria-current"], "page");
  assert.equal(globalThis.focusedNode.textContent, "Review inbox");
  find(panel._content, "nav").querySelectorAll("button")[0].click();
  await flush();
  assert.equal(find(panel._content, "nav").querySelectorAll("button")[0].attributes["aria-current"], "page");
  assert.equal(globalThis.focusedNode.textContent, "Refresh");
});

test("mobile buttons include padding in their full width and return focus stays put", async () => {
  const panel = new DaylightImportPanel();
  assert.match(find(panel.shadowRoot, "style").textContent, /button \{ box-sizing: border-box;/);
  panel.hass = {callWS: async request => request.service === "list_pending" ?
    {response: {imports: []}} : {response: {activity: []}}};
  await flush();
  await panel.showActivity();
  let release;
  panel.hass = {callWS: () => new Promise(resolve => {release = resolve;})};
  const returning = panel.showReview();
  assert.equal(globalThis.focusedNode, panel._refreshButton);
  const other = find(panel._content, "nav").querySelectorAll("button")[1];
  assert.equal(other.disabled, false);
  other.focus();
  panel.shadowRoot.activeElement = other;
  release({response: {imports: []}});
  await returning;
  assert.equal(globalThis.focusedNode.textContent, "Recent activity");
  assert.notEqual(globalThis.focusedNode, other);
  assert.equal(find(panel._content, "nav").querySelectorAll("button")[1], globalThis.focusedNode);
});

test("failed source activity shows actionable retry guidance", async () => {
  const panel = new DaylightImportPanel();
  panel.hass = {callWS: async request => request.service === "list_pending" ?
    {response: {imports: []}} : request.service === "list_activity" ?
      {response: {activity: [{id: "failed", status: "failed", title: "Submission",
        created_at: "2026-10-01T12:00:00Z"}]}} :
      {response: {activity: {id: "failed", status: "failed", title: "Submission",
        guidance: "Check the configured AI Task and submit the source again.", transitions: []}}}};
  await flush();
  await panel.showActivity();
  await panel.showActivity("failed");
  assert.equal(panel._content.querySelectorAll("p").some(node =>
    node.textContent.includes("submit the source again")), true);
});


test("activity renders email discovery processing duplicate and failure labels", async () => {
  const panel = new DaylightImportPanel();
  panel.hass = {callWS: async request => {
    if (request.service === "list_pending") return {response: {imports: []}};
    if (request.service === "list_activity") {
      return {response: {activity: [
        {id: "discover", title: "Email", status: "discovered",
          created_at: "2026-10-03T12:00:00Z"},
        {id: "processing", title: "School concert", status: "processing",
          created_at: "2026-10-03T12:01:00Z"},
        {id: "duplicate", title: "School concert", status: "duplicate",
          created_at: "2026-10-03T12:02:00Z"},
        {id: "failed", title: "Email", status: "failed",
          created_at: "2026-10-03T12:03:00Z"},
      ]}};
    }
    return {response: {activity: {
      id: "discover", title: "Email", status: "discovered",
      transitions: [
        {type: "received", at: "2026-10-03T11:59:00Z", event_id: null},
        {type: "discovered", at: "2026-10-03T12:00:00Z", event_id: null},
        {type: "processing", at: "2026-10-03T12:00:01Z", event_id: null},
        {type: "failed", at: "2026-10-03T12:00:02Z", event_id: null},
      ],
    }}};
  }};
  await flush();
  await panel.showActivity();

  const text = panel._content.querySelectorAll("p").map(node => node.textContent).join("\n");
  assert.match(text, /Discovered/);
  assert.match(text, /Processing/);
  assert.match(text, /Duplicate/);
  assert.match(text, /Failed/);

  panel._content.querySelectorAll("button")
    .find(button => button.dataset.activityId === "discover").click();
  await flush();
  const detail = panel._content.querySelectorAll("li").map(node => node.textContent).join("\n");
  assert.match(detail, /Discovered/);
  assert.match(detail, /Processing/);
  assert.match(detail, /Failed/);
});


test("admin can manage AI and calendars from native settings tabs", async () => {
  const calls = [];
  let current = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family", "calendar.work"],
    email: {
      enabled: false,
      host: "",
      port: 993,
      username: "",
      password_configured: false,
      mailbox: "INBOX",
      verify_ssl: true,
    },
  };
  const hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
      "calendar.work": {
        entity_id: "calendar.work", state: "off",
        attributes: {friendly_name: "Work", supported_features: 1},
      },
      "calendar.read_only": {
        entity_id: "calendar.read_only", state: "off",
        attributes: {friendly_name: "Read only", supported_features: 0},
      },
    },
    callWS: async message => {
      calls.push(message);
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return current;
      if (message.type === "daylight_calendar_import/settings/core/update") {
        current = {
          ...current,
          ...Object.fromEntries(
            ["ai_task_entity", "calendar_entity", "calendar_entities"]
              .filter(key => message[key] !== undefined)
              .map(key => [key, message[key]]),
          ),
        };
        return current;
      }
      throw new Error(`Unexpected call: ${message.type}`);
    },
  };

  const panel = new DaylightImportPanel();
  panel.hass = hass;
  await flush();
  const settingsButton = panel._content.querySelectorAll("button")
    .find(button => button.textContent === "Settings");
  assert.ok(settingsButton);
  settingsButton.click();
  await flush();

  assert.equal(find(panel._content, "h2").textContent, "General");
  assert.equal(panel._content.querySelectorAll("nav")[0].querySelectorAll("button")[2]
    .attributes["aria-current"], "page");
  const generalForm = find(panel._content, "form");
  const aiSelect = generalForm.elements.namedItem("ai_task_entity");
  assert.equal(aiSelect.value, "ai_task.openai");
  assert.deepEqual(aiSelect.children.map(option => option.value), [
    "ai_task.google", "ai_task.openai",
  ]);
  aiSelect.value = "ai_task.google";
  generalForm.submit({preventDefault() {}});
  await flush();
  assert.equal(current.ai_task_entity, "ai_task.google");
  assert.equal(panel._settingsError, null);
  assert.equal(panel._settings.calendar_entity, "calendar.family");
  assert.deepEqual(panel._settings.calendar_entities, ["calendar.family", "calendar.work"]);
  assert.equal(calls.at(-1).type, "daylight_calendar_import/settings/core/update");

  const settingsTabs = panel._content.querySelector(".settings-tabs");
  settingsTabs.querySelectorAll("button")
    .find(button => button.textContent === "Calendars").click();
  assert.equal(find(panel._content, "h2").textContent, "Calendars");
  const calendarForm = find(panel._content, "form");
  const defaultSelect = calendarForm.elements.namedItem("calendar_entity");
  assert.equal(defaultSelect.value, "calendar.family");
  assert.equal(
    defaultSelect.children.some(option => option.value === "calendar.read_only"),
    false,
  );
  defaultSelect.value = "calendar.work";
  for (const input of calendarForm.querySelectorAll("input")) {
    input.checked = input.value === "calendar.work";
  }
  calendarForm.submit({preventDefault() {}});
  await flush();
  assert.equal(current.calendar_entity, "calendar.work");
  assert.deepEqual(current.calendar_entities, ["calendar.work"]);
  assert.equal(current.ai_task_entity, "ai_task.google");
  assert.equal(panel._settingsError, null);
  assert.equal(find(panel._content, "h2").textContent, "Calendars");
});

test("non-admin panel does not expose native settings navigation", async () => {
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: false},
    callWS: async () => ({response: {imports: []}}),
  };
  await flush();

  assert.equal(
    panel._content.querySelectorAll("button")
      .some(button => button.textContent === "Settings"),
    false,
  );
});


test("settings save failures focus an alert that is programmatically focusable", async () => {
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {
      enabled: false,
      host: "",
      port: 993,
      username: "",
      password_configured: false,
      mailbox: "INBOX",
      verify_ssl: true,
    },
  };
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      if (message.type === "daylight_calendar_import/settings/core/update") {
        throw new Error("Save rejected");
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  const form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  await panel.saveGeneralSettings(form);

  const alert = panel._content.querySelector(".error");
  assert.equal(alert.attributes.role, "alert");
  assert.equal(alert.tabIndex, -1);
  assert.equal(globalThis.focusedNode, alert);
});

test("settings calendar labels can shrink and wrap on narrow panels", () => {
  const panel = new DaylightImportPanel();
  const styles = find(panel.shadowRoot, "style").textContent;
  assert.match(styles, /\.settings-card fieldset \{[\s\S]*?min-width: 0/);
  assert.match(styles,
    /\.settings-card fieldset label span \{[\s\S]*?min-width: 0;[\s\S]*?overflow-wrap: anywhere;/);
});


test("settings forms freeze every editable control while a save is pending", async () => {
  let resolveSave;
  const savePending = new Promise(resolve => { resolveSave = resolve; });
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family", "calendar.work"],
    email: {
      enabled: false,
      host: "",
      port: 993,
      username: "",
      password_configured: false,
      mailbox: "INBOX",
      verify_ssl: true,
    },
  };
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
      "calendar.work": {
        entity_id: "calendar.work", state: "off",
        attributes: {friendly_name: "Work", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      if (message.type === "daylight_calendar_import/settings/core/update") return savePending;
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  const form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  const saving = panel.saveGeneralSettings(form);
  await flush();

  const busyForm = find(panel._content, "form");
  assert.equal(busyForm.attributes["aria-busy"], "true");
  assert.equal(busyForm.elements.namedItem("ai_task_entity").disabled, true);
  assert.equal(find(busyForm, "button").disabled, true);
  assert.equal(
    panel._content.querySelector(".settings-tabs").querySelectorAll("button")
      .every(button => button.disabled === true),
    true,
  );

  resolveSave({...snapshot, ai_task_entity: "ai_task.google"});
  await saving;

  const savedForm = find(panel._content, "form");
  assert.notEqual(savedForm.attributes["aria-busy"], "true");
  assert.notEqual(savedForm.elements.namedItem("ai_task_entity").disabled, true);
});

test("calendar settings freeze select and checkboxes while saving", async () => {
  const panel = new DaylightImportPanel();
  panel._hass = {
    states: {
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
      "calendar.work": {
        entity_id: "calendar.work", state: "off",
        attributes: {friendly_name: "Work", supported_features: 1},
      },
    },
  };
  panel._settings = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family", "calendar.work"],
    email: {},
  };
  panel._settingsTab = "calendars";
  panel._settingsSaving = true;
  panel._status = "ready";
  panel._view = "settings";
  panel.render();

  const form = find(panel._content, "form");
  assert.equal(form.attributes["aria-busy"], "true");
  assert.equal(form.elements.namedItem("calendar_entity").disabled, true);
  assert.equal(
    form.querySelectorAll("input")
      .filter(input => input.type === "checkbox")
      .every(input => input.disabled === true),
    true,
  );
  assert.equal(find(form, "button").disabled, true);
});

test("reload_failed refreshes persisted settings and keeps restart warning", async () => {
  let persisted = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family", "calendar.work"],
    email: {
      enabled: false,
      host: "",
      port: 993,
      username: "",
      password_configured: false,
      mailbox: "INBOX",
      verify_ssl: true,
    },
  };
  let failReload = true;
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
      "calendar.work": {
        entity_id: "calendar.work", state: "off",
        attributes: {friendly_name: "Work", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return persisted;
      if (message.type === "daylight_calendar_import/settings/core/update") {
        persisted = {...persisted, ai_task_entity: message.ai_task_entity};
        if (failReload) {
          failReload = false;
          throw {
            code: "reload_failed",
            message: "Settings were saved, but Daylight could not reload. Restart Home Assistant.",
          };
        }
        return persisted;
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  let form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  await panel.saveGeneralSettings(form);

  assert.equal(panel._settings.ai_task_entity, "ai_task.google");
  assert.match(panel._settingsReloadWarning, /Restart Home Assistant/);
  assert.equal(panel._settingsDrafts.general, null);
  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("ai_task_entity").value, "ai_task.google");
  assert.equal(
    panel._content.querySelectorAll("p")
      .some(node => /Restart Home Assistant/.test(node.textContent)),
    true,
  );

  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "Calendars").click();
  assert.match(panel._settingsReloadWarning, /Restart Home Assistant/);
  assert.equal(
    panel._content.querySelectorAll("p")
      .some(node => /Restart Home Assistant/.test(node.textContent)),
    true,
  );

  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "General").click();
  form = find(panel._content, "form");
  await panel.saveGeneralSettings(form);
  assert.match(panel._settingsReloadWarning, /Restart Home Assistant/);
});


test("settings pending save exposes and focuses a busy status", async () => {
  let resolveSave;
  const pending = new Promise(resolve => { resolveSave = resolve; });
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      if (message.type === "daylight_calendar_import/settings/core/update") return pending;
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  const form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  const saving = panel.saveGeneralSettings(form);
  await flush();

  const status = panel._content.querySelector("[data-settings-saving]");
  assert.ok(status);
  assert.equal(status.attributes.role, "status");
  assert.equal(status.tabIndex, -1);
  assert.equal(globalThis.focusedNode, status);

  resolveSave({...snapshot, ai_task_entity: "ai_task.google"});
  await saving;
});

test("reload_failed keeps submitted patch when reconciliation also fails", async () => {
  let getCount = 0;
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") {
        getCount += 1;
        if (getCount === 1) return snapshot;
        throw new Error("Transient settings refresh failure");
      }
      if (message.type === "daylight_calendar_import/settings/core/update") {
        throw {
          code: "reload_failed",
          message: "Settings were saved, but Daylight could not reload. Restart Home Assistant.",
        };
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  let form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  await panel.saveGeneralSettings(form);

  assert.equal(panel._settings.ai_task_entity, "ai_task.google");
  assert.equal(panel._settingsDrafts.general, null);
  assert.match(panel._settingsError, /Transient settings refresh failure/);

  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "Calendars").click();
  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "General").click();

  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("ai_task_entity").value, "ai_task.google");
});

test("new save error receives focus ahead of an older reload warning", async () => {
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  let updateCount = 0;
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") {
        return {...snapshot, ai_task_entity: "ai_task.google"};
      }
      if (message.type === "daylight_calendar_import/settings/core/update") {
        updateCount += 1;
        if (updateCount === 1) {
          throw {
            code: "reload_failed",
            message: "Settings were saved, but Daylight could not reload. Restart Home Assistant.",
          };
        }
        throw {code: "invalid_format", message: "Save rejected"};
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  let form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  await panel.saveGeneralSettings(form);
  assert.match(panel._settingsReloadWarning, /Restart Home Assistant/);

  form = find(panel._content, "form");
  await panel.saveGeneralSettings(form);

  const currentError = panel._content.querySelector("[data-settings-save-error]");
  const oldWarning = panel._content.querySelector("[data-settings-reload-warning]");
  assert.ok(currentError);
  assert.ok(oldWarning);
  assert.equal(globalThis.focusedNode, currentError);
});

test("successful changed save clears restart warning but no-op save preserves it", async () => {
  let current = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  let updateCount = 0;
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return current;
      if (message.type === "daylight_calendar_import/settings/core/update") {
        updateCount += 1;
        current = {...current, ai_task_entity: message.ai_task_entity};
        if (updateCount === 1) {
          throw {
            code: "reload_failed",
            message: "Settings were saved, but Daylight could not reload. Restart Home Assistant.",
          };
        }
        return current;
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  let form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  await panel.saveGeneralSettings(form);
  assert.match(panel._settingsReloadWarning, /Restart Home Assistant/);

  form = find(panel._content, "form");
  await panel.saveGeneralSettings(form);
  assert.match(panel._settingsReloadWarning, /Restart Home Assistant/);

  form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.openai";
  await panel.saveGeneralSettings(form);
  assert.equal(panel._settingsReloadWarning, null);
});


test("initial settings load immediately focuses and announces loading state", async () => {
  let resolveSettings;
  const pending = new Promise(resolve => { resolveSettings = resolve; });
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel._hass = {
    user: {is_admin: true},
    states: {},
    callWS: async message => {
      if (message.type === "daylight_calendar_import/settings/get") return pending;
      throw new Error("Unexpected request");
    },
  };
  panel._announcement.replaceChildren(Object.assign(new FakeNode("span"), {
    textContent: "Inbox loaded",
  }));

  const loading = panel.showSettings();
  const status = panel._content.querySelector("[data-settings-loading]");
  assert.ok(status);
  assert.equal(status.attributes.role, "status");
  assert.equal(status.tabIndex, -1);
  assert.equal(globalThis.focusedNode, status);
  assert.equal(panel._announcement.children[0].textContent, "Loading settings…");

  resolveSettings(snapshot);
  await loading;

  assert.deepEqual(panel._announcement.children, []);
  assert.equal(find(panel._content, "h2").textContent, "General");
});

test("settings failure clears an earlier save announcement", async () => {
  let updateCount = 0;
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      if (message.type === "daylight_calendar_import/settings/core/update") {
        updateCount += 1;
        if (updateCount === 1) {
          return {...snapshot, ai_task_entity: "ai_task.google"};
        }
        throw new Error("Save rejected");
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  let form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  await panel.saveGeneralSettings(form);
  assert.equal(panel._announcement.children[0].textContent, "General settings saved");

  form = find(panel._content, "form");
  await panel.saveGeneralSettings(form);

  assert.deepEqual(panel._announcement.children, []);
  assert.match(panel._settingsError, /Save rejected/);
});

test("calendar validation focuses current error ahead of restart warning", async () => {
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family", "calendar.work"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
      "calendar.work": {
        entity_id: "calendar.work", state: "off",
        attributes: {friendly_name: "Work", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings("calendars");
  panel._settingsReloadWarning =
    "Settings were saved, but Daylight could not reload. Restart Home Assistant.";
  panel._announcement.replaceChildren(Object.assign(new FakeNode("span"), {
    textContent: "Calendar settings saved",
  }));
  panel.render();

  const form = find(panel._content, "form");
  form.elements.namedItem("calendar_entity").value = "calendar.family";
  for (const input of form.querySelectorAll("input")) {
    input.checked = input.value === "calendar.work";
  }
  await panel.saveCalendarSettings(form);

  const currentError = panel._content.querySelector("[data-settings-save-error]");
  const oldWarning = panel._content.querySelector("[data-settings-reload-warning]");
  assert.ok(currentError);
  assert.ok(oldWarning);
  assert.match(currentError.textContent, /default calendar must also be selected/);
  assert.equal(globalThis.focusedNode, currentError);
  assert.deepEqual(panel._announcement.children, []);
});


test("cached settings entry clears stale announcements without reloading", async () => {
  const panel = new DaylightImportPanel();
  panel._hass = {user: {is_admin: true}, states: {}};
  panel._settings = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {},
  };
  panel._announcement.replaceChildren(Object.assign(new FakeNode("span"), {
    textContent: "Inbox loaded",
  }));

  await panel.showSettings();

  assert.deepEqual(panel._announcement.children, []);
  assert.equal(find(panel._content, "h2").textContent, "General");
  assert.equal(globalThis.focusedNode, find(panel._content, "h2"));
});


test("ambiguous connection loss reconciles a persisted settings patch", async () => {
  let current = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  let getCount = 0;
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") {
        getCount += 1;
        return current;
      }
      if (message.type === "daylight_calendar_import/settings/core/update") {
        current = {...current, ai_task_entity: message.ai_task_entity};
        throw 3; // home-assistant-js-websocket ERR_CONNECTION_LOST
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  const form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  await panel.saveGeneralSettings(form);

  assert.equal(getCount, 2);
  assert.equal(panel._settings.ai_task_entity, "ai_task.google");
  assert.equal(panel._settingsDrafts.general, null);
  assert.equal(panel._settingsError, null);
  assert.equal(panel._announcement.children[0].textContent, "General settings saved");
});

test("ambiguous connection loss keeps draft when server confirms no write", async () => {
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family", "calendar.work"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
      "calendar.work": {
        entity_id: "calendar.work", state: "off",
        attributes: {friendly_name: "Work", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      if (message.type === "daylight_calendar_import/settings/core/update") throw 3;
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  let form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  await panel.saveGeneralSettings(form);

  assert.equal(panel._settings.ai_task_entity, "ai_task.openai");
  assert.deepEqual(panel._settingsDrafts.general, {ai_task_entity: "ai_task.google"});
  assert.match(panel._settingsError, /Could not save general settings/);

  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "Calendars").click();
  form = find(panel._content, "form");
  form.elements.namedItem("calendar_entity").value = "calendar.family";
  for (const input of form.querySelectorAll("input")) {
    input.checked = input.value === "calendar.work";
  }
  await panel.saveCalendarSettings(form);
  assert.ok(panel._settingsDrafts.calendars);

  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "General").click();
  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("ai_task_entity").value, "ai_task.google");
  assert.deepEqual(panel._settingsDrafts.general, {ai_task_entity: "ai_task.google"});
});

test("ambiguous connection loss retains draft when reconciliation also fails", async () => {
  let getCount = 0;
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") {
        getCount += 1;
        if (getCount === 1) return snapshot;
        throw 3;
      }
      if (message.type === "daylight_calendar_import/settings/core/update") throw 3;
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  let form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  await panel.saveGeneralSettings(form);

  assert.deepEqual(panel._settingsDrafts.general, {ai_task_entity: "ai_task.google"});
  assert.match(panel._settingsError, /could not confirm whether the change was saved/i);

  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "Calendars").click();
  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "General").click();
  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("ai_task_entity").value, "ai_task.google");
});

test("coded Core save errors are definitive and skip reconciliation", async () => {
  let getCount = 0;
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "ai_task.openai": {
        entity_id: "ai_task.openai", state: "idle",
        attributes: {friendly_name: "OpenAI", supported_features: 1},
      },
      "ai_task.google": {
        entity_id: "ai_task.google", state: "idle",
        attributes: {friendly_name: "Google", supported_features: 1},
      },
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") {
        getCount += 1;
        return snapshot;
      }
      if (message.type === "daylight_calendar_import/settings/core/update") {
        throw {code: "invalid_format", message: "Rejected by Home Assistant"};
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  const form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  await panel.saveGeneralSettings(form);

  assert.equal(getCount, 1);
  assert.match(panel._settingsError, /Rejected by Home Assistant/);
  assert.deepEqual(panel._settingsDrafts.general, {ai_task_entity: "ai_task.google"});
});
