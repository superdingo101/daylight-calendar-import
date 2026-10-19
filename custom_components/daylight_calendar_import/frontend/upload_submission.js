import {configureSubmissionStatus, focusSubmissionStatus} from "./submission_accessibility.js";
import {SubmissionAttempt, SubmissionFeedback} from "./submission_feedback.js";
import {inspectUpload, submitFile} from "./upload_transport.js";
import {validateSubmissionResponse} from "./manual_submission.js";

const node = (tag, text = "") => {
  const result = document.createElement(tag);
  result.textContent = text;
  return result;
};
const errorMessage = error => typeof error?.message === "string" ? error.message :
  "Could not import this file. Check the review inbox before trying again.";

/** A persistent native file input retains its selection during panel refreshes. */
export class FileSubmission {
  constructor({getHass, onSubmitted = () => {}, onSettled = () => {}, onReview, onActivity}) {
    this.element = node("section");
    this.element.className = "manual-submission";
    this.element.setAttribute("aria-label", "Import an image or PDF");
    this.help = node("p", "Choose a JPEG, PNG, WebP image or PDF up to 10 MiB. Extracted events will await your approval. Email setup is optional.");
    this.help.id = "daylight-file-help";
    this.element.append(node("h2", "Upload an image or PDF"), this.help);
    this.form = node("form");
    const fileLabel = node("label", "Image or PDF file");
    this.file = node("input");
    this.file.type = "file";
    this.file.name = "event_file";
    this.file.accept = ".jpg,.jpeg,.png,.webp,.pdf,image/jpeg,image/png,image/webp,application/pdf";
    fileLabel.append(this.file);
    this.filename = node("p", "No file selected.");
    this.filename.id = "daylight-selected-file";
    this.file.setAttribute("aria-describedby", `${this.help.id} ${this.filename.id}`);
    const contextLabel = node("label", "Supporting text (optional)");
    this.text = node("textarea");
    this.text.name = "file_context";
    this.text.setAttribute("aria-describedby", this.help.id);
    this.text.rows = 3;
    contextLabel.append(this.text);
    this.button = node("button", "Import file");
    this.button.type = "submit";
    this.status = node("p");
    configureSubmissionStatus(this.status);
    this._validationSequence = 0;
    this.attempt = new SubmissionAttempt();
    this.feedback = new SubmissionFeedback(this.status, {onReview, onActivity});
    this.form.append(fileLabel, this.filename, contextLabel, this.button, this.status, this.feedback.actions);
    this.text.addEventListener("input", () => {
      if (!this.busy) { this.attempt.reset(); this.feedback.clear(); }
    });
    this.element.append(this.form);
    this.file.addEventListener("change", async () => {
      if (this.busy) return;
      const sequence = ++this._validationSequence;
      const selected = this.file.files?.[0];
      this.filename.textContent = selected ? `Selected file: ${selected.name}` : "No file selected.";
      this.attempt.reset();
      this.feedback.clear();
      if (!selected) return;
      try {
        await inspectUpload(selected);
      } catch (error) {
        // A slow file read must not overwrite feedback for a newer selection.
        if (sequence === this._validationSequence && this.file.files?.[0] === selected && !this.busy) this.status.textContent = errorMessage(error);
      }
    });
    this.form.addEventListener("submit", async event => {
      event.preventDefault();
      if (this.busy) return;
      ++this._validationSequence; // Supersede any earlier selection-only validation.
      const selected = this.file.files?.[0];
      if (!selected) {
        this.feedback.processing("Choose an image or PDF file before importing.");
        this.file.focus();
        return;
      }
      this.busy = true;
      this.feedback.processing("Checking file…");
      focusSubmissionStatus(this.form, this.status);
      this.button.setAttribute("aria-busy", "true");
      this.file.disabled = true;
      this.text.disabled = true;
      this.button.disabled = true;
      this.button.textContent = "Processing…";
      let submitted = false;
      try {
        await inspectUpload(selected);
        this.feedback.processing("Uploading file…");
        const response = validateSubmissionResponse(await submitFile(getHass(), selected, {
          text: this.text.value || "",
          sourceId: this.attempt.forDraft(this.text.value || "", selected),
          onUploaded: () => { submitted = true; this.feedback.processing("Extracting events for review…"); },
        }));
        this.feedback.result(response);
        try {
          await onSubmitted(response);
        } catch {
          this.status.textContent += " Refresh the review inbox to see the latest imports.";
        }
      } catch (error) {
        this.feedback.error(error, submitted);
      } finally {
        this.busy = false;
        this.button.setAttribute("aria-busy", "false");
        this.file.disabled = false;
        this.text.disabled = false;
        this.button.disabled = false;
        this.button.textContent = "Import file";
        onSettled();
      }
    });
  }
}
