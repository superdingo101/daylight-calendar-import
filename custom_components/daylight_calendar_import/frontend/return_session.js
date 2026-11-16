/** Browser-local, same-tab return context shared with Daylight Calendar Card. */
const KEY_PREFIX = "daylight:return:v1:";
const MAX_AGE_MS = 24 * 60 * 60 * 1000;
const ID_PATTERN = /^[A-Za-z0-9_-]{22,128}$/;

/** Read and validate a session without treating its identifier as authorization. */
export function readReturnSession(storage, id, origin, now = Date.now()) {
  if (!ID_PATTERN.test(id || "")) return null;
  try {
    const value = JSON.parse(storage.getItem(KEY_PREFIX + id));
    if (!value || value.version !== 1 || value.id !== id || value.result != null ||
        !Number.isFinite(value.created_at) || !Number.isFinite(value.expires_at) ||
        value.created_at > now || value.expires_at <= now ||
        value.expires_at <= value.created_at || value.expires_at - value.created_at > MAX_AGE_MS ||
        typeof value.return_path !== "string" || !value.return_path.startsWith("/") ||
        value.return_path.startsWith("//") || /[\\\x00-\x1f\x7f]/.test(value.return_path)) return null;
    // Reject ambiguous encodings before parsing or normalizing the path.
    const decoded = decodeURIComponent(value.return_path);
    if (/[\\\x00-\x1f\x7f]/.test(decoded) || decoded.startsWith("//")) return null;
    const url = new URL(value.return_path, origin);
    if (url.origin !== origin || url.username || url.password || url.pathname.startsWith("//") ||
        decodeURIComponent(url.pathname).startsWith("//") ||
        /^\/(?:daylight-calendar-import|auth|redirect)(?:\/|$)/i.test(decodeURIComponent(url.pathname))) return null;
    return {id, key: KEY_PREFIX + id, value, returnPath: url.pathname + url.search + url.hash};
  } catch {
    return null;
  }
}

/** Save a result before navigation; storage errors must not cause navigation. */
export function saveReturnResult(storage, session, status, now = Date.now()) {
  if (!["completed", "cancelled"].includes(status)) return false;
  try {
    storage.setItem(session.key, JSON.stringify({...session.value, result: {
      status, calendar_may_have_changed: true, completed_at: now,
    }}));
    return true;
  } catch {
    return false;
  }
}
