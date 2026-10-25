/** Fetch review summaries through Home Assistant's authenticated service API. */
export async function loadInbox(hass) {
  const result = await hass.callWS({
    type: "call_service",
    domain: "daylight_calendar_import",
    service: "list_pending",
    return_response: true,
  });
  if (!Array.isArray(result?.response?.imports)) {
    throw new Error("The review inbox returned an unexpected response. Try again.");
  }
  return result.response.imports;
}

export async function loadImport(hass, pendingId) {
  const result = await hass.callWS({
    type: "call_service",
    domain: "daylight_calendar_import",
    service: "get_pending",
    service_data: {pending_id: pendingId},
    return_response: true,
  });
  const pending = result?.response?.pending;
  if (!pending || pending.id !== pendingId || !Array.isArray(pending.events)) {
    throw new Error("The import detail returned an unexpected response. Try again.");
  }
  return pending;
}

/** On-demand, advisory calendar intelligence for the loaded review snapshot. */
export async function checkEvent(hass, pendingId, event) {
  const result = await hass.callWS({
    type: "call_service", domain: "daylight_calendar_import",
    service: "check_pending_event",
    service_data: {pending_id: pendingId, event_id: event.id, expected_event: event},
    return_response: true,
  });
  const response = result?.response;
  const validMatch = match => match && typeof match === "object" &&
    ["exact_duplicate", "possible_duplicate", "conflict"].includes(match.kind) &&
    typeof match.calendar_entity === "string" && match.calendar_entity.length > 0 &&
    typeof match.existing_title === "string";
  if (response?.pending_id !== pendingId || response?.event_id !== event.id ||
      !Array.isArray(response.matches) || !response.matches.every(validMatch) ||
      !Array.isArray(response.observed_calendars) ||
      !response.observed_calendars.every(calendar => typeof calendar === "string")) {
    throw new Error("Calendar check returned an unexpected response. Refresh and try again.");
  }
  return response;
}

export async function loadActivity(hass) {
  const result = await hass.callWS({
    type: "call_service", domain: "daylight_calendar_import",
    service: "list_activity", return_response: true,
  });
  if (!Array.isArray(result?.response?.activity)) {
    throw new Error("Recent activity returned an unexpected response. Try again.");
  }
  return result.response.activity;
}

export async function loadActivityDetail(hass, id) {
  const result = await hass.callWS({
    type: "call_service", domain: "daylight_calendar_import",
    service: "get_activity", service_data: {pending_id: id}, return_response: true,
  });
  const activity = result?.response?.activity;
  if (activity?.id !== id || !Array.isArray(activity.transitions)) {
    throw new Error("Activity detail returned an unexpected response. Try again.");
  }
  return activity;
}

export async function saveEvent(hass, pendingId, original, draft, calendarEntity = null) {
  const serviceData = {pending_id: pendingId, event_id: original.id, event: draft,
    expected_event: original};
  if (calendarEntity) serviceData.calendar_entity = calendarEntity;
  const result = await hass.callWS({
    type: "call_service", domain: "daylight_calendar_import",
    service: "edit_pending_event",
    service_data: serviceData,
    return_response: true,
  });
  if (result?.response?.pending_id !== pendingId ||
      result?.response?.event?.id !== original.id) {
    throw new Error("The saved event returned an unexpected response. Refresh before editing again.");
  }
  return result.response.event;
}

export async function decideEvent(hass, pendingId, event, action) {
  if (action !== "approve" && action !== "reject") throw new Error("Invalid review action");
  const result = await hass.callWS({
    type: "call_service", domain: "daylight_calendar_import",
    service: `${action}_pending_event`,
    service_data: {pending_id: pendingId, event_id: event.id, expected_event: event},
    return_response: true,
  });
  const response = result?.response;
  if (response?.pending_id !== pendingId || response?.event_id !== event.id ||
      response?.[action === "approve" ? "approved" : "rejected"] !== true) {
    throw new Error("The review action returned an unexpected response. Refresh before trying again.");
  }
  return response;
}

export async function resolveEvent(hass, pendingId, event, resolution) {
  if (!["created", "not_created", "discard"].includes(resolution)) {
    throw new Error("Invalid recovery choice");
  }
  const result = await hass.callWS({
    type: "call_service", domain: "daylight_calendar_import",
    service: "resolve_pending_event",
    service_data: {pending_id: pendingId, event_id: event.id, resolution, expected_event: event},
    return_response: true,
  });
  if (result?.response?.pending_id !== pendingId ||
      result?.response?.event_id !== event.id ||
      result?.response?.resolution !== resolution) {
    throw new Error("Recovery returned an unexpected response. Refresh before trying again.");
  }
  return result.response;
}

function localeLanguage(locale) {
  return typeof locale === "string" ? locale : locale?.language;
}

function useAmPm(locale) {
  const data = typeof locale === "object" && locale !== null ? locale : {language: locale};
  if (data.time_format === "12") return true;
  if (data.time_format === "24") return false;
  const testLanguage = data.time_format === "system" ? undefined : data.language;
  return new Intl.DateTimeFormat(testLanguage, {hour: "numeric"})
    .resolvedOptions().hour12 === true;
}

function makeFormatter(locale, options) {
  return new Intl.DateTimeFormat(localeLanguage(locale), options);
}

export function formatDateTime(value, locale, timeZone) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return "Unknown time";
  const options = {year: "numeric", month: "short", day: "numeric"};
  if (useAmPm(locale)) {
    Object.assign(options, {hour: "numeric", minute: "2-digit", hour12: true});
  } else {
    Object.assign(options, {hour: "2-digit", minute: "2-digit", hourCycle: "h23"});
  }
  if (timeZone) options.timeZone = timeZone;
  try {
    return makeFormatter(locale, options).format(date);
  } catch {
    delete options.timeZone;
    return makeFormatter(locale, options).format(date);
  }
}

function isoOffsetMinutes(value) {
  if (typeof value !== "string") return null;
  if (/Z$/i.test(value)) return 0;
  const match = /([+-])(\d{2}):(\d{2})$/.exec(value);
  if (!match) return null;
  const hours = Number(match[2]);
  const minutes = Number(match[3]);
  if (hours > 23 || minutes > 59) return null;
  return (match[1] === "+" ? 1 : -1) * (hours * 60 + minutes);
}

function namedZoneOffsetMinutes(date, timeZone) {
  try {
    const zone = new Intl.DateTimeFormat("en-US", {timeZone, timeZoneName: "longOffset"})
      .formatToParts(date).find(part => part.type === "timeZoneName")?.value;
    if (zone === "GMT" || zone === "UTC") return 0;
    const match = /^GMT([+-])(\d{1,2})(?::(\d{2}))?$/.exec(zone || "");
    if (!match) return null;
    const hours = Number(match[2]);
    const minutes = Number(match[3] || "0");
    return (match[1] === "+" ? 1 : -1) * (hours * 60 + minutes);
  } catch {
    return null;
  }
}

function fixedOffsetLabel(offset) {
  if (offset === 0) return "UTC";
  const sign = offset >= 0 ? "+" : "-";
  const absolute = Math.abs(offset);
  return `UTC${sign}${String(Math.floor(absolute / 60)).padStart(2, "0")}:${String(absolute % 60).padStart(2, "0")}`;
}

function fixedOffsetZone(offset) {
  return {timeZone: "UTC", fixedOffset: offset, label: fixedOffsetLabel(offset)};
}

function eventDisplayZones(start, end, event, timeZone) {
  const startOffset = isoOffsetMinutes(event.start);
  const endOffset = isoOffsetMinutes(event.end);
  if (startOffset === null || endOffset === null) return null;
  if (timeZone &&
      namedZoneOffsetMinutes(start, timeZone) === startOffset &&
      namedZoneOffsetMinutes(end, timeZone) === endOffset) {
    const named = {timeZone, fixedOffset: null, label: null};
    return {start: named, end: named};
  }
  return {start: fixedOffsetZone(startOffset), end: fixedOffsetZone(endOffset)};
}

function displayDate(date, zone) {
  return zone.fixedOffset === null ? date : new Date(date.getTime() + zone.fixedOffset * 60000);
}

function formatDateOnly(date, locale, zone) {
  return makeFormatter(locale, {
    timeZone: zone.timeZone, year: "numeric", month: "short", day: "numeric",
  }).format(displayDate(date, zone));
}

function dateKey(date, zone) {
  const parts = new Intl.DateTimeFormat("en-CA", {
    timeZone: zone.timeZone, year: "numeric", month: "2-digit", day: "2-digit",
  }).formatToParts(displayDate(date, zone));
  const get = type => parts.find(part => part.type === type)?.value;
  return `${get("year")}-${get("month")}-${get("day")}`;
}

function timeValue(date, locale, zone) {
  const amPm = useAmPm(locale);
  const shown = displayDate(date, zone);
  let options = amPm ?
    {timeZone: zone.timeZone, hour: "numeric", minute: "2-digit", hour12: true} :
    {timeZone: zone.timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23"};
  let parts = makeFormatter(locale, options).formatToParts(shown);
  const minute = parts.find(part => part.type === "minute")?.value || "";
  if (amPm && minute === "00") {
    options = {timeZone: zone.timeZone, hour: "numeric", hour12: true};
    parts = makeFormatter(locale, options).formatToParts(shown);
  }
  const periodIndex = parts.findIndex(part => part.type === "dayPeriod");
  const hourIndex = parts.findIndex(part => part.type === "hour");
  const period = periodIndex === -1 ? "" : parts[periodIndex].value;
  const full = parts.map(part => part.value).join("").trim();
  const withoutPeriod = parts
    .filter(part => part.type !== "dayPeriod")
    .map(part => part.value).join("").trim();
  return {full, withoutPeriod, period, periodBefore: periodIndex !== -1 && periodIndex < hourIndex};
}

function zoneLabel(date, zone) {
  if (zone.label) return zone.label;
  try {
    return new Intl.DateTimeFormat("en-US", {timeZone: zone.timeZone, timeZoneName: "short"})
      .formatToParts(date).find(part => part.type === "timeZoneName")?.value || zone.timeZone;
  } catch {
    return zone.timeZone;
  }
}

function compactTimeRange(startTime, endTime, sameZone, amPm) {
  if (!amPm) return `${startTime.full}–${endTime.full}`;
  if (!sameZone || !startTime.period || startTime.period !== endTime.period) {
    return `${startTime.full}–${endTime.full}`;
  }
  return startTime.periodBefore ?
    `${startTime.full}–${endTime.withoutPeriod}` :
    `${startTime.withoutPeriod}–${endTime.full}`;
}

export function formatEventRange(event, locale, timeZone) {
  if (event.all_day) {
    const end = new Date(`${event.end}T00:00:00Z`);
    if (Number.isNaN(end.getTime())) return `${event.start} – ${event.end} (exclusive end)`;
    end.setUTCDate(end.getUTCDate() - 1);
    const lastDay = end.toISOString().slice(0, 10);
    return event.start === lastDay ? event.start : `${event.start} – ${lastDay}`;
  }

  const start = new Date(event.start);
  const end = new Date(event.end);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime())) {
    return `${event.start} – ${event.end}`;
  }
  const zones = eventDisplayZones(start, end, event, timeZone);
  if (!zones) return `${event.start} – ${event.end}`;

  const startTime = timeValue(start, locale, zones.start);
  const endTime = timeValue(end, locale, zones.end);
  const startZone = zoneLabel(start, zones.start);
  const endZone = zoneLabel(end, zones.end);
  const sameZone = startZone === endZone;
  const sameDay = dateKey(start, zones.start) === dateKey(end, zones.end);
  const times = compactTimeRange(startTime, endTime, sameZone, useAmPm(locale));

  if (sameDay && sameZone) {
    return `${formatDateOnly(end, locale, zones.end)} · ${times} (${endZone})`;
  }
  if (sameDay) {
    return `${formatDateOnly(end, locale, zones.end)} · ${startTime.full} (${startZone}) – ` +
      `${endTime.full} (${endZone})`;
  }
  const startLabel = `${formatDateOnly(start, locale, zones.start)}, ${startTime.full}` +
    `${sameZone ? "" : ` (${startZone})`}`;
  const endLabel = `${formatDateOnly(end, locale, zones.end)}, ${endTime.full} (${endZone})`;
  return `${startLabel} – ${endLabel}`;
}

export function summarizeImport(item, locale, timeZone) {
  return {
    title: item.source_title || item.title || "Untitled import",
    type: {
      manual_text: "Text", image: "Image", pdf: "PDF", email: "Email",
    }[item.source_kind] || "Source",
    created: formatDateTime(item.created_at, locale, timeZone),
    events: `${item.event_count} ${item.event_count === 1 ? "event" : "events"}`,
    warnings: item.warnings?.length || 0,
    duplicates: item.duplicate_events || 0,
    uncertain: Boolean(item.approval_in_flight),
  };
}
