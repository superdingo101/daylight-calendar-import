/** Manual text intake uses the same durable review service as email imports. */
import {isSubmissionResponse} from "./submission_response.js";

export async function submitText(hass, text, {sourceId} = {}) {
  if (!text.trim()) throw new Error("Paste event details before importing.");
  if (sourceId !== undefined && (typeof sourceId !== "string" || !sourceId.trim())) {
    throw new Error("The import source ID is invalid.");
  }
  const serviceData = {text};
  if (sourceId !== undefined) serviceData.source_id = sourceId;
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

export function submissionMessage(response) {
  if (response.pending) {
    const count = response.pending.events.length;
    return `${count} ${count === 1 ? "event is" : "events are"} ready for review. Approve them in the review inbox to add them to your calendar.`;
  }
  if (response.duplicate) return "This import contained only duplicates. No new events are awaiting review.";
  return "No events were found. Add dates and event details, then try again.";
}

const node = (tag, text = "") => {
  const result = document.createElement(tag);
  result.textContent = text;
  return result;
};

/** Keep one form alive so inbox refreshes and view changes preserve its draft. */
export class ManualSubmission {
  constructor({getHass, onSubmitted = () => {}}) {
    this.element = node("section");
    this.element.className = "manual-submission";
    this.element.setAttribute("aria-label", "Import pasted event details");
    this.element.append(node("h2", "Paste event details"));
    this.element.append(node("p", "Paste one or more events, including dates and times. Calendar routing directives can be included in the text. Email setup is optional."));
    this.form = node("form");
    const label = node("label", "Event details");
    this.text = node("textarea");
    this.text.name = "event_details";
    this.text.rows = 6;
    label.append(this.text);
    this.button = node("button", "Import events");
    this.button.type = "submit";
    this.status = node("p");
    this.status.setAttribute("role", "status");
    this.status.setAttribute("aria-live", "polite");
    this.form.append(label, this.button, this.status);
    this.element.append(this.form);
    this.text.addEventListener("input", () => {
      if (!this.busy) {
        this._sourceId = null;
        this._attemptText = null;
      }
    });
    this.form.addEventListener("submit", async event => {
      event.preventDefault();
      if (this.busy) return;
      if (!this.text.value?.trim()) {
        this.status.textContent = "Paste event details before importing.";
        this.text.focus();
        return;
      }
      this.busy = true;
      this.button.disabled = true;
      this.text.disabled = true;
      this.button.textContent = "Processing…";
      this.status.textContent = "Extracting events for review…";
      try {
        // An unchanged draft keeps its identity across explicit retries, even
        // when the service stored the import but its acknowledgement was lost.
        if (!this._sourceId || this._attemptText !== this.text.value) {
          this._sourceId = Array.from(crypto.getRandomValues(new Uint8Array(16)),
            byte => byte.toString(16).padStart(2, "0")).join("");
          this._attemptText = this.text.value;
        }
        const response = await submitText(getHass(), this.text.value, {sourceId: this._sourceId});
        this.status.textContent = submissionMessage(response);
        if (response.warnings.length) this.status.textContent += ` ${response.warnings.join(" ")}`;
        // Keep inbox loading failures separate from successful extraction feedback.
        try {
          await onSubmitted(response);
        } catch {
          this.status.textContent += " Refresh the review inbox to see the latest imports.";
        }
      } catch (error) {
        this.status.textContent = typeof error?.message === "string" ? error.message :
          "Could not import these details. Check the review inbox before trying again.";
      } finally {
        this.busy = false;
        this.button.disabled = false;
        this.text.disabled = false;
        this.button.textContent = "Import events";
      }
    });
  }
}
