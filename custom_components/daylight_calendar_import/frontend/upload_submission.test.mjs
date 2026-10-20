import assert from "node:assert/strict";
import test from "node:test";
import {FileSubmission} from "./upload_submission.js";
class FakeNode {
  constructor(tag) { this.tag = tag; this.children = []; this.attributes = {}; this.value = ""; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(name, value) { this.attributes[name] = value; }
  addEventListener(name, callback) { this[name] = callback; }
  getRootNode() { return globalThis.document; }
  contains(node) { return this === node || this.children.some(child => child.contains(node)); }
  focus() { this.focused = true; globalThis.document.activeElement = this; }
}
globalThis.document = {createElement: tag => new FakeNode(tag)};
const event = {preventDefault() {}};
const empty = {pending: null, duplicate: false, duplicate_source: false, duplicate_events: 0, warnings: []};
const pending = {...empty, pending: {id: "pending-1", events: [{}]}};
const fileId = "a".repeat(32);
const image = () => new File([new Uint8Array([137,80,78,71,13,10,26,10])], "<image>.png", {type: "image/png"});
const pdf = () => new File(["%PDF-1.4\n"], "events.pdf", {type: "application/pdf"});
function setup(response = pending, callbacks = {}) {
  const uploads = [];
  const services = [];
  const hass = {
    fetchWithAuth: async (url, options) => {
      uploads.push({url, options});
      return {status: 200, json: async () => ({file_id: fileId})};
    },
    callWS: async request => { services.push(request); return {response}; },
  };
  const form = new FileSubmission({getHass: () => hass, ...callbacks});
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
    assert.deepEqual(services[0].service_data, {file_id: fileId, text: form.text.value, source_id: form.attempt.sourceId});
    assert.match(services[0].service_data.source_id, /^manual-[a-f0-9]{32}$/);
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

test("malformed acknowledged upload results do not imply ongoing processing", async () => {
  const {form} = setup({});
  form.file.files = [pdf()];
  await form.form.submit(event);
  assert.match(form.status.textContent, /submission call completed/);
  assert.doesNotMatch(form.status.textContent, /processing may still finish/);
  assert.equal(form.button.disabled, false);
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

test("under-limit PDF extraction rejections preserve the draft and restore controls", async () => {
  // A complete one-page PDF, including byte offsets and trailer, rather than
  // only a signature: the service rejection follows a valid file upload.
  const objects = ["<< /Type /Catalog /Pages 2 0 R >>", "<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
    "<< /Type /Page /Parent 2 0 R /MediaBox [0 0 100 100] >>"];
  let contents = "%PDF-1.4\n";
  const offsets = [0];
  for (const [index, object] of objects.entries()) {
    offsets.push(contents.length);
    contents += `${index + 1} 0 obj\n${object}\nendobj\n`;
  }
  const xref = contents.length;
  contents += `xref\n0 4\n0000000000 65535 f \n${offsets.slice(1).map(offset => `${String(offset).padStart(10, "0")} 00000 n \n`).join("")}trailer\n<< /Size 4 /Root 1 0 R >>\nstartxref\n${xref}\n%%EOF\n`;
  for (const message of ["PDF text exceeds the size limit", "PDF exceeds the extraction resource limit", "PDF extraction timed out", "PDF could not be read"]) {
    const {form, hass, uploads, services} = setup(pending, {onActivity() {}});
    const selected = new File([contents], "events.pdf", {type: "application/pdf"});
    assert.ok(selected.size < 10 * 1024 * 1024);
    hass.callWS = async request => {
      services.push(request);
      throw {code: "home_assistant_error", message};
    };
    form.file.files = [selected];
    form.text.value = "Calendar: Family";
    await form.form.submit(event);
    assert.equal(uploads[0].options.method, "POST");
    assert.equal(services[0].service, "submit_pdf");
    assert.equal(form.file.files[0], selected);
    assert.equal(form.text.value, "Calendar: Family");
    assert.equal(form.busy, false);
    assert.equal(form.file.disabled, false);
    assert.equal(form.text.disabled, false);
    assert.equal(form.button.disabled, false);
    assert.equal(form.button.textContent, "Import file");
    assert.match(form.status.textContent, /[Rr]educe or split.*paste its event details/);
    assert.doesNotMatch(form.status.textContent, /10 MiB|30 pages|Check recent activity|outcome is unknown/);
    assert.equal(form.feedback.actions.hidden, true);
    assert.equal(form.feedback.actions.children.length, 0);
  }
});

test("HTTP upload authentication failures retain selection and give connection recovery", async () => {
  for (const status of [401, 403]) {
    const {form, hass, services} = setup();
    hass.fetchWithAuth = async () => ({status});
    const selected = image();
    form.file.files = [selected];
    await form.form.submit(event);
    assert.equal(services.length, 0);
    assert.equal(form.file.files[0], selected);
    assert.equal(form.button.disabled, false);
    assert.match(form.status.textContent, /Sign in to Home Assistant again/);
    assert.doesNotMatch(form.status.textContent, /AI task/);
    assert.equal(form.feedback.actions.children.length, 0);
  }
});

test("missing local media rejects image or scanned PDF before activity creation", async () => {
  for (const selected of [image(), pdf()]) {
    const {form, hass, services} = setup(pending, {onActivity() {}});
    hass.callWS = async request => {
      services.push(request);
      throw {code: "home_assistant_error", message: "No local media directory configured"};
    };
    form.file.files = [selected];
    form.text.value = "Calendar: Family";
    await form.form.submit(event);
    assert.equal(services.length, 1);
    assert.equal(form.file.files[0], selected);
    assert.equal(form.text.value, "Calendar: Family");
    assert.equal(form.busy, false);
    assert.equal(form.file.disabled, false);
    assert.equal(form.text.disabled, false);
    assert.equal(form.button.disabled, false);
    assert.equal(form.button.textContent, "Import file");
    assert.match(form.status.textContent, /Configure a local media directory.*paste/);
    assert.doesNotMatch(form.status.textContent, /Check recent activity|outcome is unknown/);
    assert.equal(form.feedback.actions.hidden, true);
    assert.equal(form.feedback.actions.children.length, 0);
  }
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

test("file retry preserves selection/source identity and rotates for changed context or file", async () => {
  const {form, hass, services, uploads} = setup();
  hass.callWS = async request => { services.push(request); throw {code: "timeout", message: "Timed out"}; };
  const selected = image();
  form.file.files = [selected];
  form.text.value = "Family calendar";
  await form.form.submit(event);
  const initial = services[0].service_data.source_id;
  assert.match(form.status.textContent, /processing may still finish/);
  assert.equal(form.file.disabled, false);
  assert.equal(form.file.files[0], selected);
  await form.form.submit(event);
  assert.equal(services[1].service_data.source_id, initial);
  assert.equal(uploads.filter(request => request.options.method === "POST").length, 2);
  form.text.value = "Work calendar";
  await form.form.submit(event);
  assert.notEqual(services[2].service_data.source_id, initial);
  const withContext = services[2].service_data.source_id;
  form.text.input();
  assert.equal(form.status.textContent, "");
  await form.form.submit(event);
  assert.notEqual(services[3].service_data.source_id, withContext);
  const beforeFile = services[3].service_data.source_id;
  form.file.files = [pdf()];
  await form.file.change();
  await form.form.submit(event);
  assert.notEqual(services[4].service_data.source_id, beforeFile);
});

test("file checking, upload and extraction show distinct progress stages", async () => {
  const {form, hass} = setup();
  let uploaded, parsed;
  hass.fetchWithAuth = async (path, options) => {
    if (options.method === "DELETE") return {status: 404};
    return new Promise(resolve => {uploaded = resolve;});
  };
  hass.callWS = () => new Promise(resolve => {parsed = resolve;});
  form.file.files = [pdf()];
  const running = form.form.submit(event);
  assert.match(form.status.textContent, /Checking file/);
  await new Promise(resolve => setImmediate(resolve));
  assert.match(form.status.textContent, /Uploading file/);
  uploaded({status: 200, json: async () => ({file_id: fileId})});
  await new Promise(resolve => setImmediate(resolve));
  assert.match(form.status.textContent, /Extracting events/);
  parsed({response: pending});
  await running;
  assert.match(form.status.textContent, /ready for review/);
});

test("a stale file selection check cannot overwrite a completed submission result", async () => {
  const {form} = setup();
  const selected = image();
  const slice = selected.slice.bind(selected);
  let first = true, finish;
  selected.slice = (...args) => {
    if (first) {
      first = false;
      return {arrayBuffer: () => new Promise(resolve => {finish = resolve;})};
    }
    return slice(...args);
  };
  form.file.files = [selected];
  const checking = form.file.change();
  await form.form.submit(event);
  assert.match(form.status.textContent, /ready for review/);
  finish(new Uint8Array([1, 2, 3]).buffer);
  await checking;
  assert.match(form.status.textContent, /ready for review/);
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
  assert.match(form.status.textContent, /source was already submitted/);
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
  assert.equal(form.attempt.sourceId, services[0].service_data.source_id);
  finish({response: pending});
  await running;
});

test("file descriptions and keyboard processing feedback survive error recovery without refocusing", async () => {
  let reject;
  const {form, hass} = setup();
  assert.equal(form.file.attributes["aria-describedby"], `${form.help.id} ${form.filename.id}`);
  assert.equal(form.text.attributes["aria-describedby"], form.help.id);
  assert.equal(form.form.children[0].children[0], form.file);
  assert.equal(form.form.children[2].children[0], form.text);
  assert.equal(form.status.attributes["aria-atomic"], "true");
  assert.equal(form.status.attributes.tabindex, "-1");
  form.file.files = [image()];
  hass.fetchWithAuth = () => new Promise((resolve, fail) => {reject = fail;});
  form.button.focus();
  form.status.focus = () => {
    assert.equal(form.button.disabled, undefined);
    assert.equal(form.file.disabled, undefined);
    assert.match(form.status.textContent, /Checking file/);
    document.activeElement = form.status;
  };
  const submitting = form.form.submit(event);
  assert.equal(document.activeElement, form.status);
  assert.equal(form.button.attributes["aria-busy"], "true");
  assert.equal(form.form.attributes["aria-busy"], undefined);
  await new Promise(resolve => setImmediate(resolve));
  const outside = new FakeNode("button");
  outside.focus();
  reject(new Error("Connection lost"));
  await submitting;
  assert.equal(document.activeElement, outside);
  assert.equal(form.button.attributes["aria-busy"], "false");
  assert.equal(form.file.disabled, false);
  assert.match(form.status.textContent, /Connection lost/);
});


test("duplicate flyer can be re-imported into review without bypassing normal retries", async () => {
  for (const selected of [image(), pdf()]) {
    const duplicate = {...empty, duplicate: true, duplicate_events: 1};
    const {form, hass, services} = setup(duplicate);
    let n = 0;
    hass.callWS = async request => {
      services.push(request);
      return {response: ++n === 1 ? duplicate : pending};
    };
    form.file.files = [selected];
    await form.form.submit(event);
    assert.equal(form.feedback.actions.children[0].textContent, "Re-import for review");
    assert.equal(services[0].service_data.reimport_duplicates, undefined);
    const initial = services[0].service_data.source_id;
    await form.feedback.actions.children[0].click();
    assert.equal(services.length, 2);
    assert.equal(services[1].service_data.reimport_duplicates, true);
    assert.notEqual(services[1].service_data.source_id, initial);
    assert.equal(services[1].service_data.text, "");
    assert.equal(form.file.files[0], selected);
    assert.match(form.status.textContent, /ready for review/);
    assert.equal(form.feedback.actions.children.length, 0);
  }
});


test("a source-only duplicate offers a fresh, opt-in upload attempt", async () => {
  const duplicate = {...empty, duplicate: true, duplicate_source: true};
  const {form, services} = setup(duplicate);
  form.file.files = [pdf()];
  await form.form.submit(event);
  assert.equal(form.feedback.actions.children[0].textContent, "Re-import for review");
  await form.feedback.actions.children[0].click();
  assert.equal(services.length, 2);
  assert.notEqual(services[0].service_data.source_id, services[1].service_data.source_id);
  assert.equal(services[1].service_data.reimport_duplicates, true);
});


test("uncertain image/PDF re-import preserves bypass and source identity on manual retry", async () => {
  for (const [selected, failure] of [[image(), {code: "timeout", message: "Timed out"}],
    [pdf(), new Error("WebSocket connection lost")]]) {
    const duplicate = {...empty, duplicate: true, duplicate_events: 1};
    const {form, hass, services, uploads} = setup(duplicate);
    hass.callWS = async request => {
      services.push(request);
      if (services.length === 1) return {response: duplicate};
      if (services.length === 2) throw failure;
      return {response: pending};
    };
    form.file.files = [selected];
    form.text.value = "Calendar: Family";
    await form.form.submit(event);
    const firstId = services[0].service_data.source_id;
    await form.feedback.actions.children[0].click();
    assert.notEqual(services[1].service_data.source_id, firstId);
    assert.equal(services[1].service_data.reimport_duplicates, true);
    assert.match(form.status.textContent, /outcome is unknown/);
    assert.equal(form.attempt.reimportDuplicates, true);
    await form.form.submit(event);
    assert.equal(services[2].service_data.source_id, services[1].service_data.source_id);
    assert.equal(services[2].service_data.reimport_duplicates, true);
    assert.equal(form.attempt.reimportDuplicates, false);
    assert.match(form.status.textContent, /ready for review/);
    assert.equal(uploads.filter(({options}) => options.method === "POST").length, 3);
    await form.form.submit(event);
    assert.equal(services[3].service_data.source_id, services[2].service_data.source_id);
    assert.equal(services[3].service_data.reimport_duplicates, undefined);
  }
});

test("changing supporting text or selected file drops an uncertain re-import bypass", async () => {
  for (const kind of ["context", "file"]) {
    const duplicate = {...empty, duplicate: true, duplicate_events: 1};
    const {form, hass, services} = setup(duplicate);
    hass.callWS = async request => {
      services.push(request);
      if (services.length === 1) return {response: duplicate};
      throw {code: "timeout", message: "Timed out"};
    };
    form.file.files = [image()];
    form.text.value = "Family";
    await form.form.submit(event);
    await form.feedback.actions.children[0].click();
    assert.equal(form.attempt.reimportDuplicates, true);
    const originalId = services[1].service_data.source_id;
    if (kind === "context") {
      // Programmatic changes are detected by forDraft even without an input event.
      form.text.value = "Work";
    } else {
      form.file.files = [pdf()];
      await form.file.change();
    }
    await form.form.submit(event);
    assert.notEqual(services[2].service_data.source_id, originalId);
    assert.equal(services[2].service_data.reimport_duplicates, undefined);
    assert.equal(form.attempt.reimportDuplicates, false);
  }
});

test("known file service rejection terminates the re-import override", async () => {
  const {form, hass, services} = setup({...empty, duplicate: true, duplicate_events: 1});
  hass.callWS = async request => {
    services.push(request);
    if (services.length === 1) {
      return {response: {...empty, duplicate: true, duplicate_events: 1}};
    }
    if (services.length === 2) {
      throw {code: "service_validation_error", message: "Service rejected"};
    }
    return {response: pending};
  };
  form.file.files = [pdf()];
  await form.form.submit(event);
  await form.feedback.actions.children[0].click();
  assert.equal(services[1].service_data.reimport_duplicates, true);
  assert.equal(form.attempt.reimportDuplicates, false);
  await form.form.submit(event);
  assert.equal(services[2].service_data.reimport_duplicates, undefined);
  assert.equal(services[2].service_data.source_id, services[1].service_data.source_id);
});
