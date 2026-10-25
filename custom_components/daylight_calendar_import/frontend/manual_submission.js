import {isSubmissionResponse} from "./submission_response.js";
import {configureSubmissionStatus, focusSubmissionStatus} from "./submission_accessibility.js";
import {SubmissionAttempt, SubmissionFeedback, submissionOutcomeUncertain} from "./submission_feedback.js";
export {submissionMessage} from "./submission_feedback.js";

/** Manual text intake uses the same durable review service as email imports. */
export async function submitText(hass, text, {sourceId, reimportDuplicates = false} = {}) {
  if (!text.trim()) throw new Error("Paste event details before importing.");
  if (sourceId !== undefined && (typeof sourceId !== "string" || !sourceId.trim())) {
    throw new Error("The import source ID is invalid.");
  }
  const serviceData = {text};
  if (sourceId !== undefined) serviceData.source_id = sourceId;
  if (reimportDuplicates) serviceData.reimport_duplicates = true;
  const result = await hass.callWS({
    type: "call_service", domain: "daylight_calendar_import", service: "submit_text",
    service_data: serviceData, return_response: true,
  });
  return validateSubmissionResponse(result?.response);
}

/** Both manual intake forms share the existing review response contract. */
export function validateSubmissionResponse(response) {
  if (!isSubmissionResponse(response)) {
    const error = new Error("The import returned an unexpected response. Check the review inbox before trying again.");
    error.code = "invalid_submission_response";
    throw error;
  }
  return response;
}

const node = (tag, text = "") => {
  const result = document.createElement(tag);
  result.textContent = text;
  return result;
};

/** Keep one form alive so inbox refreshes and view changes preserve its draft. */
export class ManualSubmission {
  constructor({getHass, onSubmitted = () => {}, onSettled = () => {}, onReview, onActivity}) {
    this.getHass = getHass;
    this.onSubmitted = onSubmitted;
    this.onSettled = onSettled;
    this.element = node("section");
    this.element.className = "manual-submission";
    this.element.setAttribute("aria-label", "Import pasted event details");
    this.element.append(node("h2", "Paste event details"));
    this.help = node("p", "Paste one or more events, including dates and times. Calendar routing directives can be included in the text. Email setup is optional.");
    this.help.id = "daylight-text-help";
    this.element.append(this.help);
    this.form = node("form");
    const label = node("label", "Event details");
    this.text = node("textarea");
    this.text.name = "event_details";
    this.text.setAttribute("aria-describedby", this.help.id);
    this.text.rows = 6;
    label.append(this.text);
    this.button = node("button", "Import events");
    this.button.type = "submit";
    this.status = node("p");
    configureSubmissionStatus(this.status);
    this.attempt = new SubmissionAttempt();
    this.feedback = new SubmissionFeedback(this.status, {
      onReview, onActivity, onReimport: () => this.submit(true),
    });
    this.form.append(label, this.button, this.status, this.feedback.actions);
    this.text.addEventListener("input", () => {
      if (!this.busy) { this.attempt.reset(); this.feedback.clear(); }
    });
    this.element.append(this.form);
    this.form.addEventListener("submit", async event => {
      event.preventDefault();
      await this.submit();
    });
  }

  async submit(reimportDuplicates = false) {
      if (this.busy) return;
      if (!this.text.value?.trim()) {
        this.feedback.processing("Paste event details before importing.");
        this.text.focus();
        return;
      }
      this.busy = true;
      this.feedback.processing("Extracting events for review…");
      focusSubmissionStatus(this.form, this.status);
      this.button.setAttribute("aria-busy", "true");
      this.button.disabled = true;
      this.text.disabled = true;
      this.button.textContent = "Processing…";
      try {
        const sourceId = reimportDuplicates ?
          this.attempt.reimport(this.text.value) : this.attempt.forDraft(this.text.value);
        const response = await submitText(this.getHass(), this.text.value, {
          sourceId, reimportDuplicates: this.attempt.reimportDuplicates,
        });
        this.attempt.complete();
        this.feedback.result(response);
        // Keep inbox loading failures separate from successful extraction feedback.
        try {
          await this.onSubmitted(response);
        } catch {
          this.status.textContent += " Refresh the review inbox to see the latest imports.";
        }
      } catch (error) {
        if (!submissionOutcomeUncertain(error)) this.attempt.complete();
        this.feedback.error(error);
      } finally {
        this.busy = false;
        this.button.setAttribute("aria-busy", "false");
        this.button.disabled = false;
        this.text.disabled = false;
        this.button.textContent = "Import events";
        this.onSettled();
      }
  }
}
