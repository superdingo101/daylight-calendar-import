/** Legacy intake responses omit both lifecycle fields; modern responses pair them. */
export function isSubmissionResponse(response) {
  if (!response || typeof response !== "object" || Array.isArray(response)) return false;
  const pending = response.pending;
  const status = pending ? "pending_review" : response.duplicate ? "duplicate" : "no_events";
  const legacy = !("status" in response) && !("activity_id" in response);
  const skippedSource = pending === null && response.duplicate_source === true &&
    response.duplicate_events === 0 && status === "duplicate";
  const lifecycle = Object.hasOwn(response, "status") && Object.hasOwn(response, "activity_id") &&
    response.status === status &&
    (status !== "pending_review" || response.activity_id === pending.id) &&
    ((typeof response.activity_id === "string" && response.activity_id.trim().length > 0) ||
      (response.activity_id === null && skippedSource));
  return typeof response.duplicate === "boolean" &&
    typeof response.duplicate_source === "boolean" &&
    Number.isInteger(response.duplicate_events) && response.duplicate_events >= 0 &&
    response.duplicate === (response.duplicate_source || response.duplicate_events > 0) &&
    (!response.duplicate_source || (pending === null && response.duplicate_events === 0)) &&
    Array.isArray(response.warnings) && response.warnings.every(warning => typeof warning === "string") &&
    (pending === null || (typeof pending === "object" && pending !== null && !Array.isArray(pending) &&
      typeof pending.id === "string" && pending.id.trim().length > 0 &&
      Array.isArray(pending.events) && pending.events.length > 0)) &&
    (legacy || lifecycle);
}
