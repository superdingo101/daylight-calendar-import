/** Upload binary files using Home Assistant's authenticated, temporary upload API.
 * The existing image/PDF services remain authoritative for PDF page/text limits,
 * provider capabilities, media validation and temporary media cleanup.
 */
import {isSubmissionResponse} from "./submission_response.js";

export const MAX_UPLOAD_BYTES = 10 * 1024 * 1024;

const finishedServiceErrors = new Set([
  "unauthorized", "not_allowed", "not_found", "not_supported", "invalid_format",
  "home_assistant_error", "service_validation_error", "unknown_error",
]);

export class UploadError extends Error {
  constructor(code, message) {
    super(message);
    this.name = "UploadError";
    this.code = code;
  }
}

/** Read only the signature; browser filenames and MIME types are advisory. */
export async function inspectUpload(file) {
  if (!file || typeof file.name !== "string" || !file.name ||
      typeof file.slice !== "function" || !Number.isSafeInteger(file.size)) {
    throw new UploadError("invalid_file", "Choose an image or PDF file.");
  }
  if (file.size <= 0) throw new UploadError("empty_attachment", "The selected file is empty.");
  if (file.size > MAX_UPLOAD_BYTES) {
    throw new UploadError("source_too_large", "Choose a file no larger than 10 MiB.");
  }
  const bytes = new Uint8Array(await file.slice(0, 12).arrayBuffer());
  const matches = (signature, offset = 0) =>
    signature.every((value, index) => bytes[index + offset] === value);
  if (matches([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a])) {
    return {kind: "image", mediaType: "image/png"};
  }
  if (matches([0xff, 0xd8, 0xff])) return {kind: "image", mediaType: "image/jpeg"};
  if (matches([0x52, 0x49, 0x46, 0x46]) && matches([0x57, 0x45, 0x42, 0x50], 8)) {
    return {kind: "image", mediaType: "image/webp"};
  }
  if (matches([0x25, 0x50, 0x44, 0x46, 0x2d])) return {kind: "pdf", mediaType: "application/pdf"};
  throw new UploadError("unsupported_media", "Choose a JPEG, PNG, WebP image or PDF.");
}

async function removeUpload(hass, fileId) {
  try {
    // This is deliberately best effort. The service normally already consumed
    // the ID (404); cleanup failures must never disguise the submission outcome.
    await hass.fetchWithAuth("/api/file_upload", {
      method: "DELETE", headers: {"Content-Type": "application/json"},
      body: JSON.stringify({file_id: fileId}),
    });
  } catch {
    // Home Assistant removes unused uploads on shutdown. Retain the original
    // service response/error so the caller can make an informed retry decision.
  }
}

/** Each explicit attempt gets a fresh upload ID; consumed IDs cannot be reused.
 * There are no automatic retries. Keep sourceId stable across retries when the
 * outcome of an earlier service call is unknown. File bytes never go over WS.
 */
export async function submitFile(hass, file, {text = "", sourceId, onUploaded} = {}) {
  const {kind} = await inspectUpload(file);
  if (typeof text !== "string" ||
      (sourceId !== undefined && (typeof sourceId !== "string" || !sourceId.trim()))) {
    throw new UploadError("invalid_submission", "The file submission contains invalid context.");
  }
  const form = new FormData();
  form.append("file", file, file.name);
  const uploaded = await hass.fetchWithAuth("/api/file_upload", {method: "POST", body: form});
  if (uploaded.status === 413) {
    throw new UploadError("source_too_large", "Home Assistant rejected the file size. Choose a file no larger than 10 MiB.");
  }
  if (uploaded.status !== 200) {
    throw new UploadError(uploaded.status === 401 || uploaded.status === 403 ? "unauthorized" : "upload_failed",
      "Home Assistant could not upload the file. Check your connection and permissions.");
  }
  const fileId = (await uploaded.json())?.file_id;
  if (typeof fileId !== "string" || !/^[0-9a-f]{32}$/.test(fileId)) {
    throw new UploadError("invalid_upload_response", "The file upload returned an unexpected response.");
  }
  const serviceData = {file_id: fileId, text: text.trim()};
  if (sourceId !== undefined) serviceData.source_id = sourceId;
  // Avoid user callbacks escaping between upload and cleanup.
  try {
    if (onUploaded) onUploaded();
  } catch (error) {
    void removeUpload(hass, fileId);
    throw error;
  }
  let completed = false;
  try {
    const result = await hass.callWS({
      type: "call_service", domain: "daylight_calendar_import",
      service: kind === "pdf" ? "submit_pdf" : "submit_image",
      service_data: serviceData, return_response: true,
    });
    completed = true;
    if (!isSubmissionResponse(result?.response)) {
      throw new UploadError("invalid_submission_response", "The submission returned an unexpected response. Check recent activity before retrying.");
    }
    return result.response;
  } catch (error) {
    if (finishedServiceErrors.has(error?.code)) completed = true;
    throw error;
  } finally {
    // A disconnect/timeout can leave the service running. Deleting its file
    // here would race the parser, so only clean after an acknowledged outcome.
    // Cleanup is detached so an unresponsive DELETE cannot hold an already
    // acknowledged submission result (or its usable retry controls) hostage.
    if (completed) void removeUpload(hass, fileId);
  }
}
