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

export async function saveEvent(hass, pendingId, original, draft) {
  const result = await hass.callWS({
    type: "call_service", domain: "daylight_calendar_import",
    service: "edit_pending_event",
    service_data: {pending_id: pendingId, event_id: original.id, event: draft,
      expected_event: original},
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
  return new Date("January 1, 2023 22:00:00").toLocaleString(testLanguage).includes("10");
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

function eventDisplayZone(start, end, event, timeZone) {
  const startOffset = isoOffsetMinutes(event.start);
  const endOffset = isoOffsetMinutes(event.end);
  if (endOffset === null) return null;
  if (timeZone && startOffset !== null &&
      namedZoneOffsetMinutes(start, timeZone) === startOffset &&
      namedZoneOffsetMinutes(end, timeZone) === endOffset) {
    return {timeZone, fixedOffset: null, label: null};
  }
  return {timeZone: "UTC", fixedOffset: endOffset, label: fixedOffsetLabel(endOffset)};
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
  const options = amPm ?
    {timeZone: zone.timeZone, hour: "numeric", minute: "2-digit", hour12: true} :
    {timeZone: zone.timeZone, hour: "2-digit", minute: "2-digit", hourCycle: "h23"};
  const parts = makeFormatter(locale, options).formatToParts(displayDate(date, zone));
  const hour = parts.find(part => part.type === "hour")?.value || "";
  const minute = parts.find(part => part.type === "minute")?.value || "";
  const period = parts.find(part => part.type === "dayPeriod")?.value || "";
  return {clock: amPm && minute === "00" ? hour : `${hour}:${minute}`, period};
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

function clockWithPeriod(value) {
  return value.period ? `${value.clock} ${value.period}` : value.clock;
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
  const zone = eventDisplayZone(start, end, event, timeZone);
  if (!zone) return `${event.start} – ${event.end}`;

  const startTime = timeValue(start, locale, zone);
  const endTime = timeValue(end, locale, zone);
  const startZone = zoneLabel(start, zone);
  const endZone = zoneLabel(end, zone);
  const sameZone = startZone === endZone;
  const sameDay = dateKey(start, zone) === dateKey(end, zone);
  let times;
  if (useAmPm(locale)) {
    const compactStart = sameZone && startTime.period === endTime.period ?
      startTime.clock : clockWithPeriod(startTime);
    times = `${compactStart}–${clockWithPeriod(endTime)}`;
  } else {
    times = `${startTime.clock}–${endTime.clock}`;
  }

  if (sameDay && sameZone) {
    return `${formatDateOnly(end, locale, zone)} · ${times} (${endZone})`;
  }
  if (sameDay) {
    return `${formatDateOnly(end, locale, zone)} · ${clockWithPeriod(startTime)} (${startZone}) – ` +
      `${clockWithPeriod(endTime)} (${endZone})`;
  }
  const startLabel = `${formatDateOnly(start, locale, zone)}, ${clockWithPeriod(startTime)}` +
    `${sameZone ? "" : ` (${startZone})`}`;
  const endLabel = `${formatDateOnly(end, locale, zone)}, ${clockWithPeriod(endTime)} (${endZone})`;
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
