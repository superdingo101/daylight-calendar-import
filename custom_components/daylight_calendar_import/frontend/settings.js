/** Native Daylight settings WebSocket client. */

const SETTINGS_GET = "daylight_calendar_import/settings/get";
const SETTINGS_CORE_UPDATE = "daylight_calendar_import/settings/core/update";
const SETTINGS_EMAIL_UPDATE = "daylight_calendar_import/settings/email/update";
const SETTINGS_CALENDAR_INTELLIGENCE_UPDATE = "daylight_calendar_import/settings/calendar_intelligence/update";
const SETTINGS_NOTIFICATIONS_UPDATE = "daylight_calendar_import/settings/notifications/update";

const NOTIFICATION_CLASSES = new Set([
  "review_ready", "calendar_created", "calendar_create_failed",
  "calendar_write_uncertain", "conflict_detected", "parse_failed",
]);

function validNotificationPolicy(policy) {
  return policy && typeof policy === "object" && !Array.isArray(policy) &&
    typeof policy.enabled === "boolean" &&
    (policy.target === null || typeof policy.target === "string" &&
      /^notify\\.[a-z0-9_]+$/.test(policy.target) && policy.target.length <= 128) &&
    Array.isArray(policy.classes) &&
    policy.classes.every(value => NOTIFICATION_CLASSES.has(value)) &&
    new Set(policy.classes).size === policy.classes.length;
}

function validateSettings(result) {
  if (!result || typeof result.entry_id !== "string" ||
      typeof result.ai_task_entity !== "string" ||
      typeof result.calendar_entity !== "string" ||
      !Array.isArray(result.calendar_entities) ||
      typeof result.email !== "object" || result.email === null ||
      !validNotificationPolicy(result.notifications)) {
    throw new Error("Daylight settings returned an unexpected response. Try again.");
  }
  return result;
}

export function isSettingsErrorCode(error, code) {
  return typeof error === "object" && error !== null &&
    typeof error.code === "string" && error.code === code;
}

export function settingsErrorMessage(error, fallback) {
  return typeof error === "object" && error !== null &&
    typeof error.message === "string" ? error.message :
    error instanceof Error ? error.message :
      typeof error === "string" && error ? error : fallback;
}

export async function loadSettings(hass) {
  return validateSettings(await hass.callWS({type: SETTINGS_GET}));
}

export async function saveCoreSettings(hass, settings) {
  const payload = {
    type: SETTINGS_CORE_UPDATE,
    entry_id: settings.entry_id,
  };
  for (const key of ["ai_task_entity", "calendar_entity", "calendar_entities"]) {
    if (settings[key] !== undefined) payload[key] = settings[key];
  }
  return validateSettings(await hass.callWS(payload));
}

export async function saveEmailSettings(hass, settings) {
  const payload = {
    type: SETTINGS_EMAIL_UPDATE,
    entry_id: settings.entry_id,
    enabled: settings.enabled,
  };
  for (const key of ["host", "port", "username", "password", "mailbox", "verify_ssl", "sender_allowlist"]) {
    if (settings[key] !== undefined) payload[key] = settings[key];
  }
  return validateSettings(await hass.callWS(payload));
}

export async function saveNotificationSettings(hass, settings) {
  return validateSettings(await hass.callWS({
    type: SETTINGS_NOTIFICATIONS_UPDATE,
    entry_id: settings.entry_id,
    notifications: settings.notifications,
    expected_notifications: settings.expected_notifications,
  }));
}

export async function saveCalendarIntelligenceSettings(hass, settings) {
  const payload = {
    type: SETTINGS_CALENDAR_INTELLIGENCE_UPDATE,
    entry_id: settings.entry_id,
  };
  for (const key of ["calendar_aliases", "conflict_calendar_entities"]) {
    if (settings[key] !== undefined) payload[key] = settings[key];
  }
  return validateSettings(await hass.callWS(payload));
}
