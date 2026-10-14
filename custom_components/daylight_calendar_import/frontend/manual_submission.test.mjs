import assert from "node:assert/strict";
import test from "node:test";
import {ManualSubmission, submitText, submissionMessage} from "./manual_submission.js";

class FakeNode {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.value = ""; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this[name] = callback; }
  focus() { this.focused = true; }
}
globalThis.document = {createElement: tag => new FakeNode(tag)};
const empty = {pending: null, duplicate: false, duplicate_source: false, duplicate_events: 0, warnings: []};
const pending = {...empty, pending: {id: "pending-1", events: [{}, {}]}};
const event = {preventDefault() {}};

test("text transport preserves multiple events and routing directives unmodified", async () => {
  const text = "  Calendar: Family\nDinner tomorrow 6pm\nDentist Friday 9am  ";
  let request;
  assert.equal(await submitText({callWS: async value => { request = value; return {response: pending}; }}, text), pending);
  assert.deepEqual(request, {type: "call_service", domain: "daylight_calendar_import", service: "submit_text", service_data: {text}, return_response: true});
});

test("whitespace and malformed responses never report successful extraction", async () => {
  let calls = 0;
  const hass = {callWS: async () => { calls++; return {}; }};
  await assert.rejects(submitText(hass, " \n\t"), /Paste event details/);
  assert.equal(calls, 0);
  for (const response of [null, {}, [], {...empty, pending: {}}, {...empty, warnings: [1]},
    {...empty, duplicate_events: -1}, {...empty, pending: {id: "p", events: []}},
    {...empty, pending: {id: "  ", events: [{}]}}, {...empty, duplicate: true},
    {...pending, duplicate_source: true}, {...empty, duplicate_events: 1},
    {...pending, status: "no_events"}, {...empty, activity_id: " "}]) {
    await assert.rejects(submitText({callWS: async () => ({response})}, "Dinner"), error => {
      assert.match(error.message, /unexpected response/);
      assert.equal(error.code, "invalid_submission_response");
      return true;
    });
  }
});

test("pending, duplicate and no-event responses describe review outcomes", () => {
  assert.match(submissionMessage(pending), /2 events are ready for review/);
  assert.match(submissionMessage({...pending, duplicate: true, duplicate_events: 1}), /ready for review/);
  assert.match(submissionMessage({...empty, duplicate: true, duplicate_source: true}), /only duplicates/);
  assert.match(submissionMessage(empty), /No events were found/);
});

test("form validates whitespace locally and preserves the original draft", async () => {
  let calls = 0;
  const form = new ManualSubmission({getHass: () => ({callWS: async () => { calls++; return {response: empty}; }})});
  form.text.value = " \n ";
  await form.form.submit(event);
  assert.equal(calls, 0);
  assert.equal(form.text.focused, true);
  assert.match(form.status.textContent, /Paste event details/);
  form.text.value = " Dinner tomorrow at 6pm ";
  await form.form.submit(event);
  assert.equal(form.text.value, " Dinner tomorrow at 6pm ");
  assert.match(form.status.textContent, /No events were found/);
  assert.equal(form.button.disabled, false);
});

test("processing prevents concurrent submission and renders warnings safely", async () => {
  let resolve;
  let calls = 0;
  let submitted;
  const form = new ManualSubmission({
    getHass: () => ({callWS: () => { calls++; return new Promise(done => { resolve = done; }); }}),
    onSubmitted: response => { submitted = response; },
  });
  form.text.value = "Dinner";
  const first = form.form.submit(event);
  assert.equal(form.busy, true);
  assert.equal(form.text.disabled, true);
  assert.equal(form.button.disabled, true);
  await form.form.submit(event);
  assert.equal(calls, 1);
  const response = {...pending, warnings: ["<img src=x onerror=alert(1)>"]};
  resolve({response});
  await first;
  assert.equal(submitted, response);
  assert.match(form.status.textContent, /Approve them/);
  assert.match(form.status.textContent, /<img/);
  assert.equal(form.status.children.length, 0);
  assert.equal(form.busy, false);
  assert.equal(form.text.disabled, false);
});

test("service errors retain the draft and restore usable controls", async () => {
  const form = new ManualSubmission({getHass: () => ({callWS: async () => { throw {message: "AI provider is unavailable"}; }})});
  form.text.value = "Dinner";
  await form.form.submit(event);
  assert.equal(form.status.textContent, "AI provider is unavailable");
  assert.equal(form.text.value, "Dinner");
  assert.equal(form.button.disabled, false);
  assert.equal(form.busy, false);
});

test("inbox callback failure cannot disguise a successful submission", async () => {
  const form = new ManualSubmission({
    getHass: () => ({callWS: async () => ({response: pending})}),
    onSubmitted: async () => { throw new Error("Inbox unavailable"); },
  });
  form.text.value = "Dinner";
  await form.form.submit(event);
  assert.match(form.status.textContent, /ready for review/);
  assert.match(form.status.textContent, /Refresh the review inbox/);
  assert.equal(form.button.disabled, false);
});

test("text transport validates optional source IDs before calling the service", async () => {
  let request;
  const hass = {callWS: async value => { request = value; return {response: empty}; }};
  for (const sourceId of [null, "", "  ", 1]) {
    await assert.rejects(submitText(hass, "Dinner", {sourceId}), /source ID is invalid/);
    assert.equal(request, undefined);
  }
  await submitText(hass, "Dinner", {sourceId: "manual-1"});
  assert.deepEqual(request.service_data, {text: "Dinner", source_id: "manual-1"});
});

test("explicit text retries retain source identity after an unknown outcome and edits rotate it", async () => {
  const calls = [];
  const form = new ManualSubmission({getHass: () => ({callWS: async request => {
    calls.push(request.service_data);
    if (calls.length === 1) throw new Error("Connection lost after submitting");
    return {response: pending};
  }})});
  form.text.value = "Dinner tomorrow 6pm";
  await form.form.submit(event);
  assert.match(form.status.textContent, /Connection lost/);
  await form.form.submit(event);
  assert.match(calls[0].source_id, /^[a-f0-9]{32}$/);
  assert.equal(calls[1].source_id, calls[0].source_id);
  assert.equal(calls[1].text, calls[0].text);
  form.text.input();
  await form.form.submit(event);
  assert.notEqual(calls[2].source_id, calls[1].source_id);
  form.text.value = "Dinner Friday 7pm";
  await form.form.submit(event);
  assert.notEqual(calls[3].source_id, calls[2].source_id);
  assert.equal(calls[3].text, "Dinner Friday 7pm");
});
