import assert from "node:assert/strict";
import test from "node:test";
import {SubmissionAttempt, SubmissionFeedback, pdfPreprocessingFailure, submissionPreprocessingFailure, submissionErrorMessage, submissionMessage} from "./submission_feedback.js";
class FakeNode {
  constructor(tag) { this.tag = tag; this.children = []; this.textContent = ""; }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  addEventListener(name, callback) { this[name] = callback; }
}
globalThis.document = {createElement: tag => new FakeNode(tag)};
const empty = {pending: null, duplicate: false, duplicate_source: false, duplicate_events: 0, warnings: []};
const pending = {...empty, activity_id: "activity-1", pending: {id: "pending-1", events: [{}]}};

test("attempt IDs are stable for identical retries and rotate for changed text, file or reset", () => {
  const attempt = new SubmissionAttempt();
  const id = attempt.forDraft("Dinner");
  assert.match(id, /^manual-[0-9a-f]{32}$/);
  assert.equal(attempt.forDraft("Dinner"), id);
  assert.notEqual(attempt.forDraft("Dentist"), id);
  const file = new File(["%PDF-1.4"], "calendar.pdf");
  const withFile = attempt.forDraft("Dinner", file);
  assert.equal(attempt.forDraft("Dinner", file), withFile);
  assert.notEqual(attempt.forDraft("Dinner", new File([file], file.name)), withFile);
  const beforeReset = attempt.forDraft("Dinner", file);
  attempt.reset();
  assert.notEqual(attempt.forDraft("Dinner", file), beforeReset);
  assert.notEqual(new SubmissionAttempt().forDraft("Dinner", file), attempt.sourceId);
});

test("review results report partial duplicates, no events and repeated sources distinctly", () => {
  assert.match(submissionMessage({...pending, duplicate_events: 1}), /1 duplicate event was skipped/);
  assert.match(submissionMessage({...pending, duplicate_events: 3}), /3 duplicate events were skipped/);
  assert.match(submissionMessage({...empty, duplicate: true, duplicate_source: true}), /source was already submitted/);
  assert.match(submissionMessage({...empty, duplicate: true}), /only duplicates/);
  assert.match(submissionMessage(empty), /Add dates and event details/);
  assert.doesNotMatch(submissionMessage(pending), /created|added to your calendar/);
});

test("feedback uses exact pending/activity IDs and renders warnings as plain text", async () => {
  const status = new FakeNode("p");
  const reviewed = [], activity = [];
  const feedback = new SubmissionFeedback(status, {onReview: id => reviewed.push(id), onActivity: id => activity.push(id)});
  feedback.result({...pending, warnings: ["<img src=x onerror=alert(1)>", "Route unknown"]});
  assert.match(status.textContent, /<img/);
  assert.equal(status.children.length, 0);
  assert.equal(feedback.actions.hidden, false);
  assert.deepEqual(feedback.actions.children.map(button => button.type), ["button", "button"]);
  await feedback.actions.children[0].click();
  await feedback.actions.children[1].click();
  assert.deepEqual(reviewed, ["pending-1"]);
  assert.deepEqual(activity, ["activity-1"]);
  feedback.result({...empty, activity_id: "no-events-activity"});
  assert.equal(feedback.actions.children.length, 1);
  await feedback.actions.children[0].click();
  assert.equal(activity.at(-1), "no-events-activity");
});

test("processing and editing clear previous result links; failure links open the activity list", async () => {
  const status = new FakeNode("p");
  let viewed = "unset";
  const feedback = new SubmissionFeedback(status, {onReview() {}, onActivity: id => {viewed = id;}});
  feedback.result(pending);
  feedback.processing("Uploading file…");
  assert.equal(status.textContent, "Uploading file…");
  assert.equal(feedback.actions.hidden, true);
  assert.equal(feedback.actions.children.length, 0);
  feedback.error({code: "timeout", message: "Request timed out"});
  assert.match(status.textContent, /processing may still finish/);
  await feedback.actions.children[0].click();
  assert.equal(viewed, null);
  feedback.error({code: "unsupported_media", message: "Unsupported file"}, false);
  assert.equal(feedback.actions.children.length, 0);
  feedback.clear();
  assert.equal(status.textContent, "");
  assert.equal(feedback.actions.hidden, true);
});

test("errors give actionable permissions, provider, size, PDF and transport recovery advice", () => {
  const checks = [
    [{code: "unauthorized", message: "Denied"}, true, /account can use.*AI task/],
    [{code: "source_too_large", message: "Too large"}, false, /10 MiB/],
    [{code: "home_assistant_error", message: "Parser does not support this attachment"}, true, /provider that supports attachments/],
    [{code: "unsupported_media", message: "Unsupported format"}, false, /JPEG, PNG, WebP/],
    [{code: "home_assistant_error", message: "PDF is encrypted"}, true, /unencrypted PDF.*30 pages/],
    [{code: "home_assistant_error", message: "AI task is unavailable"}, true, /Settings/],
    [{code: "service_validation_error", message: "Wrong destination"}, true, /recorded failure.*Resolve the problem/],
    [new Error("Disconnected"), true, /outcome is unknown.*before retrying/],
    [{code: "timeout"}, true, /outcome is unknown/],
    [new Error("Network failed"), false, /Check the file and connection/],
  ];
  for (const [error, submitted, expected] of checks) assert.match(submissionErrorMessage(error, submitted), expected);
});

test("upload authentication failures do not advise changing AI task permissions", () => {
  const message = submissionErrorMessage({code: "unauthorized", message: "Upload denied"}, false);
  assert.match(message, /Sign in to Home Assistant again.*connection permissions/);
  assert.doesNotMatch(message, /AI task/);
});

test("PDF extraction limits are distinct from upload bytes, page counts and encryption", () => {
  for (const message of ["PDF text exceeds the size limit", "PDF exceeds the extraction resource limit", "PDF exceeds the complexity limit", "PDF extraction timed out"]) {
    for (const code of ["source_too_large", "home_assistant_error"]) {
      const advice = submissionErrorMessage({code, message});
      assert.match(advice, /Reduce or split the PDF content.*paste its event details/);
      assert.doesNotMatch(advice, /10 MiB|30 pages/);
    }
  }
  assert.match(submissionErrorMessage({code: "source_too_large", message: "Source attachments exceed the size limit"}), /10 MiB/);
  for (const message of ["PDF exceeds the page limit", "Encrypted PDF is not supported"]) {
    const advice = submissionErrorMessage({code: "home_assistant_error", message});
    assert.match(advice, /unencrypted PDF.*30 pages/);
    assert.doesNotMatch(advice, /10 MiB|Reduce or split/);
  }
});

test("local PDF preprocessing errors do not link to nonexistent activity records", () => {
  const feedback = new SubmissionFeedback(new FakeNode("p"), {onActivity() {}});
  for (const message of ["PDF extraction timed out", "PDF could not be read", "PDF has no pages",
    "PDF text exceeds the size limit", "PDF exceeds the extraction resource limit", "PDF exceeds the page limit", "Encrypted PDF is not supported"]) {
    for (const code of ["invalid_pdf", "home_assistant_error"]) {
      const error = {code, message};
      assert.ok(pdfPreprocessingFailure(error));
      feedback.error(error);
      assert.equal(feedback.actions.hidden, true);
      assert.equal(feedback.actions.children.length, 0);
      assert.doesNotMatch(feedback.status.textContent, /Check recent activity|outcome is unknown|10 MiB/);
      if (/could not be read|has no pages/.test(message)) {
        assert.match(feedback.status.textContent, /Re-export a readable PDF.*reduce or split.*paste its event details/);
        assert.doesNotMatch(feedback.status.textContent, /CPU|resource limit/);
      }
    }
  }
  for (const error of [null, {}, {message: 42}, {code: "timeout", message: "AI provider timed out"},
    {code: "home_assistant_error", message: "AI parsing failed"}]) {
    assert.equal(pdfPreprocessingFailure(error), null);
    feedback.error(error);
    assert.equal(feedback.actions.children.length, 1);
  }
});

test("malformed acknowledged responses are completed calls with unconfirmed results", () => {
  const message = submissionErrorMessage({code: "invalid_submission_response", message: "Unexpected submission result"});
  assert.match(message, /submission call completed.*Check recent activity and the review inbox/);
  assert.doesNotMatch(message, /processing may still finish|outcome is unknown/);
});

test("known pre-lifecycle failures give direct recovery without activity links", () => {
  const feedback = new SubmissionFeedback(new FakeNode("p"), {onActivity() {}});
  const cases = [
    [{code: "unauthorized", message: "Unauthorized"}, /account can use.*AI task/],
    [{code: "not_allowed"}, /account can use.*AI task/],
    [{code: "unknown_user"}, /account can use.*AI task/],
    [{translation_key: "media_storage_unavailable"}, /Configure a local media directory.*paste/],
    [{code: "home_assistant_error", message: "No local media directory configured"}, /Configure a local media directory.*paste/],
    [{code: "service_validation_error", message: "Daylight is unloading; retry after reload"}, /finish reloading/],
    [{code: "invalid_format", message: "Invalid service data"}, /Refresh Daylight.*inputs/],
    [{code: "not_found", message: "Service not found"}, /Refresh Daylight.*inputs/],
    [{code: "not_supported", message: "Service response not supported"}, /Refresh Daylight.*inputs/],
    [{message: "text must not be empty"}, /Paste event details/],
    [{code: "invalid_pdf"}, /Re-export a readable PDF/],
    [{code: "too_many_pages"}, /30 pages/],
    [{translation_key: "unsupported_pdf"}, /unencrypted PDF/],
  ];
  for (const [error, advice] of cases) {
    assert.ok(submissionPreprocessingFailure(error));
    feedback.error(error);
    assert.match(feedback.status.textContent, advice);
    assert.doesNotMatch(feedback.status.textContent, /Check recent activity|outcome is unknown/);
    assert.equal(feedback.actions.hidden, true);
    assert.equal(feedback.actions.children.length, 0);
  }
  // These source errors can also occur in provider validation after the
  // lifecycle record exists. Their recovery link must remain available.
  for (const error of [{code: "unsupported_capability", message: "Parser does not support this attachment"},
    {code: "home_assistant_error", message: "AI task is unavailable"},
    {code: "source_too_large", message: "Source attachments exceed the size limit"},
    {code: "timeout", message: "Connection lost"}]) {
    assert.equal(submissionPreprocessingFailure(error), null);
    feedback.error(error);
    assert.equal(feedback.actions.children.length, 1);
  }
});

test("navigation callback failures retain extraction outcome and allow a fresh attempt", async () => {
  const status = new FakeNode("p");
  const feedback = new SubmissionFeedback(status, {onReview: async () => {throw new Error("offline");}});
  feedback.result(pending);
  const button = feedback.actions.children[0];
  await button.click();
  assert.match(status.textContent, /ready for review.*Could not open that view/);
  assert.equal(button.disabled, false);
});

test("navigation actions cannot run concurrently", async () => {
  let complete, calls = 0;
  const feedback = new SubmissionFeedback(new FakeNode("p"), {onReview: () => {calls++; return new Promise(done => {complete = done;});}});
  feedback.result(pending);
  const button = feedback.actions.children[0];
  const first = button.click();
  assert.equal(button.disabled, true);
  await button.click();
  assert.equal(calls, 1);
  complete();
  await first;
  assert.equal(button.disabled, false);
});

test("late navigation failure cannot overwrite feedback cleared for a new draft", async () => {
  let fail;
  const status = new FakeNode("p");
  const feedback = new SubmissionFeedback(status, {onReview: () => new Promise((resolve, reject) => {fail = reject;})});
  feedback.result(pending);
  const navigating = feedback.actions.children[0].click();
  feedback.processing("Extracting events for review…");
  fail(new Error("offline"));
  await navigating;
  assert.equal(status.textContent, "Extracting events for review…");
});
