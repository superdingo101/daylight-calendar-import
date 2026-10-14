/** Stable source identity prevents an explicit retry duplicating an unchanged draft. */
export class SubmissionAttempt {
  reset() { this.sourceId = null; }
  forDraft(text, file = null) {
    if (!this.sourceId || this.text !== text || this.file !== file) {
      const bytes = new Uint8Array(16);
      // getRandomValues also works on local HTTP HA installations where
      // crypto.randomUUID is unavailable. Never derive IDs from private text.
      globalThis.crypto.getRandomValues(bytes);
      this.sourceId = `manual-${Array.from(bytes, value => value.toString(16).padStart(2, "0")).join("")}`;
      this.text = text;
      this.file = file;
    }
    return this.sourceId;
  }
}

export function submissionMessage(response) {
  if (response.pending) {
    const count = response.pending.events.length;
    let message = `${count} ${count === 1 ? "event is" : "events are"} ready for review. Approve them in the review inbox to add them to your calendar.`;
    if (response.duplicate_events > 0) {
      message += ` ${response.duplicate_events} duplicate ${response.duplicate_events === 1 ? "event was" : "events were"} skipped.`;
    }
    return message;
  }
  if (response.duplicate_source) return "This source was already submitted. No new events are awaiting review from this attempt.";
  if (response.duplicate) return "This import contained only duplicates. No new events are awaiting review.";
  return "No events were found. Add dates and event details, then try again.";
}

const completedErrors = new Set([
  "unauthorized", "not_allowed", "not_found", "not_supported", "invalid_format",
  "home_assistant_error", "service_validation_error", "unknown_error",
]);

/** Known local PDF validation failures occur before a lifecycle record exists. */
export function pdfPreprocessingFailure(error) {
  const message = typeof error?.message === "string" ? error.message : "";
  if (/PDF (?:text exceeds the size limit|exceeds the (?:extraction resource|complexity) limit|extraction timed out)/i.test(message)) return "content";
  if (/PDF (?:could not be read|has no pages)/i.test(message)) return "unreadable";
  if (/PDF exceeds the page limit|Encrypted PDF is not supported/i.test(message)) return "format";
  if ([error?.code, error?.translation_key].includes("invalid_pdf")) return "unreadable";
  if ([error?.code, error?.translation_key].some(value => ["unsupported_pdf", "too_many_pages"].includes(value))) return "format";
  return null;
}

/** Only classify failures known to precede async_begin_submission. Source
 * size/media/capability errors can also come from the recorded parser phase.
 */
export function submissionPreprocessingFailure(error) {
  const identifiers = [error?.code, error?.translation_key];
  if (identifiers.some(value => ["unauthorized", "not_allowed", "unknown_user"].includes(value))) return "permission";
  if (identifiers.some(value => ["invalid_format", "not_found", "not_supported"].includes(value))) return "request";
  const message = typeof error?.message === "string" ? error.message : "";
  if (identifiers.includes("media_storage_unavailable") || /No local media directory configured/i.test(message)) return "media";
  if (/Daylight is unloading; retry after reload/i.test(message)) return "unloading";
  if (/text must not be empty/i.test(message)) return "text";
  return pdfPreprocessingFailure(error);
}

/** Separate rejected work from a transport failure whose result is unknown. */
export function submissionErrorMessage(error, submitted = true) {
  const detail = typeof error?.message === "string" && error.message.trim() ? error.message.trim() : "Could not submit this import.";
  const identifier = `${error?.code || ""} ${error?.translation_key || ""} ${detail}`;
  const preprocessingFailure = submissionPreprocessingFailure(error);
  const pdfFailure = pdfPreprocessingFailure(error);
  if (preprocessingFailure === "permission" || /unauthorized|not_allowed/.test(identifier)) {
    if (!submitted) {
      return `${detail} Sign in to Home Assistant again and check your connection permissions, then retry the upload.`;
    }
    return `${detail} Check that your account can use the configured AI task entity, then try again.`;
  }
  if (preprocessingFailure === "media") {
    return `${detail} Configure a local media directory in Home Assistant, or paste the event details as text.`;
  }
  if (preprocessingFailure === "unloading") return `${detail} Wait for Daylight to finish reloading, then try again.`;
  if (preprocessingFailure === "request") return `${detail} Refresh Daylight and check the submission inputs, then try again.`;
  if (preprocessingFailure === "text") return `${detail} Paste event details before importing.`;
  // Extracted text and parser resource limits can reject a PDF below the
  // upload byte limit. A smaller byte count alone need not resolve either.
  if (pdfFailure === "content") {
    return `${detail} Reduce or split the PDF content, or paste its event details as text.`;
  }
  if (pdfFailure === "unreadable") {
    return `${detail} Re-export a readable PDF, reduce or split its content, or paste its event details as text.`;
  }
  if (pdfFailure === "format") return `${detail} Use an unencrypted PDF with no more than 30 pages, or paste its event details as text.`;
  if (/source_too_large|size limit|file size/i.test(identifier)) {
    return `${detail} Choose a file no larger than 10 MiB.`;
  }
  if (/unsupported_capability|does not support.*attachment|attachments.*not supported/i.test(identifier)) {
    return `${detail} Choose an AI task provider that supports attachments, or paste the event details as text.`;
  }
  if (/AI task.*(unavailable|not found)|provider.*unavailable/i.test(identifier)) {
    return `${detail} Check the configured AI task entity in Settings, then try again.`;
  }
  if (/unsupported_media/.test(identifier)) return `${detail} Choose a JPEG, PNG, WebP image or PDF.`;
  if (/pdf.*(page|encrypted|password)|(?:page|encrypted|password).*pdf/i.test(identifier)) {
    return `${detail} Use an unencrypted PDF with no more than 30 pages, or paste its event details as text.`;
  }
  if (error?.code === "invalid_submission_response") {
    return `${detail} The submission call completed, but its result could not be validated. Check recent activity and the review inbox to confirm the outcome before retrying.`;
  }
  if (!submitted) return `${detail} Check the file and connection, then try again.`;
  if (!completedErrors.has(error?.code)) {
    return `${detail} The submission outcome is unknown and processing may still finish. Check recent activity and the review inbox before retrying this unchanged draft.`;
  }
  return `${detail} Check recent activity for any recorded failure. Resolve the problem, then try again.`;
}

const node = (tag, text = "") => {
  const result = document.createElement(tag);
  result.textContent = text;
  return result;
};

/** Rendering feedback is separate from refreshing or navigating the review UI. */
export class SubmissionFeedback {
  constructor(status, {onReview, onActivity} = {}) {
    this.generation = 0;
    this.status = status;
    this.onReview = onReview;
    this.onActivity = onActivity;
    this.actions = node("div");
    this.actions.className = "submission-actions";
    this.actions.hidden = true;
  }
  clear() {
    ++this.generation;
    this.status.textContent = "";
    this.actions.replaceChildren();
    this.actions.hidden = true;
  }
  processing(message) {
    this.clear();
    this.status.textContent = message;
  }
  action(label, callback) {
    if (!callback) return;
    const generation = this.generation;
    const button = node("button", label);
    button.type = "button";
    button.addEventListener("click", async () => {
      if (button.disabled) return;
      button.disabled = true;
      try { await callback(); }
      catch {
        if (generation === this.generation) this.status.textContent += " Could not open that view. Use the Daylight navigation to try again.";
      }
      finally { button.disabled = false; }
    });
    this.actions.append(button);
    this.actions.hidden = false;
  }
  result(response) {
    this.clear();
    this.status.textContent = submissionMessage(response);
    if (response.warnings.length) this.status.textContent += ` ${response.warnings.join(" ")}`;
    if (response.pending && this.onReview) this.action("Review events", () => this.onReview(response.pending.id));
    if (this.onActivity) this.action("View recent activity", () => this.onActivity(response.activity_id || null));
  }
  error(error, submitted = true) {
    this.clear();
    this.status.textContent = submissionErrorMessage(error, submitted);
    if (submitted && this.onActivity && !submissionPreprocessingFailure(error)) this.action("Check recent activity", () => this.onActivity(null));
  }
}
