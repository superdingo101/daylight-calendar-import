/** Native Daylight settings WebSocket client. */

const SETTINGS_GET = "daylight_calendar_import/settings/get";
const SETTINGS_CORE_UPDATE = "daylight_calendar_import/settings/core/update";
const SETTINGS_EMAIL_UPDATE = "daylight_calendar_import/settings/email/update";

function validateSettings(result) {
  if (!result || typeof result.entry_id !== "string" ||
      typeof result.ai_task_entity !== "string" ||
      typeof result.calendar_entity !== "string" ||
      !Array.isArray(result.calendar_entities) ||
      typeof result.email !== "object" || result.email === null) {
    throw new Error("Daylight settings returned an unexpected response. Try again.");
  }
  return result;
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
  for (const key of ["host", "port", "username", "password", "mailbox", "verify_ssl"]) {
    if (settings[key] !== undefined) payload[key] = settings[key];
  }
  return validateSettings(await hass.callWS(payload));
}
