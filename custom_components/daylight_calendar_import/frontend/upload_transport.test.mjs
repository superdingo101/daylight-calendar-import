import assert from "node:assert/strict";
import test from "node:test";
import {inspectUpload, MAX_UPLOAD_BYTES, submitFile} from "./upload_transport.js";
const ID = "1234567890abcdef1234567890abcdef";
const PNG = () => new File([new Uint8Array([137, 80, 78, 71, 13, 10, 26, 10])], "schedule.png");
const PDF = () => new File(["%PDF-1.7"], "schedule.pdf");
const noEvents = () => ({pending: null, duplicate: false, duplicate_source: false, duplicate_events: 0, warnings: []});
function connection(options = {}) {
  const http = [], ws = [];
  return {http, ws,
    fetchWithAuth: async (path, args) => {
      http.push({path, ...args});
      if (args.method === "DELETE") {
        if (options.cleanupError) throw options.cleanupError;
        return {status: 404};
      }
      if (options.networkError) throw options.networkError;
      return {status: options.status ?? 200, json: async () => {
        if (options.jsonError) throw options.jsonError;
        return {file_id: Object.hasOwn(options, "fileId") ? options.fileId : ID};
      }};
    },
    callWS: async args => {
      ws.push(args);
      if (options.error) throw options.error;
      return {response: Object.hasOwn(options, "response") ? options.response : {pending: null, duplicate: false, duplicate_source: false, duplicate_events: 0, warnings: []}};
    },
  };
}

test("allowed signatures determine routing independently of extension and MIME", async () => {
  for (const [file, kind, mediaType] of [
    [PNG(), "image", "image/png"],
    [new File([new Uint8Array([255, 216, 255])], "wrong.pdf", {type: "application/pdf"}), "image", "image/jpeg"],
    [new File(["RIFF1234WEBP"], "photo.bin"), "image", "image/webp"],
    [PDF(), "pdf", "application/pdf"],
  ]) assert.deepEqual(await inspectUpload(file), {kind, mediaType});
  for (const content of ["RIFF1234WAV ", "not a PDF", "R"]) {
    await assert.rejects(inspectUpload(new File([content], "fake.pdf")), {code: "unsupported_media"});
  }
});

test("size validation precedes reading/upload; the exact 10MiB bound is accepted", async () => {
  let read = false;
  const huge = {name: "big.png", size: MAX_UPLOAD_BYTES + 1, slice: () => {read = true;}};
  const hass = connection();
  await assert.rejects(submitFile(hass, huge), {code: "source_too_large"});
  assert.equal(read, false);
  assert.equal(hass.http.length, 0);
  assert.equal((await inspectUpload(new File([PNG(), new Uint8Array(MAX_UPLOAD_BYTES - 8)], "limit.png"))).kind, "image");
  for (const file of [null, {name: "x", size: 1.5, slice() {}}, {name: "", size: 1, slice() {}}]) {
    await assert.rejects(inspectUpload(file), {code: "invalid_file"});
  }
  await assert.rejects(inspectUpload(new File([], "empty.png")), {code: "empty_attachment"});
});

test("authenticated multipart upload sends only file ID and text context over websocket", async () => {
  for (const [file, service, uploadedName] of [[PNG(), "submit_image", "daylight-upload.png"], [PDF(), "submit_pdf", "daylight-upload.pdf"]]) {
    const hass = connection();
    let notified = false;
    assert.deepEqual(await submitFile(hass, file, {text: "  family calendar  ", sourceId: "stable", onUploaded: () => {notified = true;}}),
      {pending: null, duplicate: false, duplicate_source: false, duplicate_events: 0, warnings: []});
    assert.equal(notified, true);
    assert.equal(hass.http[0].path, "/api/file_upload");
    assert.equal(hass.http[0].method, "POST");
    assert.ok(hass.http[0].body instanceof FormData);
    assert.equal(hass.http[0].body.get("file").name, uploadedName);
    assert.deepEqual(hass.ws[0], {type: "call_service", domain: "daylight_calendar_import", service,
      service_data: {file_id: ID, text: "  family calendar  ", source_id: "stable"}, return_response: true});
    assert.deepEqual(hass.http[1], {path: "/api/file_upload", method: "DELETE", headers: {"Content-Type": "application/json"}, body: JSON.stringify({file_id: ID})});
  }
});

test("uploaded filenames are safe and based on signatures, without modifying original bytes", async () => {
  const cases = [
    [new File([new Uint8Array([255, 216, 255])], "Screenshot_20261024-123658~2.jpg",
      {type: "image/png"}), "daylight-upload.jpg", "submit_image"],
    [new File([PNG()], "family/..schedule.jpeg",
      {type: "application/pdf"}), "daylight-upload.png", "submit_image"],
    [new File(["RIFF1234WEBP"], "club\\flyer~1.png",
      {type: "image/jpeg"}), "daylight-upload.webp", "submit_image"],
    [new File(["%PDF-1.7"], "school/../bulletin.jpg",
      {type: "image/jpeg"}), "daylight-upload.pdf", "submit_pdf"],
  ];
  for (const [file, expectedName, expectedService] of cases) {
    const hass = connection();
    const originalName = file.name;
    await submitFile(hass, file);
    const uploadedFile = hass.http[0].body.get("file");
    assert.equal(uploadedFile.name, expectedName);
    assert.doesNotMatch(uploadedFile.name, /~|\.\.|[/\\]/);
    assert.deepEqual(new Uint8Array(await uploadedFile.arrayBuffer()), new Uint8Array(await file.arrayBuffer()));
    assert.equal(file.name, originalName);
    assert.equal(hass.ws[0].service, expectedService);
  }
});

test("invalid context never uploads", async () => {
  for (const options of [{text: null}, {sourceId: " "}, {sourceId: 1}]) {
    const hass = connection();
    await assert.rejects(submitFile(hass, PNG(), options), {code: "invalid_submission"});
    assert.equal(hass.http.length, 0);
  }
});

test("HTTP/network/JSON/malformed ID failures never invoke ingestion", async () => {
  for (const [status, code] of [[400, "upload_failed"], [413, "source_too_large"], [401, "unauthorized"], [403, "unauthorized"], [500, "upload_failed"]]) {
    const hass = connection({status});
    await assert.rejects(submitFile(hass, PNG()), status === 400 ? {code, message: /HTTP 400/} : {code});
    assert.equal(hass.ws.length, 0);
    assert.equal(hass.http.length, 1);
  }
  for (const fileId of [null, 123, " ", "not-an-upload-id"]) {
    const hass = connection({fileId});
    await assert.rejects(submitFile(hass, PNG()), {code: "invalid_upload_response"});
    assert.equal(hass.ws.length, 0);
  }
  for (const options of [{networkError: new Error("offline")}, {jsonError: new Error("invalid JSON")}]) {
    const hass = connection(options);
    await assert.rejects(submitFile(hass, PNG()), /offline|invalid JSON/);
    assert.equal(hass.ws.length, 0);
  }
});

test("known service rejections clean unconsumed uploads and retain structured error details", async () => {
  for (const code of ["unauthorized", "not_allowed", "not_found", "not_supported", "invalid_format", "home_assistant_error", "service_validation_error", "unknown_error"]) {
    const error = {code, message: "provider unsupported", translation_key: "unsupported_media"};
    const hass = connection({error});
    await assert.rejects(submitFile(hass, PNG()), failure => failure === error);
    assert.equal(hass.http[1].method, "DELETE");
    assert.equal(hass.ws.length, 1);
  }
});

test("unacknowledged disconnects/timeouts cannot race parser by deleting its file", async () => {
  for (const error of [new Error("Disconnected"), {code: "timeout", message: "timeout"}]) {
    const hass = connection({error});
    await assert.rejects(submitFile(hass, PNG()), failure => failure === error);
    assert.equal(hass.http.length, 1);
    assert.equal(hass.ws.length, 1);
  }
});

test("cleanup failure preserves successful result or original service failure", async () => {
  const cleanupError = new Error("offline");
  assert.deepEqual(await submitFile(connection({cleanupError}), PNG()), {pending: null, duplicate: false, duplicate_source: false, duplicate_events: 0, warnings: []});
  const error = {code: "service_validation_error", message: "unsupported"};
  await assert.rejects(submitFile(connection({error, cleanupError}), PNG()), failure => failure === error);
});

test("callback failures and malformed acknowledged service results clean unused files", async () => {
  const hass = connection();
  await assert.rejects(submitFile(hass, PNG(), {onUploaded: () => {throw new Error("callback failed");}}), /callback failed/);
  assert.equal(hass.ws.length, 0);
  assert.equal(hass.http[1].method, "DELETE");
  for (const response of [null, [], "unexpected"]) {
    const bad = connection({response});
    await assert.rejects(submitFile(bad, PNG()), /Check recent activity before retrying/);
    assert.equal(bad.http[1].method, "DELETE");
  }
});

test("malformed submission fields never report an acknowledged import as successful", async () => {
  const invalid = [
    {}, {pending: null, duplicate: false, warnings: []},
    ...["duplicate", "duplicate_source", "duplicate_events", "warnings", "pending"].map(field => {
      const response = noEvents();
      delete response[field];
      return response;
    }),
    ...[null, 0, "false"].map(duplicate => ({...noEvents(), duplicate})),
    ...[null, 0, "false"].map(duplicate_source => ({...noEvents(), duplicate_source})),
    ...[-1, 1.5, null, "0", NaN, Infinity].map(duplicate_events => ({...noEvents(), duplicate_events})),
    ...[null, "warning", [1], [null], [{}]].map(warnings => ({...noEvents(), warnings})),
    ...[{}, [], 1, "pending", {id: "", events: [{}]}, {id: " ", events: [{}]},
      {id: 1, events: [{}]}, {id: "pending", events: []}, {id: "pending", events: null},
      {id: "pending", events: "events"}].map(pending => ({...noEvents(), pending})),
    ...[null, "pending_review", "duplicate", "failed", 1].map(status => ({...noEvents(), status})),
    ...["", " ", 1, {}, []].map(activity_id => ({...noEvents(), activity_id})),
    {...noEvents(), duplicate: true, status: "no_events"},
    {...noEvents(), duplicate: true},
    {...noEvents(), duplicate_source: true},
    {...noEvents(), duplicate_events: 1},
    {...noEvents(), pending: {id: "pending", events: [{}]}, duplicate: true},
    {...noEvents(), pending: {id: "pending", events: [{}]}, duplicate_source: true},
    {...noEvents(), pending: {id: "pending", events: [{}]}, duplicate_events: 1},
    {...noEvents(), pending: {id: "pending", events: [{}]}, status: "no_events"},
    ...[
      {...noEvents(), duplicate: true, duplicate_source: true, duplicate_events: 1},
      {...noEvents(), duplicate: true, duplicate_source: true, pending: {id: "pending", events: [{}]}},
      {...noEvents(), duplicate: true, duplicate_source: true, duplicate_events: 1, pending: {id: "pending", events: [{}]}},
    ].flatMap(outcome => [outcome, {...outcome,
      status: outcome.pending ? "pending_review" : "duplicate", activity_id: "activity"}]),
  ];
  for (const response of invalid) {
    const hass = connection({response});
    await assert.rejects(submitFile(hass, PNG()), {code: "invalid_submission_response"});
    assert.equal(hass.ws.length, 1);
    assert.equal(hass.http[1].method, "DELETE");
  }
});

test("legacy and lifecycle submission outcomes pass through without losing details", async () => {
  for (const response of [
    noEvents(),
    {...noEvents(), status: "no_events", activity_id: "activity"},
    {...noEvents(), duplicate: true, duplicate_source: true, status: "duplicate", activity_id: null},
    {...noEvents(), duplicate: true, duplicate_source: true, status: "duplicate", activity_id: "activity"},
    {...noEvents(), duplicate: true, duplicate_events: 2, warnings: ["Duplicates skipped"]},
    {...noEvents(), pending: {id: "pending", events: [{summary: "Event"}]},
      status: "pending_review", activity_id: "pending", warnings: ["Check dates"]},
    {...noEvents(), pending: {id: "partial", events: [{summary: "New event"}]},
      duplicate: true, duplicate_events: 1, status: "pending_review", activity_id: "partial"},
  ]) {
    assert.equal(await submitFile(connection({response}), PNG()), response);
  }
});

test("lifecycle fields must be a complete, consistent pair with durable activity correlation", async () => {
  const outcomes = [
    [noEvents(), "no_events"],
    [{...noEvents(), pending: {id: "pending", events: [{}]}}, "pending_review"],
    [{...noEvents(), duplicate: true, duplicate_events: 1}, "duplicate"],
    [{...noEvents(), duplicate: true, duplicate_source: true}, "duplicate"],
    [{...noEvents(), pending: {id: "partial", events: [{}]}, duplicate: true,
      duplicate_events: 1}, "pending_review"],
  ];
  for (const [outcome, status] of outcomes) {
    // Older integrations omit both keys. Processed modern outcomes correlate
    // with activity, including duplicate events and partially deduplicated sets.
    const activityId = outcome.pending?.id ?? "activity";
    for (const response of [outcome, {...outcome, status, activity_id: activityId}]) {
      assert.equal(await submitFile(connection({response}), PNG()), response);
    }
    const invalid = [
      {...outcome, status}, {...outcome, activity_id: "activity"}, {...outcome, activity_id: null},
      {...outcome, status: undefined}, {...outcome, activity_id: undefined},
      {...outcome, status: undefined, activity_id: undefined},
      {...outcome, status, activity_id: undefined},
      ...[undefined, null, "wrong_status"].map(badStatus =>
        ({...outcome, status: badStatus, activity_id: "activity"})),
      ...["", " ", 1, {}, []].map(activity_id => ({...outcome, status, activity_id})),
      Object.assign(Object.create({status, activity_id: "activity"}), outcome),
      Object.assign(Object.create({activity_id: "activity"}), outcome, {status}),
      Object.assign(Object.create({status}), outcome, {activity_id: "activity"}),
    ];
    const skippedSource = outcome.pending === null && outcome.duplicate_source && outcome.duplicate_events === 0;
    const nullActivity = {...outcome, status, activity_id: null};
    if (skippedSource) assert.equal(await submitFile(connection({response: nullActivity}), PNG()), nullActivity);
    else invalid.push(nullActivity);
    if (outcome.pending) invalid.push({...outcome, status, activity_id: "different-activity"});
    for (const response of invalid) {
      const hass = connection({response});
      await assert.rejects(submitFile(hass, PNG()), {code: "invalid_submission_response"});
      assert.equal(hass.http[1].method, "DELETE");
    }
  }
});

test("explicit retries upload fresh IDs while keeping stable source ID", async () => {
  const hass = connection();
  let index = 0;
  const fetch = hass.fetchWithAuth;
  hass.fetchWithAuth = async (path, options) => {
    const response = await fetch(path, options);
    if (options.method === "POST") return {status: 200, json: async () => ({file_id: `${++index}`.padStart(32, "0")})};
    return response;
  };
  await submitFile(hass, PDF(), {sourceId: "stable"});
  await submitFile(hass, PDF(), {sourceId: "stable"});
  assert.deepEqual(hass.ws.map(request => request.service_data), [
    {file_id: "1".padStart(32, "0"), text: "", source_id: "stable"},
    {file_id: "2".padStart(32, "0"), text: "", source_id: "stable"},
  ]);
});


test("never-settling cleanup cannot delay acknowledged success, rejection or callback failure", {timeout: 1000}, async () => {
  for (const options of [{}, {error: {code: "service_validation_error", message: "unsupported"}}]) {
    const hass = connection(options);
    const fetch = hass.fetchWithAuth;
    hass.fetchWithAuth = (path, args) => args.method === "DELETE" ? new Promise(() => {}) : fetch(path, args);
    if (options.error) await assert.rejects(submitFile(hass, PNG()), failure => failure === options.error);
    else assert.deepEqual(await submitFile(hass, PNG()), {pending: null, duplicate: false, duplicate_source: false, duplicate_events: 0, warnings: []});
  }
  const hass = connection();
  const fetch = hass.fetchWithAuth;
  hass.fetchWithAuth = (path, args) => args.method === "DELETE" ? new Promise(() => {}) : fetch(path, args);
  await assert.rejects(submitFile(hass, PNG(), {onUploaded: () => {throw new Error("callback failed");}}), /callback failed/);
});


test("raw upload context reaches server validation before whitespace normalization", async () => {
  const text = " ".repeat(1_000_000) + "x";
  const error = {code: "invalid_format", message: "Source text exceeds the 1,000,000-character limit."};
  for (const file of [PNG(), PDF()]) {
    const hass = connection({error});
    await assert.rejects(submitFile(hass, file, {text}), failure => failure === error);
    assert.equal(hass.ws[0].service_data.text, text);
    assert.equal(hass.http[1].method, "DELETE");
  }
});
