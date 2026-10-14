import assert from "node:assert/strict";
import test from "node:test";
import {FileSubmission} from "./upload_submission.js";
class FakeNode {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.value = ""; }
  append(...children) { this.children.push(...children); }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this[name] = callback; }
  focus() { this.focused = true; }
}
globalThis.document = {createElement: tag => new FakeNode(tag)};
const event = {preventDefault() {}};
const empty = {pending: null, duplicate: false, duplicate_source: false, duplicate_events: 0, warnings: []};
const pending = {...empty, pending: {id: "pending-1", events: [{}]}};
const fileId = "a".repeat(32);
const image = () => new File([new Uint8Array([137,80,78,71,13,10,26,10])], "<image>.png", {type: "image/png"});
const pdf = () => new File(["%PDF-1.4\n"], "events.pdf", {type: "application/pdf"});
function setup(response = pending) {
  const uploads = [];
  const services = [];
  const hass = {
    fetchWithAuth: async (url, options) => {
      uploads.push({url, options});
      return {status: 200, json: async () => ({file_id: fileId})};
    },
    callWS: async request => { services.push(request); return {response}; },
  };
  const form = new FileSubmission({getHass: () => hass});
  return {form, uploads, services, hass};
}

test("file form has native picker and validates missing input", async () => {
  const {form, uploads} = setup();
  assert.equal(form.file.type, "file");
  assert.match(form.file.accept, /image\/jpeg/);
  assert.match(form.file.accept, /application\/pdf/);
  assert.equal(form.file.multiple, undefined);
  await form.form.submit(event);
  assert.equal(uploads.length, 0);
  assert.equal(form.file.focused, true);
  assert.match(form.status.textContent, /Choose an image or PDF/);
});

test("selection shows filename as text and rejects invalid or oversized content", async () => {
  const {form, uploads} = setup();
  form.file.files = [image()];
  await form.file.change();
  assert.equal(form.filename.textContent, "Selected file: <image>.png");
  assert.equal(form.filename.children.length, 0);
  form.file.files = [new File(["plain text"], "events.png", {type: "image/png"})];
  await form.file.change();
  assert.match(form.status.textContent, /Choose a JPEG/);
  await form.form.submit(event);
  assert.equal(uploads.length, 0);
  assert.equal(form.file.disabled, false);
  form.file.files = [{name: "huge.pdf", size: 10 * 1024 * 1024 + 1, slice() { throw new Error("Must reject before reading"); }}];
  await form.file.change();
  assert.match(form.status.textContent, /10 MiB/);
  await form.form.submit(event);
  assert.equal(uploads.length, 0);
  form.file.files = [];
  await form.file.change();
  assert.equal(form.filename.textContent, "No file selected.");
  assert.equal(form.status.textContent, "");
});

test("images and PDFs use multipart intake with optional context", async () => {
  for (const [selected, service] of [[image(), "submit_image"], [pdf(), "submit_pdf"]]) {
    const {form, uploads, services} = setup();
    form.file.files = [selected];
    form.text.value = "Calendar: Family\nThe flyer is for next Tuesday";
    await form.file.change();
    await form.form.submit(event);
    assert.equal(uploads[0].url, "/api/file_upload");
    assert.equal(uploads[0].options.method, "POST");
    assert.ok(uploads[0].options.body instanceof FormData);
    const sent = uploads[0].options.body.get("file");
    assert.equal(sent.name, selected.name);
    assert.deepEqual(new Uint8Array(await sent.arrayBuffer()), new Uint8Array(await selected.arrayBuffer()));
    assert.equal(services[0].service, service);
    assert.deepEqual(services[0].service_data, {file_id: fileId, text: form.text.value, source_id: form._sourceId});
    assert.match(form.status.textContent, /ready for review/);
    assert.equal(form.file.files[0], selected);
    assert.equal(form.text.value, "Calendar: Family\nThe flyer is for next Tuesday");
    assert.equal(form.file.disabled, false);
    assert.equal(form.button.disabled, false);
  }
});

test("file duplicates, no-event and malformed outcomes are clear", async () => {
  for (const [response, expected] of [[{...empty, duplicate: true, duplicate_events: 1}, /only duplicates/], [empty, /No events were found/], [{}, /unexpected response/]]) {
    const {form} = setup(response);
    form.file.files = [pdf()];
    await form.form.submit(event);
    assert.match(form.status.textContent, expected);
    assert.equal(form.button.disabled, false);
  }
});

test("provider rejection preserves file selection and supporting draft", async () => {
  const {form, hass} = setup();
  hass.callWS = async () => { throw {code: "home_assistant_error", message: "Select an attachment-capable AI provider"}; };
  const selected = image();
  form.file.files = [selected];
  form.text.value = "Optional instructions";
  await form.form.submit(event);
  assert.match(form.status.textContent, /attachment-capable/);
  assert.equal(form.file.files[0], selected);
  assert.equal(form.text.value, "Optional instructions");
  assert.equal(form.button.disabled, false);
});

test("busy processing blocks repeat submits and separates refresh failure", async () => {
  const {form, hass, uploads} = setup();
  let finish;
  hass.callWS = () => new Promise(resolve => { finish = resolve; });
  form.file.files = [pdf()];
  const running = form.form.submit(event);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(form.status.textContent, /Extracting events/);
  assert.equal(form.busy, true);
  assert.equal(form.file.disabled, true);
  await form.form.submit(event);
  assert.equal(uploads.length, 1);
  finish({response: pending});
  await running;
  assert.equal(form.busy, false);
  const broken = new FileSubmission({getHass: () => ({fetchWithAuth: hass.fetchWithAuth, callWS: async () => ({response: pending})}), onSubmitted: async () => { throw new Error("Inbox offline"); }});
  broken.file.files = [pdf()];
  await broken.form.submit(event);
  assert.match(broken.status.textContent, /ready for review/);
  assert.match(broken.status.textContent, /Refresh the review inbox/);
});

test("late validation feedback cannot replace a newer file selection", async () => {
  const {form} = setup();
  let finish;
  const slow = {name: "slow.png", size: 9, slice: () => ({arrayBuffer: () => new Promise(resolve => { finish = resolve; })})};
  form.file.files = [slow];
  const checking = form.file.change();
  form.file.files = [image()];
  await form.file.change();
  finish(new Uint8Array([1, 2, 3]).buffer);
  await checking;
  assert.equal(form.filename.textContent, "Selected file: <image>.png");
  assert.equal(form.status.textContent, "");
});

test("an unknown outcome retries with a stable source ID and a fresh upload", async () => {
  const {form, hass, uploads, services} = setup();
  let attempt = 0;
  hass.fetchWithAuth = async (url, options) => {
    uploads.push({url, options});
    return {status: 200, json: async () => ({file_id: (++attempt).toString(16).padStart(32, "0")})};
  };
  hass.callWS = async request => {
    services.push(request);
    if (services.length === 1) throw new Error("WebSocket connection lost");
    return {response: {...empty, duplicate: true, duplicate_source: true}};
  };
  const selected = pdf();
  form.file.files = [selected];
  form.text.value = "Calendar: Family";
  await form.form.submit(event);
  assert.match(form.status.textContent, /connection lost/);
  await form.form.submit(event);
  assert.match(services[0].service_data.source_id, /^manual-[0-9a-f]{32}$/);
  assert.equal(services[1].service_data.source_id, services[0].service_data.source_id);
  assert.notEqual(services[1].service_data.file_id, services[0].service_data.file_id);
  assert.equal(uploads.filter(({options}) => options.method === "POST").length, 2);
  assert.match(form.status.textContent, /only duplicates/);
  assert.equal(form.file.files[0], selected);
});

test("file selection and supporting text changes rotate the submission identity", async () => {
  const {form, services} = setup();
  form.file.files = [pdf()];
  await form.form.submit(event);
  form.text.value = "Different instructions";
  await form.text.input();
  await form.form.submit(event);
  form.file.files = [image()];
  await form.file.change();
  await form.form.submit(event);
  assert.equal(new Set(services.map(request => request.service_data.source_id)).size, 3);
});

test("programmatic draft changes rotate identity without input or change events", async () => {
  const {form, services} = setup();
  form.file.files = [pdf()];
  await form.form.submit(event);
  form.text.value = "A changed calendar directive";
  await form.form.submit(event);
  form.file.files = [pdf()];
  await form.form.submit(event);
  assert.equal(new Set(services.map(request => request.service_data.source_id)).size, 3);
  await form.form.submit(event);
  assert.equal(services[3].service_data.source_id, services[2].service_data.source_id);
});

test("input events during processing cannot erase the submitted identity", async () => {
  const {form, hass, services} = setup();
  let finish;
  hass.callWS = request => {
    services.push(request);
    return new Promise(resolve => { finish = resolve; });
  };
  form.file.files = [pdf()];
  const running = form.form.submit(event);
  await new Promise(resolve => setImmediate(resolve));
  await form.text.input();
  await form.file.change();
  assert.equal(form._sourceId, services[0].service_data.source_id);
  finish({response: pending});
  await running;
});
