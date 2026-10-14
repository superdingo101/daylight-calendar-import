import {inspectUpload, submitFile} from "./upload_transport.js";
import {submissionMessage, validateSubmissionResponse} from "./manual_submission.js";

const node = (tag, text = "") => {
  const result = document.createElement(tag);
  result.textContent = text;
  return result;
};
const errorMessage = error => typeof error?.message === "string" ? error.message :
  "Could not import this file. Check the review inbox before trying again.";

/** A persistent native file input retains its selection during panel refreshes. */
export class FileSubmission {
  constructor({getHass, onSubmitted = () => {}}) {
    this._sourceId = null;
    this._attemptFile = null;
    this._attemptText = null;
    this.element = node("section");
    this.element.className = "manual-submission";
    this.element.setAttribute("aria-label", "Import an image or PDF");
    this.element.append(node("h2", "Upload an image or PDF"),
      node("p", "Choose a JPEG, PNG, WebP image or PDF up to 10 MiB. Extracted events will await your approval. Email setup is optional."));
    this.form = node("form");
    const fileLabel = node("label", "Image or PDF file");
    this.file = node("input");
    this.file.type = "file";
    this.file.name = "event_file";
    this.file.accept = ".jpg,.jpeg,.png,.webp,.pdf,image/jpeg,image/png,image/webp,application/pdf";
    fileLabel.append(this.file);
    this.filename = node("p", "No file selected.");
    const contextLabel = node("label", "Supporting text (optional)");
    this.text = node("textarea");
    this.text.name = "file_context";
    this.text.rows = 3;
    contextLabel.append(this.text);
    this.button = node("button", "Import file");
    this.button.type = "submit";
    this.status = node("p");
    this.status.setAttribute("role", "status");
    this.status.setAttribute("aria-live", "polite");
    this.form.append(fileLabel, this.filename, contextLabel, this.button, this.status);
    this.element.append(this.form);
    this.text.addEventListener("input", () => {
      if (!this.busy) this._sourceId = null;
    });
    this.file.addEventListener("change", async () => {
      if (this.busy) return;
      this._sourceId = null;
      const selected = this.file.files?.[0];
      this.filename.textContent = selected ? `Selected file: ${selected.name}` : "No file selected.";
      this.status.textContent = "";
      if (!selected) return;
      try {
        await inspectUpload(selected);
      } catch (error) {
        // A slow file read must not overwrite feedback for a newer selection.
        if (this.file.files?.[0] === selected) this.status.textContent = errorMessage(error);
      }
    });
    this.form.addEventListener("submit", async event => {
      event.preventDefault();
      if (this.busy) return;
      const selected = this.file.files?.[0];
      if (!selected) {
        this.status.textContent = "Choose an image or PDF file before importing.";
        this.file.focus();
        return;
      }
      // Retrying an unacknowledged service call must deduplicate the same draft,
      // even though the transport uploads fresh bytes with a new temporary ID.
      const text = (this.text.value || "").trim();
      if (!this._sourceId || this._attemptFile !== selected || this._attemptText !== text) {
        const bytes = crypto.getRandomValues(new Uint8Array(16));
        this._sourceId = `manual-${Array.from(bytes, byte => byte.toString(16).padStart(2, "0")).join("")}`;
        this._attemptFile = selected;
        this._attemptText = text;
      }
      this.busy = true;
      this.file.disabled = true;
      this.text.disabled = true;
      this.button.disabled = true;
      this.button.textContent = "Processing…";
      this.status.textContent = "Uploading file…";
      try {
        const response = validateSubmissionResponse(await submitFile(getHass(), selected, {
          text,
          sourceId: this._sourceId,
          onUploaded: () => { this.status.textContent = "Extracting events for review…"; },
        }));
        this.status.textContent = submissionMessage(response);
        if (response.warnings.length) this.status.textContent += ` ${response.warnings.join(" ")}`;
        try {
          await onSubmitted(response);
        } catch {
          this.status.textContent += " Refresh the review inbox to see the latest imports.";
        }
      } catch (error) {
        this.status.textContent = errorMessage(error);
      } finally {
        this.busy = false;
        this.file.disabled = false;
        this.text.disabled = false;
        this.button.disabled = false;
        this.button.textContent = "Import file";
      }
    });
  }
}
