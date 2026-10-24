import assert from "node:assert/strict";
import test from "node:test";

class FakeNode {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this[name] = callback; }
  focus() { this.focused = true; }
  querySelectorAll(selector) {
    const all = [this, ...this.children.flatMap(child => child.querySelectorAll(selector))];
    if (selector.startsWith("[")) {
      const [name, value] = selector.slice(1, -1).split("=");
      return all.filter(item => name in item.attributes && (value === undefined || item.attributes[name] === value));
    }
    return all.filter(item => item.tag === selector);
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  get elements() { return {namedItem: name => this.querySelectorAll("input").find(item => item.name === name)}; }
}
globalThis.document = {createElement: tag => new FakeNode(tag)};
const {HostedConnectionSettings, configureHostedService, loadHostedState, runHostedAction, trustedHostedOrigin, validateHostedState} = await import("./hosted_settings.js");
const snapshot = {
  entry_id: "entry-1", configured: true, automatic_lifecycle: false, api_origin: "https://api.example.test",
  account_origin: "https://account.example.test", account_url: null,
  state: "disconnected", credential_status: null, credential_expires_at: null,
};
const pairing = {user_code: "ABCD1234EFGH", verification_uri: "https://account.example.test/connect",
  expires_at: "2026-12-01T00:10:00Z", poll_interval_seconds: 5};
const texts = root => [root.textContent || "", ...root.children.flatMap(texts)];
const findButton = (root, text) => root.querySelectorAll("button").find(button => button.textContent === text);
const event = () => ({preventDefault() {}});
const flush = () => new Promise(resolve => setImmediate(resolve));

function setup(response = snapshot) {
  const calls = [];
  let handler = async () => response;
  let hass = {user: {id: "owner", is_admin: true}, connection: {connected: true}, callWS: async message => {
    calls.push(message); return handler(message);
  }};
  const busy = [];
  const settings = new HostedConnectionSettings({getHass: () => hass, onBusy: value => busy.push(value)});
  return {settings, calls, busy, setHass: value => { hass = value; }, getHass: () => hass,
    setHandler: value => { handler = value; }};
}

test("hosted client sends only explicit closed requests and never secrets", async () => {
  const calls = [];
  const hass = {callWS: async message => { calls.push(message); return snapshot; }};
  await loadHostedState(hass);
  await loadHostedState(hass, "entry-1");
  await configureHostedService(hass, "entry-1", snapshot.api_origin, snapshot.account_origin);
  await runHostedAction(hass, "entry-1", "connect", {processing_consent: true, installation_label: "Home", token: "secret"});
  await runHostedAction(hass, "entry-1", "disconnect", {confirm_disconnect: true, processing_consent: true, installation_label: "secret"});
  assert.deepEqual(calls, [
    {type: "daylight_calendar_import/hosted/get"},
    {type: "daylight_calendar_import/hosted/get", entry_id: "entry-1"},
    {type: "daylight_calendar_import/hosted/configure", entry_id: "entry-1", api_origin: snapshot.api_origin, account_origin: snapshot.account_origin, automatic_lifecycle: false},
    {type: "daylight_calendar_import/hosted/action", entry_id: "entry-1", action: "connect", processing_consent: true, installation_label: "Home"},
    {type: "daylight_calendar_import/hosted/action", entry_id: "entry-1", action: "disconnect", confirm_disconnect: true},
  ]);
  for (const input of [{processing_consent: false, installation_label: "Home"},
    {processing_consent: 1, installation_label: "Home"}, {processing_consent: true, installation_label: ""},
    {processing_consent: true, installation_label: "x".repeat(129)}]) {
    await assert.rejects(() => runHostedAction(hass, "entry-1", "connect", input), /consent/);
  }
  await assert.rejects(() => runHostedAction(hass, "entry-1", "clear_stop_marker"), /Unsupported/);
  await assert.rejects(() => configureHostedService(hass, "entry-1", "http://api.example.test", snapshot.account_origin), /HTTPS/);
  for (const input of [{}, {confirm_disconnect: false}, {confirm_disconnect: 1}]) {
    await assert.rejects(() => runHostedAction(hass, "entry-1", "disconnect", input), /Confirm local stop/);
  }
  assert.equal(calls.length, 5);
});

test("trusted origins canonicalize equivalent HTTPS destinations and reject URL tricks", () => {
  assert.equal(trustedHostedOrigin("https://API.example.test:443/"), snapshot.api_origin);
  assert.equal(trustedHostedOrigin("https://[2001:0db8::1]:8443"), "https://[2001:db8::1]:8443");
  for (const value of [null, 4, "http://api.example.test", "https://api.example.test/path", "https://u:p@api.example.test",
    "https://api.example.test?", "https://api.example.test#", "https://api.example.test\\evil", "https://api.example.test\n",
    "https://api.example.test:0", "https://" + "x".repeat(2041)]) assert.equal(trustedHostedOrigin(value), null);
});

test("hosted response is closed, entry-scoped, and secret-free", () => {
  assert.deepEqual(validateHostedState(snapshot, {entryId: "entry-1"}), snapshot);
  const unconfigured = {...snapshot, configured: false, automatic_lifecycle: false, api_origin: null, account_origin: null, state: "not_configured"};
  assert.deepEqual(validateHostedState(unconfigured), unconfigured);
  for (const value of [null, [], {}, {...snapshot, installation_token: "secret"}, {...snapshot, state: "<script>"},
    {...snapshot, configured: 1}, {...snapshot, entry_id: ""}, {...snapshot, credential_status: "secret"},
    {...snapshot, credential_expires_at: "2026-12-01T00:00:00"}, {...snapshot, configured: false},
    {...snapshot, api_origin: "http://api.example.test"}, {...snapshot, account_url: "https://foreign.example/account"},
    {...snapshot, account_url: "https://account.example.test/account?token=secret"}, {...snapshot, pairing}]) {
    assert.throws(() => validateHostedState(value), /Unexpected/);
  }
  assert.throws(() => validateHostedState(snapshot, {entryId: "other"}), /Unexpected/);
  assert.equal(validateHostedState({...snapshot, credential_expires_at: "2026-12-01T00:00:00+00:00",
    account_url: "https://account.example.test/account"}).account_url, "https://account.example.test/account");
});

test("pairing instructions can appear only on explicit connect and use the trusted account origin", async () => {
  const hass = {callWS: async () => ({...snapshot, pairing})};
  assert.deepEqual((await runHostedAction(hass, "entry-1", "connect", {processing_consent: true, installation_label: "Home"})).pairing, pairing);
  await assert.rejects(() => loadHostedState(hass), /Unexpected/);
  await assert.rejects(() => runHostedAction(hass, "entry-1", "poll"), /Unexpected/);
  for (const change of [{verification_uri: "javascript:alert(1)"}, {verification_uri: "https://other.example/connect"},
    {verification_uri: "https://owner:password@account.example.test/connect"}, {verification_uri: "https://account.example.test/connect?secret=x"},
    {verification_uri: "https://account.example.test/connect#secret"}, {verification_uri: "https://account.example.test/\nconnect"},
    {poll_interval_seconds: 4}, {poll_interval_seconds: 31}, {poll_interval_seconds: "5"}, {user_code: "<script>"},
    {expires_at: "invalid"}, {private_verifier: "secret"}]) {
    assert.throws(() => validateHostedState({...snapshot, pairing: {...pairing, ...change}}, {pairing: true}), /Unexpected/);
  }
});

test("connection UI needs explicit consent and disconnect confirmation, with no automatic polling", async () => {
  const {settings, calls, setHandler} = setup();
  await settings.load("entry-1");
  assert.equal(calls.length, 1);
  const form = settings.element.querySelector("[data-hosted-connect]");
  const consent = form.elements.namedItem("processing_consent");
  assert.equal(consent.checked, false);
  assert.equal(findButton(form, "Start new connection").disabled, true);
  form.submit(event()); await flush();
  assert.equal(calls.length, 1);
  setHandler(async message => message.action === "connect" ? {...snapshot, state: "pairing_pending", pairing} : snapshot);
  consent.checked = true; consent.change();
  assert.equal(findButton(form, "Start new connection").disabled, false);
  form.submit(event()); await flush();
  assert.equal(calls[1].action, "connect");
  assert.ok(texts(settings.element).includes("Approval code: ABCD1234EFGH"));
  assert.equal(settings.snapshot.pairing, undefined);
  const approve = settings.element.querySelectorAll("a").find(anchor => anchor.textContent === "Approve this connection");
  assert.equal(approve.href, pairing.verification_uri);
  assert.equal(approve.rel, "noopener noreferrer");
  assert.equal(calls.length, 2);
  const disconnect = settings.element.querySelector("[data-hosted-disconnect]");
  disconnect.submit(event()); await flush();
  assert.equal(calls.length, 2);
  const confirm = disconnect.elements.namedItem("confirm_disconnect");
  confirm.checked = true; confirm.change(); disconnect.submit(event()); await flush();
  assert.equal(calls[2].action, "disconnect");
  assert.equal(calls[2].confirm_disconnect, true);
  assert.equal(settings.pairing, null);
});

test("configuration and actions share admission and sanitize uncertain failures", async () => {
  const {settings, calls, busy, setHandler} = setup();
  await settings.load("entry-1");
  let finish;
  setHandler(() => new Promise(resolve => { finish = resolve; }));
  const action = settings.submit("reconcile");
  await settings.submit("disconnect");
  assert.equal(calls.length, 2);
  assert.equal(settings.busy, true);
  finish({...snapshot, state: "connected", credential_status: "current"}); await action;
  assert.deepEqual(busy, [true, false]);
  assert.equal(findButton(settings.element, "Renew credentials").disabled, false);
  setHandler(async () => { throw new Error("secret token and verifier"); });
  await settings.submit("rotate");
  assert.equal(texts(settings.element).some(text => /secret token/.test(text)), false);
  assert.equal(texts(settings.element).some(text => /could not be confirmed/.test(text)), true);
  const config = settings.element.querySelector("[data-hosted-configure]");
  config.elements.namedItem("api_origin").value = "https://api2.example.test";
  config.submit(event()); await flush();
  assert.equal(calls.at(-1).type, "daylight_calendar_import/hosted/configure");
  assert.equal(settings.pairing, null);
});

test("owner repair and overlap retain only safe explicit actions and verified account links", async () => {
  const {settings, setHandler} = setup({...snapshot, state: "repair_required", credential_status: "overlap",
    credential_expires_at: "2026-12-01T00:00:00Z", account_url: "https://account.example.test/account"});
  await settings.load("entry-1");
  assert.equal(findButton(settings.element, "Renew credentials").disabled, true);
  assert.equal(settings.element.querySelector("[data-hosted-connect]"), null);
  const portal = settings.element.querySelectorAll("a").find(anchor => anchor.textContent === "Open account portal");
  assert.equal(portal.href, "https://account.example.test/account");
  assert.equal(portal.rel, "noopener noreferrer");
  for (const state of ["storage_repair", "configuration_repair", "service_repair", "operation_finishing", "remote_disconnect_unconfirmed", "new_future_state"]) {
    setHandler(async () => ({...snapshot, state})); await settings.load("entry-1");
    assert.equal(settings.element.querySelector("[data-hosted-connect]"), null);
    assert.equal(findButton(settings.element, "Renew credentials").disabled, true);
    assert.equal(settings.element.querySelector("[data-hosted-disconnect]") !== null, true);
  }
});

test("pairing instructions survive a pending manual poll but disappear on clear", async () => {
  const {settings, setHandler} = setup(); await settings.load("entry-1");
  setHandler(async () => ({...snapshot, state: "pairing_pending", pairing}));
  await settings.submit("connect", {processing_consent: true, installation_label: "Home"});
  setHandler(async () => ({...snapshot, state: "pairing_wait"}));
  await settings.submit("poll");
  assert.deepEqual(settings.pairing, pairing);
  settings.clear();
  assert.equal(settings.pairing, null);
  assert.equal(texts(settings.element).some(text => text.includes(pairing.user_code)), false);
});

test("late read and mutation responses cannot restore cleared or changed-session pairing", async () => {
  for (const change of ["clear", "user", "connection", "admin", "disconnected"]) {
    const context = setup(); const {settings, setHandler, getHass, setHass, calls} = context;
    await settings.load("entry-1");
    let finish;
    setHandler(() => new Promise(resolve => { finish = resolve; }));
    const operation = settings.submit("connect", {processing_consent: true, installation_label: "Home"});
    if (change === "clear") settings.clear();
    else if (change === "user") setHass({...getHass(), user: {id: "other", is_admin: true}});
    else if (change === "connection") setHass({...getHass(), connection: {connected: true}});
    else if (change === "admin") setHass({...getHass(), user: {id: "owner", is_admin: false}});
    else setHass({...getHass(), connection: {...getHass().connection, connected: false}});
    settings.syncSession();
    finish({...snapshot, state: "pairing_pending", pairing}); await operation;
    assert.equal(settings.pairing, null, change);
    assert.equal(settings.snapshot, null, change);
    assert.equal(settings.busy, false, change);
    assert.equal(calls.length, 2);
  }
  const {settings, setHandler} = setup();
  let finish;
  setHandler(() => new Promise(resolve => { finish = resolve; }));
  const loading = settings.load("entry-1"); settings.clear(); finish(snapshot); await loading;
  assert.equal(settings.snapshot, null);
});

test("non-administrators cannot invoke reads or actions and load errors use fixed text", async () => {
  const {settings, getHass, setHass, setHandler, calls} = setup();
  setHass({...getHass(), user: {is_admin: false}});
  await settings.load("entry-1"); await settings.submit("disconnect");
  assert.equal(calls.length, 0);
  assert.equal(texts(settings.element).some(text => /administrator/.test(text)), true);
  setHass({...getHass(), user: {id: "owner", is_admin: true}});
  setHandler(async () => { throw {message: "secret raw provider data"}; });
  await settings.load("entry-1");
  assert.equal(texts(settings.element).some(text => /secret raw/.test(text)), false);
  assert.equal(texts(settings.element).some(text => /Could not load/.test(text)), true);
});


test("socket disconnect clears codes immediately and reconnect never repeats an action", async () => {
  const listeners = new Map();
  const connection = {connected: true,
    addEventListener: (name, callback) => listeners.set(name, callback),
    removeEventListener: (name, callback) => { if (listeners.get(name) === callback) listeners.delete(name); },
  };
  const {settings, getHass, setHass, setHandler, calls} = setup();
  setHass({...getHass(), connection});
  await settings.load("entry-1");
  setHandler(async () => ({...snapshot, state: "pairing_pending", pairing}));
  await settings.submit("connect", {processing_consent: true, installation_label: "Home"});
  const count = calls.length;
  listeners.get("disconnected")();
  assert.equal(settings.pairing, null);
  assert.equal(settings.snapshot, null);
  await settings.load("entry-1");
  assert.equal(calls.length, count);
  setHandler(async () => snapshot);
  listeners.get("ready")();
  await flush();
  assert.equal(calls.length, count + 1);
  await settings.load("entry-1");
  assert.equal(calls.at(-1).type, "daylight_calendar_import/hosted/get");
  assert.equal(settings.pairing, null);
  settings.stop();
  assert.equal(listeners.size, 0);
});


test("returning without a saved approval code offers fresh explicit consent and pairing", async () => {
  const {settings, setHandler, calls} = setup();
  for (const state of ["pairing_pending", "pairing_wait"]) {
    settings.clear();
    setHandler(async () => ({...snapshot, state}));
    await settings.load("entry-1");
    const form = settings.element.querySelector("[data-hosted-connect]");
    assert.ok(form);
    assert.equal(form.elements.namedItem("processing_consent").checked, false);
    assert.equal(findButton(form, "Start new connection").disabled, true);
  }
  assert.equal(calls.every(call => call.type.endsWith("/get")), true);
});


test("a settled previous operation has an explicit recovery control without mutation replay", async () => {
  const {settings, setHandler, calls} = setup();
  setHandler(async call => ({...snapshot, state: call.action === "reconcile" ? "disconnected" : "operation_finishing"}));
  await settings.load("entry-1");
  const check = findButton(settings.element, "Check and recover connection");
  assert.equal(check.disabled, false);
  assert.equal(findButton(settings.element, "Check pairing approval").disabled, true);
  assert.equal(findButton(settings.element, "Renew credentials").disabled, true);
  assert.equal(calls.length, 1);
  check.click(); await flush();
  assert.equal(calls.length, 2);
  assert.equal(calls[1].action, "reconcile");
  assert.equal(settings.snapshot.state, "disconnected");
});


test("socket readiness reloads safe state while open without restoring codes or actions", async () => {
  const context = setup();
  const listeners = new Map();
  context.getHass().connection.addEventListener = (event, callback) => listeners.set(event, callback);
  context.getHass().connection.removeEventListener = event => listeners.delete(event);
  await context.settings.load("entry-1");
  context.setHandler(async () => ({...snapshot, state: "pairing_pending", pairing}));
  await context.settings.submit("connect", {processing_consent: true, installation_label: "Home"});
  context.getHass().connection.connected = false;
  listeners.get("disconnected")();
  assert.equal(context.settings.pairing, null);
  context.setHandler(async () => ({...snapshot, state: "recovery_required"}));
  context.getHass().connection.connected = true;
  listeners.get("ready")();
  await flush();
  assert.equal(context.settings.snapshot.state, "recovery_required");
  assert.equal(context.calls.at(-1).type, "daylight_calendar_import/hosted/get");
  assert.equal(context.calls.filter(call => call.action).length, 1);
  assert.equal(context.settings.pairing, null);
  listeners.get("disconnected")();
  context.settings.clear(); // Navigation must not reload the closed view.
  const count = context.calls.length;
  listeners.get("ready")(); await flush();
  assert.equal(context.calls.length, count);
});

const automaticSnapshot = {...snapshot, automatic_lifecycle: true};
function subscribedContext(response = automaticSnapshot) {
  const context = setup(response);
  const subscriptions = [];
  context.getHass().connection.subscribeMessage = (callback, message) => {
    const subscription = {callback, message, closed: false};
    subscriptions.push(subscription);
    return Promise.resolve(() => { subscription.closed = true; });
  };
  return {...context, subscriptions};
}

test("automatic maintenance checkbox is explicit and saved with trusted origins", async () => {
  const context = setup();
  await context.settings.load("entry-1");
  const form = context.settings.element.querySelector("[data-hosted-configure]");
  const checkbox = form.elements.namedItem("automatic_lifecycle");
  assert.equal(checkbox.checked, false);
  checkbox.checked = true;
  context.setHandler(async () => automaticSnapshot);
  form.submit(event()); await flush();
  assert.equal(context.calls.at(-1).automatic_lifecycle, true);
  assert.equal(context.settings.element.querySelector("[data-hosted-configure]").elements.namedItem("automatic_lifecycle").checked, true);
});

test("admin subscription fetches fresh safe state and retains only current pending approval", async () => {
  const context = subscribedContext();
  const {settings, subscriptions} = context;
  await settings.load("entry-1"); await flush();
  assert.deepEqual(subscriptions[0].message, {type: "daylight_calendar_import/hosted/subscribe", entry_id: "entry-1"});
  context.setHandler(async message => message.action === "connect" ? {...automaticSnapshot, state: "pairing_pending", pairing} :
    {...automaticSnapshot, state: "pairing_wait"});
  await settings.submit("connect", {processing_consent: true, installation_label: "Home"});
  subscriptions[0].callback({entry_id: "entry-1"}); await flush();
  assert.equal(settings.pairing.user_code, pairing.user_code);
  assert.equal(settings.snapshot.state, "pairing_wait");
  context.setHandler(async () => ({...automaticSnapshot, state: "connected", credential_status: "current", credential_expires_at: "2026-12-01T00:00:00Z"}));
  subscriptions[0].callback({entry_id: "entry-1"}); await flush();
  assert.equal(settings.pairing, null);
  assert.equal(settings.snapshot.state, "connected");
  assert.equal(context.calls.filter(call => call.action).length, 1);
  settings.clear(); assert.equal(subscriptions[0].closed, true);
  const count = context.calls.length;
  subscriptions[0].callback({entry_id: "entry-1"}); await flush();
  assert.equal(context.calls.length, count);
});

test("late subscription and refresh cannot restore another session or older state", async () => {
  const context = subscribedContext();
  const {settings, subscriptions} = context;
  let resolveSubscription, unsubscribed = false;
  context.getHass().connection.subscribeMessage = () => new Promise(resolve => { resolveSubscription = resolve; });
  await settings.load("entry-1"); settings.clear();
  resolveSubscription(() => { unsubscribed = true; }); await flush();
  assert.equal(unsubscribed, true);
  context.getHass().connection.subscribeMessage = (callback, message) => {
    subscriptions.push({callback, message}); return Promise.resolve(() => {});
  };
  await settings.load("entry-1"); await flush();
  const finishes = [];
  context.setHandler(() => new Promise(resolve => finishes.push(resolve)));
  subscriptions[0].callback({entry_id: "entry-1"});
  subscriptions[0].callback({entry_id: "entry-1"});
  finishes[1]({...automaticSnapshot, state: "connected"}); await flush();
  finishes[0]({...automaticSnapshot, state: "pairing_pending"}); await flush();
  assert.equal(settings.snapshot.state, "connected");
  subscriptions[0].callback({entry_id: "entry-1"});
  settings.stop();
  finishes[2](automaticSnapshot); await flush();
  assert.equal(settings.snapshot, null);
});

test("subscription ignores foreign or private payloads and opt-out closes it", async () => {
  const context = subscribedContext();
  await context.settings.load("entry-1"); await flush();
  const count = context.calls.length;
  for (const payload of [{entry_id: "other"}, {entry_id: "entry-1", token: "secret"}, null]) context.subscriptions[0].callback(payload);
  await flush(); assert.equal(context.calls.length, count);
  context.setHandler(async () => snapshot);
  await context.settings.submit("configure", {...snapshot, automatic_lifecycle: false});
  assert.equal(context.subscriptions[0].closed, true);
});


test("busy outage renders and late responses preserve only the same-session reconnect intent", async () => {
  const context = setup();
  const {settings, getHass, setHandler, setHass, calls} = context;
  const listeners = new Map();
  getHass().connection.addEventListener = (event, callback) => listeners.set(event, callback);
  getHass().connection.removeEventListener = event => listeners.delete(event);
  await settings.load("entry-1");
  let finish;
  setHandler(() => new Promise(resolve => { finish = resolve; }));
  const pending = settings.submit("connect", {processing_consent: true, installation_label: "Home"});
  getHass().connection.connected = false;
  listeners.get("disconnected")();
  // The parent renders during the outage, then the interrupted call settles.
  await settings.load("entry-1");
  finish({...snapshot, state: "pairing_pending", pairing}); await pending;
  assert.equal(settings.reconnectEntry, "entry-1");
  listeners.get("disconnected")(); // Repeated outage events are harmless.
  setHandler(async () => snapshot);
  getHass().connection.connected = true;
  listeners.get("ready")(); await flush();
  assert.equal(settings.snapshot.state, "disconnected");
  assert.equal(settings.pairing, null);
  assert.equal(calls.filter(call => call.action).length, 1);
  getHass().connection.connected = false;
  listeners.get("disconnected")();
  setHass({...getHass(), user: {id: "other", is_admin: true}});
  settings.syncSession();
  assert.equal(settings.reconnectEntry, null);
  settings.stop();
});
