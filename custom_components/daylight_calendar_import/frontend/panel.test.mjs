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

test("email source context shows title, sender, and received date without raw attachment hashes", async () => {
  const panel = new DaylightImportPanel();
  const event = {id: "event", title: "Birthday",
    start: "2026-12-05T18:00:00-08:00", end: "2026-12-05T20:00:00-08:00",
    all_day: false, status: "pending", confidence: 1};
  panel.hass = {
    locale: {language: "en-US", time_format: "12"},
    config: {time_zone: "America/Los_Angeles"},
    callWS: async request => request.service === "list_pending" ?
      {response: {imports: []}} : {response: {pending: {
        id: "one",
        created_at: "2026-10-03T19:00:00+00:00",
        source_kind: "email",
        source_title: "Mel's 40th Birthday",
        source_sender: "Megan Example <megan@example.test>",
        source_text: "Email attachment: 3491.png (SHA-256: secret-hash)",
        warnings: [],
        events: [event],
      }}},
  };
  await flush();
  await panel.showImport("one");

  const source = panel._content.querySelector(".source");
  assert.equal(source.tag, "div");
  assert.equal(source.tabIndex, 0);
  assert.equal(source.attributes.role, "region");
  assert.equal(source.attributes["aria-label"], "Email source details");
  assert.equal(source.children[0].textContent, "Email title: Mel's 40th Birthday");
  assert.equal(source.children[1].textContent,
    "Email sender: Megan Example <megan@example.test>");
  assert.match(source.children[2].textContent, /^Received: .*Oct 3, 2026/);
  assert.equal(source.children.some(child => /SHA-256/.test(child.textContent || "")), false);
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
  assert.equal(form.elements.namedItem("start").value, "2026-10-01T10:00");
  assert.equal(form.elements.namedItem("start").type, "datetime-local");
  assert.equal(form.elements.namedItem("end").type, "datetime-local");
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
  assert.equal(activeForm.elements.namedItem("start").disabled, false);
  assert.equal(activeForm.elements.namedItem("start").required, true);
  assert.equal(activeForm.elements.namedItem("start_date").disabled, true);
  assert.equal(activeForm.elements.namedItem("start_date").required, false);
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

test("editor matches the card default date/time picker and preserves duration", async () => {
  const panel = new DaylightImportPanel();
  const timed = {id: "timed", title: "Dinner",
    start: "2026-12-05T18:00:00-08:00", end: "2026-12-05T20:00:00-08:00",
    all_day: false, status: "pending", confidence: 0.9};
  const allDayEvent = {id: "all-day", title: "Trip",
    start: "2026-12-10", end: "2026-12-13",
    all_day: true, status: "pending", confidence: 0.8};
  const calls = [];
  panel.hass = {
    config: {time_zone: "America/Los_Angeles"},
    locale: {language: "en-US", time_format: "12"},
    callWS: async request => {
      calls.push(request);
      if (request.service === "list_pending") return {response: {imports: []}};
      if (request.service === "get_pending") return {response: {pending: {
        id: "one", events: [timed, allDayEvent],
      }}};
      return {response: {
        pending_id: "one",
        event: {...timed, ...request.service_data.event},
      }};
    },
  };
  await flush();
  await panel.showImport("one");

  panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "timed").click();
  let form = find(panel._content, "form");
  const start = form.elements.namedItem("start");
  const end = form.elements.namedItem("end");
  const startDate = form.elements.namedItem("start_date");
  const endDate = form.elements.namedItem("end_date");
  assert.equal(start.type, "datetime-local");
  assert.equal(end.type, "datetime-local");
  assert.equal(start.step, "60");
  assert.equal(end.step, "60");
  assert.equal(start.value, "2026-12-05T18:00");
  assert.equal(end.value, "2026-12-05T20:00");
  assert.equal(start.required, true);
  assert.equal(end.required, true);
  assert.equal(start.disabled, false);
  assert.equal(startDate.type, "date");
  assert.equal(endDate.type, "date");
  assert.equal(startDate.required, false);
  assert.equal(startDate.disabled, true);
  assert.equal(form.querySelector(".timed-event-fields").hidden, false);
  assert.equal(form.querySelector(".all-day-event-fields").hidden, true);

  start.value = "2026-12-05T19:30";
  start.change();
  assert.equal(end.value, "2026-12-05T21:30");

  const allDay = form.elements.namedItem("all_day");
  allDay.checked = true;
  allDay.change();
  assert.equal(form.querySelector(".timed-event-fields").hidden, true);
  assert.equal(form.querySelector(".all-day-event-fields").hidden, false);
  assert.equal(start.disabled, true);
  assert.equal(start.required, false);
  assert.equal(startDate.disabled, false);
  assert.equal(startDate.required, true);
  form.elements.namedItem("start_date").value = "2026-12-24";
  form.elements.namedItem("end_date").value = "2026-12-26";
  await panel.saveEdit(timed, form);
  const editCall = calls.findLast(call => call.service === "edit_pending_event");
  assert.equal(editCall.service_data.event.all_day, true);
  assert.equal(editCall.service_data.event.start, "2026-12-24");
  assert.equal(editCall.service_data.event.end, "2026-12-27");

  await panel.showImport("one");
  panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "all-day").click();
  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("start_date").value, "2026-12-10");
  assert.equal(form.elements.namedItem("end_date").value, "2026-12-12");
  assert.equal(form.elements.namedItem("start").disabled, true);
  assert.equal(form.elements.namedItem("start_date").disabled, false);
});

test("all-day toggles carry the currently edited range in both directions", async () => {
  const panel = new DaylightImportPanel();
  const timed = {
    id: "timed-toggle",
    title: "Dinner",
    start: "2026-12-05T18:00:00-08:00",
    end: "2026-12-05T20:00:00-08:00",
    all_day: false,
    status: "pending",
    confidence: 0.9,
  };
  const allDayEvent = {
    id: "all-day-toggle",
    title: "Trip",
    start: "2026-12-10",
    end: "2026-12-13",
    all_day: true,
    status: "pending",
    confidence: 0.8,
  };
  const calls = [];
  panel.hass = {
    config: {time_zone: "America/Los_Angeles"},
    callWS: async request => {
      calls.push(request);
      if (request.service === "list_pending") return {response: {imports: []}};
      if (request.service === "get_pending") {
        return {response: {pending: {id: "one", events: [timed, allDayEvent]}}};
      }
      return {response: {
        pending_id: "one",
        event: {
          ...request.service_data.expected_event,
          ...request.service_data.event,
        },
      }};
    },
  };
  await flush();
  await panel.showImport("one");

  panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "timed-toggle").click();
  let form = find(panel._content, "form");
  let start = form.elements.namedItem("start");
  let end = form.elements.namedItem("end");
  let allDay = form.elements.namedItem("all_day");
  start.value = "2026-12-24T19:30";
  start.change();
  assert.equal(end.value, "2026-12-24T21:30");

  allDay.checked = true;
  allDay.change();
  assert.equal(form.elements.namedItem("start_date").value, "2026-12-24");
  assert.equal(form.elements.namedItem("end_date").value, "2026-12-24");
  await panel.saveEdit(timed, form);

  let editCall = calls.findLast(call => call.service === "edit_pending_event");
  assert.equal(editCall.service_data.event.all_day, true);
  assert.equal(editCall.service_data.event.start, "2026-12-24");
  assert.equal(editCall.service_data.event.end, "2026-12-25");

  await panel.showImport("one");
  panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "all-day-toggle").click();
  form = find(panel._content, "form");
  const startDate = form.elements.namedItem("start_date");
  const endDate = form.elements.namedItem("end_date");
  startDate.value = "2026-12-24";
  startDate.change();
  assert.equal(endDate.value, "2026-12-26");

  allDay = form.elements.namedItem("all_day");
  allDay.checked = false;
  allDay.change();
  start = form.elements.namedItem("start");
  end = form.elements.namedItem("end");
  assert.equal(start.value, "2026-12-24T00:00");
  assert.equal(end.value, "2026-12-27T00:00");

  start.value = "2026-12-25T00:00";
  start.change();
  assert.equal(end.value, "2026-12-28T00:00");
  await panel.saveEdit(allDayEvent, form);

  editCall = calls.findLast(call => call.service === "edit_pending_event");
  assert.equal(editCall.service_data.event.all_day, false);
  assert.equal(editCall.service_data.event.start, "2026-12-25T00:00:00-08:00");
  assert.equal(editCall.service_data.event.end, "2026-12-28T00:00:00-08:00");
});

test("invalid active ranges block mode switches instead of exposing stale values", async () => {
  const panel = new DaylightImportPanel();
  const timed = {
    id: "timed-invalid",
    title: "Timed",
    start: "2026-12-05T18:00:00-08:00",
    end: "2026-12-05T20:00:00-08:00",
    all_day: false,
    status: "pending",
    confidence: 1,
  };
  const allDayEvent = {
    id: "all-day-invalid",
    title: "All day",
    start: "2026-12-10",
    end: "2026-12-13",
    all_day: true,
    status: "pending",
    confidence: 1,
  };
  panel.hass = {
    config: {time_zone: "America/Los_Angeles"},
    callWS: async request => {
      if (request.service === "list_pending") return {response: {imports: []}};
      return {response: {pending: {id: "one", events: [timed, allDayEvent]}}};
    },
  };
  await flush();
  await panel.showImport("one");

  panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "timed-invalid").click();
  let form = find(panel._content, "form");
  form.elements.namedItem("start").value = "2026-12-05T21:00";
  form.elements.namedItem("end").value = "2026-12-05T20:00";
  let allDay = form.elements.namedItem("all_day");
  allDay.checked = true;
  allDay.change();

  assert.equal(allDay.checked, false);
  assert.equal(form.querySelector(".timed-event-fields").hidden, false);
  assert.equal(form.querySelector(".all-day-event-fields").hidden, true);
  assert.match(find(form, "p").textContent, /after start/i);

  panel._editingId = null;
  panel.render();
  panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "all-day-invalid").click();
  form = find(panel._content, "form");
  form.elements.namedItem("start_date").value = "2026-12-26";
  form.elements.namedItem("end_date").value = "2026-12-24";
  allDay = form.elements.namedItem("all_day");
  allDay.checked = false;
  allDay.change();

  assert.equal(allDay.checked, true);
  assert.equal(form.querySelector(".timed-event-fields").hidden, true);
  assert.equal(form.querySelector(".all-day-event-fields").hidden, false);
  assert.match(find(form, "p").textContent, /fix the start and end dates/i);
});

test("timed editor preserves original offsets and follows the Home Assistant zone after edits", async () => {
  const panel = new DaylightImportPanel();
  let current = {id: "event", title: "DST test",
    start: "2026-03-08T01:30:00-08:00", end: "2026-03-08T03:30:00-07:00",
    all_day: false, status: "pending", confidence: 1};
  const calls = [];
  panel.hass = {
    config: {time_zone: "America/Los_Angeles"},
    callWS: async request => {
      calls.push(request);
      if (request.service === "list_pending") return {response: {imports: []}};
      if (request.service === "get_pending") return {response: {pending: {id: "one", events: [current]}}};
      current = {...current, ...request.service_data.event};
      return {response: {pending_id: "one", event: current}};
    },
  };
  await flush();
  await panel.showImport("one");
  panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "event").click();
  let form = find(panel._content, "form");
  await panel.saveEdit(current, form);
  let editCall = calls.findLast(call => call.service === "edit_pending_event");
  assert.equal(editCall.service_data.event.start, "2026-03-08T01:30:00-08:00");
  assert.equal(editCall.service_data.event.end, "2026-03-08T03:30:00-07:00");

  panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "event").click();
  form = find(panel._content, "form");
  form.elements.namedItem("start").value = "2026-03-08T04:30";
  form.elements.namedItem("end").value = "2026-03-08T05:30";
  await panel.saveEdit(current, form);
  editCall = calls.findLast(call => call.service === "edit_pending_event");
  assert.equal(editCall.service_data.event.start, "2026-03-08T04:30:00-07:00");
  assert.equal(editCall.service_data.event.end, "2026-03-08T05:30:00-07:00");
});

test("fixed-offset events preserve the entire range model during duration sync", async () => {
  const panel = new DaylightImportPanel();
  let current = {
    id: "fixed",
    title: "Fixed offset across DST",
    start: "2026-03-07T10:00:00-08:00",
    end: "2026-03-09T10:00:00-08:00",
    all_day: false,
    status: "pending",
    confidence: 1,
  };
  const calls = [];
  panel.hass = {
    config: {time_zone: "America/Los_Angeles"},
    callWS: async request => {
      calls.push(request);
      if (request.service === "list_pending") return {response: {imports: []}};
      if (request.service === "get_pending") {
        return {response: {pending: {id: "one", events: [current]}}};
      }
      current = {...current, ...request.service_data.event};
      return {response: {pending_id: "one", event: current}};
    },
  };
  await flush();
  await panel.showImport("one");
  panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "fixed").click();
  const form = find(panel._content, "form");
  const start = form.elements.namedItem("start");
  const end = form.elements.namedItem("end");
  start.value = "2026-03-09T10:00";
  start.change();
  assert.equal(end.value, "2026-03-11T10:00");

  await panel.saveEdit(current, form);
  const call = calls.findLast(request => request.service === "edit_pending_event");
  assert.equal(call.service_data.event.start, "2026-03-09T10:00:00-08:00");
  assert.equal(call.service_data.event.end, "2026-03-11T10:00:00-08:00");
  assert.equal(
    new Date(call.service_data.event.end) - new Date(call.service_data.event.start),
    48 * 60 * 60 * 1000,
  );
});

test("duration sync preserves the generated occurrence inside a fall-back fold", async () => {
  const panel = new DaylightImportPanel();
  let current = {
    id: "event",
    title: "Fold event",
    start: "2026-11-01T01:30:00-07:00",
    end: "2026-11-01T01:30:00-08:00",
    all_day: false,
    status: "pending",
    confidence: 1,
  };
  const calls = [];
  panel.hass = {
    config: {time_zone: "America/Los_Angeles"},
    callWS: async request => {
      calls.push(request);
      if (request.service === "list_pending") return {response: {imports: []}};
      if (request.service === "get_pending") {
        return {response: {pending: {id: "one", events: [current]}}};
      }
      current = {...current, ...request.service_data.event};
      return {response: {pending_id: "one", event: current}};
    },
  };
  await flush();
  await panel.showImport("one");
  panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "event").click();

  const form = find(panel._content, "form");
  const start = form.elements.namedItem("start");
  const end = form.elements.namedItem("end");
  assert.equal(start.value, "2026-11-01T01:30");
  assert.equal(end.value, "2026-11-01T01:30");

  start.value = "2026-11-01T00:30";
  start.change();
  assert.equal(end.value, "2026-11-01T01:30");

  const allDay = form.elements.namedItem("all_day");
  allDay.checked = true;
  allDay.change();
  assert.equal(form.elements.namedItem("start_date").value, "2026-11-01");
  assert.equal(form.elements.namedItem("end_date").value, "2026-11-01");
  allDay.checked = false;
  allDay.change();
  assert.equal(start.value, "2026-11-01T00:30");
  assert.equal(end.value, "2026-11-01T01:30");

  await panel.saveEdit(current, form);

  const editCall = calls.findLast(call => call.service === "edit_pending_event");
  assert.equal(editCall.service_data.event.start, "2026-11-01T00:30:00-07:00");
  assert.equal(editCall.service_data.event.end, "2026-11-01T01:30:00-07:00");
  assert.equal(
    new Date(editCall.service_data.event.end).getTime() -
      new Date(editCall.service_data.event.start).getTime(),
    60 * 60 * 1000,
  );
});

test("editor rejects nonexistent local DST times before sending a save request", async () => {
  const panel = new DaylightImportPanel();
  const event = {id: "event", title: "DST gap",
    start: "2026-03-08T01:30:00-08:00", end: "2026-03-08T03:30:00-07:00",
    all_day: false, status: "pending", confidence: 1};
  const calls = [];
  panel.hass = {
    config: {time_zone: "America/Los_Angeles"},
    callWS: async request => {
      calls.push(request);
      if (request.service === "list_pending") return {response: {imports: []}};
      return {response: {pending: {id: "one", events: [event]}}};
    },
  };
  await flush();
  await panel.showImport("one");
  panel._content.querySelectorAll("button")
    .find(button => button.dataset.eventId === "event").click();
  const form = find(panel._content, "form");
  form.elements.namedItem("start").value = "2026-03-08T02:30";
  form.elements.namedItem("end").value = "2026-03-08T04:30";

  await panel.saveEdit(event, form);

  assert.equal(
    calls.filter(call => call.service === "edit_pending_event").length,
    0,
  );
  const error = find(form, "p");
  assert.match(error.textContent, /does not exist/);
  assert.equal(error.attributes.role, "alert");
  assert.equal(globalThis.focusedNode, error);
  assert.equal(panel._saving, false);

  form.elements.namedItem("start").value = "2026-03-08T04:30";
  form.elements.namedItem("end").value = "2026-03-08T05:30";
  const retry = panel.saveEdit(event, form);
  assert.equal(form.querySelector(".error"), null);
  await retry;
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
  assert.match(panel._settingsReloadWarning, /could not confirm that the integration reloaded/i);
  assert.equal(panel._announcement.children[0].textContent, "General settings saved");
  assert.equal(
    globalThis.focusedNode,
    panel._content.querySelector("[data-settings-reload-warning]"),
  );
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


test("successful save in one settings tab preserves another tab's unconfirmed draft", async () => {
  let current = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family", "calendar.work"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  let generalFailed = false;
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
      if (message.type === "daylight_calendar_import/settings/get") return current;
      if (message.type === "daylight_calendar_import/settings/core/update") {
        if (message.ai_task_entity !== undefined && !generalFailed) {
          generalFailed = true;
          throw 3;
        }
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
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  let form = find(panel._content, "form");
  form.elements.namedItem("ai_task_entity").value = "ai_task.google";
  await panel.saveGeneralSettings(form);
  assert.deepEqual(panel._settingsDrafts.general, {ai_task_entity: "ai_task.google"});

  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "Calendars").click();
  form = find(panel._content, "form");
  form.elements.namedItem("calendar_entity").value = "calendar.work";
  for (const input of form.querySelectorAll("input")) {
    input.checked = input.value === "calendar.work";
  }
  await panel.saveCalendarSettings(form);

  assert.equal(panel._settingsDrafts.calendars, null);
  assert.deepEqual(panel._settingsDrafts.general, {ai_task_entity: "ai_task.google"});

  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "General").click();
  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("ai_task_entity").value, "ai_task.google");
});


test("unsaved General selection survives settings tab switches", async () => {
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
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  let form = find(panel._content, "form");
  const ai = form.elements.namedItem("ai_task_entity");
  ai.value = "ai_task.google";
  ai.change();

  assert.deepEqual(panel._settingsDrafts.general, {ai_task_entity: "ai_task.google"});
  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "Calendars").click();
  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "General").click();

  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("ai_task_entity").value, "ai_task.google");
});

test("unsaved Calendar selections survive settings tab switches", async () => {
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

  let form = find(panel._content, "form");
  const defaultSelect = form.elements.namedItem("calendar_entity");
  defaultSelect.value = "calendar.work";
  defaultSelect.change();
  for (const input of form.querySelectorAll("input")) {
    input.checked = input.value === "calendar.work";
    input.change();
  }

  assert.deepEqual(panel._settingsDrafts.calendars, {
    calendar_entity: "calendar.work",
    calendar_entities: ["calendar.work"],
  });

  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "General").click();
  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "Calendars").click();

  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("calendar_entity").value, "calendar.work");
  assert.deepEqual(
    form.querySelectorAll("input")
      .filter(input => input.checked)
      .map(input => input.value),
    ["calendar.work"],
  );
});

test("settings refresh preserves unmatched local drafts", async () => {
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
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  let form = find(panel._content, "form");
  const ai = form.elements.namedItem("ai_task_entity");
  ai.value = "ai_task.google";
  ai.change();
  await panel.showSettings("general", true);

  assert.deepEqual(panel._settingsDrafts.general, {ai_task_entity: "ai_task.google"});
  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("ai_task_entity").value, "ai_task.google");
});

test("unavailable writable calendar is preserved without becoming a new default option", async () => {
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
        entity_id: "calendar.work", state: "unavailable",
        attributes: {friendly_name: "Work", supported_features: 1},
      },
      "calendar.new": {
        entity_id: "calendar.new", state: "off",
        attributes: {friendly_name: "New", supported_features: 1},
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

  const form = find(panel._content, "form");
  const defaultSelect = form.elements.namedItem("calendar_entity");
  assert.equal(
    defaultSelect.children.some(option => option.value === "calendar.work"),
    false,
  );
  assert.equal(
    defaultSelect.children.some(option => option.value === "calendar.new"),
    true,
  );
  const workCheckbox = form.querySelectorAll("input")
    .find(input => input.value === "calendar.work");
  assert.ok(workCheckbox);
  assert.equal(workCheckbox.checked, true);
});

test("unavailable current default remains preservable in the default picker", async () => {
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
      "calendar.family": {
        entity_id: "calendar.family", state: "unavailable",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
      "calendar.new": {
        entity_id: "calendar.new", state: "off",
        attributes: {friendly_name: "New", supported_features: 1},
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

  const form = find(panel._content, "form");
  const defaultSelect = form.elements.namedItem("calendar_entity");
  assert.equal(defaultSelect.value, "calendar.family");
  assert.equal(
    defaultSelect.children.some(option => option.value === "calendar.family"),
    true,
  );
  assert.equal(
    defaultSelect.children.some(option => option.value === "calendar.new"),
    true,
  );
});


test("unsaved AI draft remains visible if the selected entity becomes unavailable", async () => {
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  const states = {
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
  };
  panel.hass = {
    user: {is_admin: true},
    states,
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings();

  let form = find(panel._content, "form");
  const ai = form.elements.namedItem("ai_task_entity");
  ai.value = "ai_task.google";
  ai.change();

  states["ai_task.google"] = {
    ...states["ai_task.google"],
    state: "unavailable",
  };
  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "Calendars").click();
  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "General").click();

  form = find(panel._content, "form");
  const restored = form.elements.namedItem("ai_task_entity");
  assert.equal(restored.value, "ai_task.google");
  assert.equal(
    restored.children.some(option => option.value === "ai_task.google"),
    true,
  );
});


test("admin can configure and disable Direct IMAP from native Email settings", async () => {
  const calls = [];
  let current = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {
      enabled: true,
      host: "imap.old.test",
      port: 993,
      username: "old@example.test",
      password_configured: true,
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
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      calls.push(message);
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return current;
      if (message.type === "daylight_calendar_import/settings/email/update") {
        current = {
          ...current,
          email: {
            ...current.email,
            enabled: message.enabled,
            ...(message.enabled ? {
              host: message.host,
              port: message.port,
              username: message.username,
              password_configured:
                current.email.password_configured || Boolean(message.password),
              mailbox: message.mailbox,
              verify_ssl: message.verify_ssl,
            } : {}),
          },
        };
        return current;
      }
      throw new Error(`Unexpected call: ${message.type}`);
    },
  };
  await flush();
  await panel.showSettings("email");

  assert.equal(find(panel._content, "h2").textContent, "Email");
  assert.equal(
    panel._content.querySelector(".settings-tabs").querySelectorAll("button")
      .some(button => button.textContent === "Email"),
    true,
  );

  let form = find(panel._content, "form");
  let fields = form.elements;
  assert.equal(fields.namedItem("email_password").value, "");
  assert.match(fields.namedItem("email_password").placeholder, /Configured/);
  assert.equal(find(form, "fieldset").disabled, false);

  fields.namedItem("email_host").value = "imap.new.test";
  fields.namedItem("email_port").value = "1993";
  fields.namedItem("email_username").value = "new@example.test";
  fields.namedItem("email_mailbox").value = "Calendar";
  fields.namedItem("email_verify_ssl").checked = false;
  await panel.saveEmailSettingsForm(form);

  const saved = calls.filter(call =>
    call.type === "daylight_calendar_import/settings/email/update").at(-1);
  assert.deepEqual(saved, {
    type: "daylight_calendar_import/settings/email/update",
    entry_id: "entry-1",
    enabled: true,
    host: "imap.new.test",
    port: 1993,
    username: "new@example.test",
    password: "",
    mailbox: "Calendar",
    verify_ssl: false,
  });
  assert.equal(panel._settings.email.host, "imap.new.test");
  assert.equal(panel._settingsDrafts.email, null);

  form = find(panel._content, "form");
  fields = form.elements;
  const enabled = fields.namedItem("email_enabled");
  enabled.checked = false;
  enabled.change();
  assert.equal(find(form, "fieldset").disabled, true);
  await panel.saveEmailSettingsForm(form);

  const disabled = calls.filter(call =>
    call.type === "daylight_calendar_import/settings/email/update").at(-1);
  assert.deepEqual(disabled, {
    type: "daylight_calendar_import/settings/email/update",
    entry_id: "entry-1",
    enabled: false,
  });
  assert.equal(panel._settings.email.enabled, false);
  assert.equal(panel._settingsDrafts.email, null);
});

test("Email validation failure preserves enabled submitted values and replacement password", async () => {
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {
      enabled: false,
      host: "imap.old.test",
      port: 993,
      username: "old@example.test",
      password_configured: true,
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
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      if (message.type === "daylight_calendar_import/settings/email/update") {
        throw {
          code: "invalid_auth",
          message: "The IMAP server rejected the supplied credentials.",
        };
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings("email");

  let form = find(panel._content, "form");
  let fields = form.elements;
  fields.namedItem("email_enabled").checked = true;
  fields.namedItem("email_enabled").change();
  fields.namedItem("email_host").value = "imap.retry.test";
  fields.namedItem("email_host").input();
  fields.namedItem("email_port").value = "1993";
  fields.namedItem("email_port").input();
  fields.namedItem("email_username").value = "retry@example.test";
  fields.namedItem("email_username").input();
  fields.namedItem("email_password").value = "new-secret";
  fields.namedItem("email_password").input();
  fields.namedItem("email_mailbox").value = "Calendar";
  fields.namedItem("email_mailbox").input();
  fields.namedItem("email_verify_ssl").checked = false;
  fields.namedItem("email_verify_ssl").change();

  await panel.saveEmailSettingsForm(form);

  assert.match(panel._settingsError, /rejected/);
  assert.ok(panel._settingsDrafts.email);
  form = find(panel._content, "form");
  fields = form.elements;
  assert.equal(fields.namedItem("email_enabled").checked, true);
  assert.equal(find(form, "fieldset").disabled, false);
  assert.equal(fields.namedItem("email_host").value, "imap.retry.test");
  assert.equal(fields.namedItem("email_port").value, 1993);
  assert.equal(fields.namedItem("email_username").value, "retry@example.test");
  assert.equal(fields.namedItem("email_password").value, "new-secret");
  assert.equal(fields.namedItem("email_mailbox").value, "Calendar");
  assert.equal(fields.namedItem("email_verify_ssl").checked, false);
  assert.equal(
    globalThis.focusedNode,
    panel._content.querySelector("[data-settings-save-error]"),
  );
});

test("unsaved Email edits survive tab switches and settings refresh", async () => {
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {
      enabled: false,
      host: "imap.old.test",
      port: 993,
      username: "old@example.test",
      password_configured: true,
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
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings("email");

  let form = find(panel._content, "form");
  let fields = form.elements;
  fields.namedItem("email_enabled").checked = true;
  fields.namedItem("email_enabled").change();
  fields.namedItem("email_host").value = "imap.unsaved.test";
  fields.namedItem("email_host").input();
  fields.namedItem("email_password").value = "unsaved-secret";
  fields.namedItem("email_password").input();

  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "General").click();
  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "Email").click();

  form = find(panel._content, "form");
  fields = form.elements;
  assert.equal(fields.namedItem("email_enabled").checked, true);
  assert.equal(fields.namedItem("email_host").value, "imap.unsaved.test");
  assert.equal(fields.namedItem("email_password").value, "unsaved-secret");

  await panel.showSettings("email", true);
  form = find(panel._content, "form");
  fields = form.elements;
  assert.equal(fields.namedItem("email_enabled").checked, true);
  assert.equal(fields.namedItem("email_host").value, "imap.unsaved.test");
  assert.equal(fields.namedItem("email_password").value, "unsaved-secret");
});

test("Email reload failure reflects persisted settings and clears replacement password draft", async () => {
  let persisted = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {
      enabled: true,
      host: "imap.old.test",
      port: 993,
      username: "old@example.test",
      password_configured: false,
      mailbox: "INBOX",
      verify_ssl: true,
    },
  };
  let updateAttempted = false;
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
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return persisted;
      if (message.type === "daylight_calendar_import/settings/email/update") {
        updateAttempted = true;
        persisted = {
          ...persisted,
          email: {
            enabled: true,
            host: message.host,
            port: message.port,
            username: message.username,
            password_configured: true,
            mailbox: message.mailbox,
            verify_ssl: message.verify_ssl,
          },
        };
        throw {
          code: "reload_failed",
          message: "Settings were saved, but Daylight could not reload. Restart Home Assistant.",
        };
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings("email");

  let form = find(panel._content, "form");
  form.elements.namedItem("email_host").value = "imap.saved.test";
  form.elements.namedItem("email_password").value = "saved-secret";
  await panel.saveEmailSettingsForm(form);

  assert.equal(updateAttempted, true);
  assert.equal(panel._settings.email.host, "imap.saved.test");
  assert.equal(panel._settings.email.password_configured, true);
  assert.equal(panel._settingsDrafts.email, null);
  assert.match(panel._settingsReloadWarning, /Restart Home Assistant/);
  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("email_password").value, "");
});

test("ambiguous Email password replacement remains retryable when prior password existed", async () => {
  let getCount = 0;
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {
      enabled: true,
      host: "imap.old.test",
      port: 993,
      username: "old@example.test",
      password_configured: true,
      mailbox: "INBOX",
      verify_ssl: true,
    },
  };
  let reconciled = snapshot;
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
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") {
        getCount += 1;
        return getCount === 1 ? snapshot : reconciled;
      }
      if (message.type === "daylight_calendar_import/settings/email/update") {
        reconciled = {
          ...snapshot,
          email: {
            ...snapshot.email,
            host: message.host,
            username: message.username,
          },
        };
        throw 3;
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings("email");

  let form = find(panel._content, "form");
  form.elements.namedItem("email_host").value = "imap.maybe.test";
  form.elements.namedItem("email_username").value = "maybe@example.test";
  form.elements.namedItem("email_password").value = "replacement-secret";
  await panel.saveEmailSettingsForm(form);

  assert.equal(getCount, 2);
  assert.match(panel._settingsError, /Could not save Direct IMAP settings/);
  assert.ok(panel._settingsDrafts.email);
  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("email_host").value, "imap.maybe.test");
  assert.equal(form.elements.namedItem("email_password").value, "replacement-secret");
});

test("Email controls and settings subnav freeze while save is pending", async () => {
  let resolveSave;
  const pending = new Promise(resolve => { resolveSave = resolve; });
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {
      enabled: true,
      host: "imap.example.test",
      port: 993,
      username: "user@example.test",
      password_configured: true,
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
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      if (message.type === "daylight_calendar_import/settings/email/update") return pending;
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings("email");

  const form = find(panel._content, "form");
  const saving = panel.saveEmailSettingsForm(form);
  await flush();

  const busyForm = find(panel._content, "form");
  assert.equal(busyForm.attributes["aria-busy"], "true");
  assert.equal(
    busyForm.querySelectorAll("input").every(input => input.disabled === true),
    true,
  );
  assert.equal(find(busyForm, "button").disabled, true);
  assert.equal(
    panel._content.querySelector(".settings-tabs").querySelectorAll("button")
      .every(button => button.disabled === true),
    true,
  );

  resolveSave(snapshot);
  await saving;
});


test("ambiguous IMAP disable confirms from enabled state only", async () => {
  let getCount = 0;
  const initial = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {
      enabled: true,
      host: "imap.old.test",
      port: 993,
      username: "old@example.test",
      password_configured: true,
      mailbox: "INBOX",
      verify_ssl: true,
    },
  };
  const reconciled = {
    ...initial,
    email: {...initial.email, enabled: false},
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
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") {
        getCount += 1;
        return getCount === 1 ? initial : reconciled;
      }
      if (message.type === "daylight_calendar_import/settings/email/update") {
        throw 3;
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings("email");

  const form = find(panel._content, "form");
  const fields = form.elements;
  fields.namedItem("email_host").value = "imap.unsent.test";
  fields.namedItem("email_host").input();
  fields.namedItem("email_enabled").checked = false;
  fields.namedItem("email_enabled").change();
  await panel.saveEmailSettingsForm(form);

  assert.equal(getCount, 2);
  assert.equal(panel._settings.email.enabled, false);
  assert.equal(panel._settingsDrafts.email, null);
  assert.equal(panel._settingsError, null);
  assert.match(panel._settingsReloadWarning, /could not confirm that the integration reloaded/i);
});


test("leaving Settings scrubs replacement passwords but keeps non-secret Email edits", async () => {
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {
      enabled: true,
      host: "imap.old.test",
      port: 993,
      username: "old@example.test",
      password_configured: true,
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
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      if (message.type === "call_service") {
        return message.service === "list_activity" ?
          {response: {activity: []}} :
          {response: {imports: []}};
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings("email");

  let form = find(panel._content, "form");
  form.elements.namedItem("email_host").value = "imap.unsaved.test";
  form.elements.namedItem("email_host").input();
  form.elements.namedItem("email_password").value = "activity-secret";
  form.elements.namedItem("email_password").input();

  await panel.showActivity();

  assert.equal(panel._settingsDrafts.email.host, "imap.unsaved.test");
  assert.equal(panel._settingsDrafts.email.password, "");

  await panel.showSettings("email");
  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("email_host").value, "imap.unsaved.test");
  assert.equal(form.elements.namedItem("email_password").value, "");

  form.elements.namedItem("email_password").value = "review-secret";
  form.elements.namedItem("email_password").input();
  await panel.showReview();

  assert.equal(panel._settingsDrafts.email.host, "imap.unsaved.test");
  assert.equal(panel._settingsDrafts.email.password, "");

  await panel.showSettings("email");
  form = find(panel._content, "form");
  const renderedPassword = form.elements.namedItem("email_password");
  renderedPassword.value = "disconnect-secret";
  renderedPassword.input();
  panel.disconnectedCallback();

  assert.equal(renderedPassword.value, "");
  assert.equal(panel._settingsDrafts.email.host, "imap.unsaved.test");
  assert.equal(panel._settingsDrafts.email.password, "");
});

test("leaving Settings drops an Email draft when password was the only edit", async () => {
  const snapshot = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    email: {
      enabled: true,
      host: "imap.example.test",
      port: 993,
      username: "user@example.test",
      password_configured: true,
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
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
    },
    callWS: async message => {
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      if (message.type === "call_service") return {response: {activity: []}};
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings("email");

  const form = find(panel._content, "form");
  form.elements.namedItem("email_password").value = "abandoned-secret";
  form.elements.namedItem("email_password").input();
  assert.ok(panel._settingsDrafts.email);

  await panel.showActivity();

  assert.equal(panel._settingsDrafts.email, null);
});

test("routing tab saves aliases and read-only conflict calendar scope separately", async () => {
  let current = {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family",
    calendar_entities: ["calendar.family"],
    calendar_aliases: {},
    conflict_calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const calls = [];
  const panel = new DaylightImportPanel();
  panel.hass = {
    user: {is_admin: true},
    states: {
      "calendar.family": {
        entity_id: "calendar.family", state: "off",
        attributes: {friendly_name: "Family", supported_features: 1},
      },
      "calendar.busy": {
        entity_id: "calendar.busy", state: "off",
        attributes: {friendly_name: "Busy", supported_features: 0},
      },
    },
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return current;
      if (message.type === "daylight_calendar_import/settings/calendar_intelligence/update") {
        calls.push(message);
        current = {...current, calendar_aliases: message.calendar_aliases,
          conflict_calendar_entities: message.conflict_calendar_entities};
        return current;
      }
      throw new Error("Unexpected request");
    },
  };
  await flush();
  await panel.showSettings("routing");
  const form = find(panel._content, "form");
  form.elements.namedItem("calendar_aliases").value = "Kids = calendar.family";
  for (const input of form.querySelectorAll("input")) {
    input.checked = input.value === "calendar.busy";
  }
  await panel.saveRoutingSettings(form);
  assert.deepEqual(calls[0], {
    type: "daylight_calendar_import/settings/calendar_intelligence/update",
    entry_id: "entry-1",
    calendar_aliases: {Kids: "calendar.family"},
    conflict_calendar_entities: ["calendar.busy"],
  });
  assert.deepEqual(panel._settings.calendar_aliases, {Kids: "calendar.family"});
  assert.equal(panel._settingsDrafts.routing, null);
});

test("routing tab rejects aliases pointing to read-only calendars without saving", async () => {
  const snapshot = {
    entry_id: "entry-1", ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family", calendar_entities: ["calendar.family"],
    calendar_aliases: {}, conflict_calendar_entities: [],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  const messages = [];
  panel.hass = {
    user: {is_admin: true}, states: {},
    callWS: async message => {
      messages.push(message.type);
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type === "daylight_calendar_import/settings/get") return snapshot;
      throw new Error("Unexpected update");
    },
  };
  await flush();
  await panel.showSettings("routing");
  const form = find(panel._content, "form");
  form.elements.namedItem("calendar_aliases").value = "Secret = calendar.private";
  await panel.saveRoutingSettings(form);
  assert.match(form.querySelector(".error").textContent, /writable calendar/);
  assert.equal(messages.includes("daylight_calendar_import/settings/calendar_intelligence/update"), false);
});

test("routing aliases with equals and astral Unicode names are accepted by client", async () => {
  let current = {
    entry_id: "entry-1", ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family", calendar_entities: ["calendar.family"],
    calendar_aliases: {"Pickup=A": "calendar.family"},
    conflict_calendar_entities: [],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  let sent;
  const panel = new DaylightImportPanel();
  panel.hass = {user: {is_admin: true}, states: {},
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type.endsWith("/settings/get")) return current;
      if (message.type.endsWith("/settings/calendar_intelligence/update")) {
        sent = message;
        current = {...current, calendar_aliases: message.calendar_aliases,
          conflict_calendar_entities: message.conflict_calendar_entities};
        return current;
      }
      throw Error("Unexpected call");
    },
  };
  await flush();
  await panel.showSettings("routing");
  const form = find(panel._content, "form");
  assert.match(form.elements.namedItem("calendar_aliases").value, /Pickup=A/);
  form.elements.namedItem("calendar_aliases").value =
    `Pickup=A = calendar.family\n${"🎉".repeat(33)} = calendar.family`;
  await panel.saveRoutingSettings(form);
  assert.ok(sent);
  assert.deepEqual(Object.keys(sent.calendar_aliases), ["Pickup=A", "🎉".repeat(33)]);
});

test("routing preserves unfinished text and conflict selection across tabs", async () => {
  const snapshot = {
    entry_id: "entry-1", ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family", calendar_entities: ["calendar.family"],
    calendar_aliases: {}, conflict_calendar_entities: [],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel.hass = {user: {is_admin: true}, states: {
    "calendar.family": {entity_id: "calendar.family", state: "off",
      attributes: {supported_features: 1}},
    "calendar.personal": {entity_id: "calendar.personal", state: "off",
      attributes: {supported_features: 0}},
  }, callWS: async message => {
    if (message.type === "call_service") return {response: {imports: []}};
    if (message.type.endsWith("/settings/get")) return snapshot;
    throw Error("Unexpected call");
  }};
  await flush();
  await panel.showSettings("routing");
  let form = find(panel._content, "form");
  const textarea = form.elements.namedItem("calendar_aliases");
  textarea.value = "Unfinished = ";
  textarea.input();
  for (const input of form.querySelectorAll("input")) {
    if (input.value === "calendar.personal") {
      input.checked = true;
      input.change();
    }
  }
  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "General").click();
  panel._content.querySelector(".settings-tabs").querySelectorAll("button")
    .find(button => button.textContent === "Routing").click();
  form = find(panel._content, "form");
  assert.equal(form.elements.namedItem("calendar_aliases").value, "Unfinished = ");
  assert.equal(form.querySelectorAll("input")
    .find(input => input.value === "calendar.personal").checked, true);
});

test("routing ambiguous save reconciles normalized server aliases", async () => {
  let current = {
    entry_id: "entry-1", ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family", calendar_entities: ["calendar.family"],
    calendar_aliases: {}, conflict_calendar_entities: [],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel.hass = {user: {is_admin: true}, states: {},
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type.endsWith("/settings/get")) return current;
      if (message.type.endsWith("/settings/calendar_intelligence/update")) {
        current = {...current, calendar_aliases: {kids: "calendar.family"}};
        throw Error("response lost after persistence");
      }
      throw Error("Unexpected call");
    },
  };
  await flush();
  await panel.showSettings("routing");
  const form = find(panel._content, "form");
  form.elements.namedItem("calendar_aliases").value = " Kids = calendar.family";
  await panel.saveRoutingSettings(form);
  assert.equal(panel._settingsDrafts.routing, null);
  assert.equal(panel._settingsError, null);
  assert.deepEqual(panel._settings.calendar_aliases, {kids: "calendar.family"});
});

test("routing textarea is disabled while settings save is awaiting response", async () => {
  let finish;
  const pending = new Promise(resolve => {finish = resolve;});
  const snapshot = {
    entry_id: "entry-1", ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family", calendar_entities: ["calendar.family"],
    calendar_aliases: {}, conflict_calendar_entities: [],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel.hass = {user: {is_admin: true}, states: {}, callWS: async message => {
    if (message.type === "call_service") return {response: {imports: []}};
    if (message.type.endsWith("/settings/get")) return snapshot;
    if (message.type.endsWith("/settings/calendar_intelligence/update")) return pending;
    throw Error("Unexpected call");
  }};
  await flush();
  await panel.showSettings("routing");
  find(panel._content, "textarea").value = "Kids = calendar.family";
  const started = panel.saveRoutingSettings(find(panel._content, "form"));
  assert.equal(find(panel._content, "textarea").disabled, true);
  finish(snapshot);
  await started;
});

test("routing save patches only changed settings fields and preserves special keys", async () => {
  const snapshot = {
    entry_id: "entry-1", ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family", calendar_entities: ["calendar.family"],
    calendar_aliases: Object.fromEntries([["__proto__", "calendar.family"]]),
    conflict_calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const calls = [];
  const panel = new DaylightImportPanel();
  panel.hass = {user: {is_admin: true}, states: {
    "calendar.family": {entity_id: "calendar.family", state: "off", attributes: {supported_features: 1}},
    "calendar.readonly": {entity_id: "calendar.readonly", state: "off", attributes: {supported_features: 0}},
  }, callWS: async message => {
    if (message.type === "call_service") return {response: {imports: []}};
    if (message.type.endsWith("/settings/get")) return snapshot;
    if (message.type.endsWith("/settings/calendar_intelligence/update")) {
      calls.push(message);
      return {...snapshot, ...message};
    }
    throw Error("Unexpected call");
  }};
  await flush();
  await panel.showSettings("routing");
  let form = find(panel._content, "form");
  assert.match(form.elements.namedItem("calendar_aliases").value, /__proto__/);
  for (const input of form.querySelectorAll("input")) {
    input.checked = input.value === "calendar.readonly";
  }
  await panel.saveRoutingSettings(form);
  assert.deepEqual(calls[0].conflict_calendar_entities, ["calendar.readonly"]);
  assert.equal(Object.hasOwn(calls[0], "calendar_aliases"), false);

  panel._settings = snapshot;
  await panel.showSettings("routing");
  form = find(panel._content, "form");
  form.elements.namedItem("calendar_aliases").value =
    "__proto__ = calendar.family\nKids = calendar.family";
  await panel.saveRoutingSettings(form);
  assert.equal(Object.hasOwn(calls[1], "conflict_calendar_entities"), false);
  assert.equal(Object.hasOwn(calls[1].calendar_aliases, "__proto__"), true);
  assert.equal(calls[1].calendar_aliases.__proto__, "calendar.family");
});

test("routing reconciles server whitespace-folded aliases after a lost save response", async () => {
  let current = {
    entry_id: "entry-1", ai_task_entity: "ai_task.openai",
    calendar_entity: "calendar.family", calendar_entities: ["calendar.family"],
    calendar_aliases: {}, conflict_calendar_entities: ["calendar.family"],
    email: {enabled: false, host: "", port: 993, username: "",
      password_configured: false, mailbox: "INBOX", verify_ssl: true},
  };
  const panel = new DaylightImportPanel();
  panel.hass = {user: {is_admin: true}, states: {},
    callWS: async message => {
      if (message.type === "call_service") return {response: {imports: []}};
      if (message.type.endsWith("/settings/get")) return current;
      if (message.type.endsWith("/settings/calendar_intelligence/update")) {
        current = {...current, calendar_aliases: {"kids events": "calendar.family"}};
        throw Error("response lost after persistence");
      }
      throw Error("Unexpected call");
    },
  };
  await flush();
  await panel.showSettings("routing");
  const form = find(panel._content, "form");
  form.elements.namedItem("calendar_aliases").value =
    " Kids   Events = calendar.family";
  await panel.saveRoutingSettings(form);
  assert.equal(panel._settingsDrafts.routing, null);
  assert.equal(panel._settingsError, null);
  assert.deepEqual(panel._settings.calendar_aliases, {"kids events": "calendar.family"});
});
