/** Pure event-editor date/time conversion and validation helpers. */

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
    const zone = new Intl.DateTimeFormat("en-US", {
      timeZone,
      timeZoneName: "longOffset",
    }).formatToParts(date).find(part => part.type === "timeZoneName")?.value;
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

function formatOffset(offset) {
  if (offset === 0) return "Z";
  const sign = offset >= 0 ? "+" : "-";
  const absolute = Math.abs(offset);
  const hours = String(Math.floor(absolute / 60)).padStart(2, "0");
  const minutes = String(absolute % 60).padStart(2, "0");
  return sign + hours + ":" + minutes;
}

function formatDateTimeParts(date, timeZone) {
  try {
    const parts = new Intl.DateTimeFormat("en-CA", {
      timeZone,
      year: "numeric",
      month: "2-digit",
      day: "2-digit",
      hour: "2-digit",
      minute: "2-digit",
      hourCycle: "h23",
    }).formatToParts(date);
    const value = type => parts.find(part => part.type === type)?.value;
    const year = value("year");
    const month = value("month");
    const day = value("day");
    const hour = value("hour");
    const minute = value("minute");
    return year && month && day && hour && minute
      ? year + "-" + month + "-" + day + "T" + hour + ":" + minute
      : "";
  } catch {
    return "";
  }
}

function fixedOffsetDateTimeParts(date, offset) {
  const shown = new Date(date.getTime() + offset * 60000);
  if (Number.isNaN(shown.getTime())) return "";
  const year = shown.getUTCFullYear();
  const month = String(shown.getUTCMonth() + 1).padStart(2, "0");
  const day = String(shown.getUTCDate()).padStart(2, "0");
  const hour = String(shown.getUTCHours()).padStart(2, "0");
  const minute = String(shown.getUTCMinutes()).padStart(2, "0");
  return year + "-" + month + "-" + day + "T" + hour + ":" + minute;
}

function wallUtcMilliseconds(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})$/.exec(value || "");
  if (!match) return null;
  const [year, month, day, hour, minute] = match.slice(1).map(Number);
  const millis = Date.UTC(year, month - 1, day, hour, minute, 0);
  const check = new Date(millis);
  if (check.getUTCFullYear() !== year ||
      check.getUTCMonth() !== month - 1 ||
      check.getUTCDate() !== day ||
      check.getUTCHours() !== hour ||
      check.getUTCMinutes() !== minute) return null;
  return millis;
}

function validWallOffsets(value, timeZone) {
  const wallUtc = wallUtcMilliseconds(value);
  if (wallUtc === null || !timeZone) return [];
  const offsets = new Set();
  for (const deltaHours of [-48, -24, -12, 0, 12, 24, 48]) {
    const offset = namedZoneOffsetMinutes(
      new Date(wallUtc + deltaHours * 60 * 60 * 1000),
      timeZone,
    );
    if (offset !== null) offsets.add(offset);
  }
  return [...offsets].filter(offset => {
    const instant = new Date(wallUtc - offset * 60000);
    return namedZoneOffsetMinutes(instant, timeZone) === offset &&
      formatDateTimeParts(instant, timeZone) === value;
  });
}

function wallZoneOffsetMinutes(value, timeZone, preferredOffset = null) {
  const offsets = validWallOffsets(value, timeZone);
  if (!offsets.length) return null;
  if (preferredOffset !== null && offsets.includes(preferredOffset)) {
    return preferredOffset;
  }
  return Math.max(...offsets);
}

function validIsoDate(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
  if (!match) return false;
  const [year, month, day] = match.slice(1).map(Number);
  const date = new Date(Date.UTC(year, month - 1, day));
  return date.getUTCFullYear() === year &&
    date.getUTCMonth() === month - 1 &&
    date.getUTCDate() === day;
}

export function editDateTimeValue(value, timeZone) {
  const date = new Date(value);
  const offset = isoOffsetMinutes(value);
  if (!Number.isNaN(date.getTime()) && timeZone && offset !== null &&
      namedZoneOffsetMinutes(date, timeZone) === offset) {
    const named = formatDateTimeParts(date, timeZone);
    if (named) return named;
  }
  const wall = /^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2})/.exec(value || "");
  if (wall) return wall[1];
  if (!Number.isNaN(date.getTime()) && timeZone) {
    return formatDateTimeParts(date, timeZone);
  }
  return "";
}

export function editDateTimeIso(value, original, timeZone, instantHint = null) {
  if (wallUtcMilliseconds(value) === null) return null;
  if (instantHint && editDateTimeValue(instantHint, timeZone) === value) {
    const hintedDate = new Date(instantHint);
    if (!Number.isNaN(hintedDate.getTime()) &&
        instantEditDateTimeIso(hintedDate, original, timeZone) === instantHint) {
      return instantHint;
    }
  }
  if (value === editDateTimeValue(original, timeZone)) return original;

  const originalDate = new Date(original);
  const originalOffset = isoOffsetMinutes(original);
  const originalUsesNamedZone = Boolean(
    timeZone && originalOffset !== null && !Number.isNaN(originalDate.getTime()) &&
    namedZoneOffsetMinutes(originalDate, timeZone) === originalOffset
  );

  if (originalUsesNamedZone || originalOffset === null) {
    const preferredOffset = originalUsesNamedZone ? originalOffset : null;
    const zoneOffset = wallZoneOffsetMinutes(value, timeZone, preferredOffset);
    if (zoneOffset !== null) return value + ":00" + formatOffset(zoneOffset);
    if (timeZone) return null;
  }

  if (originalOffset !== null) return value + ":00" + formatOffset(originalOffset);

  const local = new Date(value + ":00");
  if (Number.isNaN(local.getTime())) return null;
  return value + ":00" + formatOffset(-local.getTimezoneOffset());
}

export function instantEditDateTimeIso(date, original, timeZone) {
  if (!(date instanceof Date) || Number.isNaN(date.getTime())) return null;
  const originalDate = new Date(original);
  const originalOffset = isoOffsetMinutes(original);
  const originalUsesNamedZone = Boolean(
    timeZone && originalOffset !== null && !Number.isNaN(originalDate.getTime()) &&
    namedZoneOffsetMinutes(originalDate, timeZone) === originalOffset
  );

  if (originalUsesNamedZone) {
    const wall = formatDateTimeParts(date, timeZone);
    const offset = namedZoneOffsetMinutes(date, timeZone);
    return wall && offset !== null ? wall + ":00" + formatOffset(offset) : null;
  }

  if (originalOffset !== null) {
    const wall = fixedOffsetDateTimeParts(date, originalOffset);
    return wall ? wall + ":00" + formatOffset(originalOffset) : null;
  }

  if (timeZone) {
    const wall = formatDateTimeParts(date, timeZone);
    const offset = namedZoneOffsetMinutes(date, timeZone);
    return wall && offset !== null ? wall + ":00" + formatOffset(offset) : null;
  }

  const year = date.getFullYear();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  const hour = String(date.getHours()).padStart(2, "0");
  const minute = String(date.getMinutes()).padStart(2, "0");
  const wall = year + "-" + month + "-" + day + "T" + hour + ":" + minute;
  return wall + ":00" + formatOffset(-date.getTimezoneOffset());
}

export function instantEditDateTimeValue(date, original, timeZone) {
  const iso = instantEditDateTimeIso(date, original, timeZone);
  return iso ? editDateTimeValue(iso, timeZone) : "";
}

function shiftIsoDate(value, days) {
  if (!validIsoDate(value)) return "";
  const [year, month, day] = value.split("-").map(Number);
  return new Date(Date.UTC(year, month - 1, day + days)).toISOString().slice(0, 10);
}

export function visibleAllDayEnd(exclusiveEnd) {
  return shiftIsoDate(exclusiveEnd, -1);
}

export function exclusiveAllDayEnd(visibleEnd) {
  return shiftIsoDate(visibleEnd, 1);
}

export function dateOnlyFromTimed(value, timeZone) {
  const local = editDateTimeValue(value, timeZone);
  return local ? local.slice(0, 10) : "";
}

export function timedEditToAllDayRange(startDateTime, endDateTime) {
  if (wallUtcMilliseconds(startDateTime) === null ||
      wallUtcMilliseconds(endDateTime) === null) return null;
  const startDate = startDateTime.slice(0, 10);
  const endDate = endDateTime.slice(0, 10);
  const endTime = endDateTime.slice(11);
  const visibleEnd = endTime === "00:00" && endDate > startDate ?
    shiftIsoDate(endDate, -1) : endDate;
  if (!visibleEnd || visibleEnd < startDate) return null;
  return {startDate, endDate: visibleEnd};
}

export function allDayEditToTimedRange(
  startDate,
  endDate,
  previousStartDateTime = "",
  previousEndDateTime = "",
  preserveTimes = false,
) {
  if (!validIsoDate(startDate) || !validIsoDate(endDate) || endDate < startDate) {
    return null;
  }

  const previousStartValid = wallUtcMilliseconds(previousStartDateTime) !== null;
  const previousEndValid = wallUtcMilliseconds(previousEndDateTime) !== null;
  if (preserveTimes && previousStartValid && previousEndValid) {
    const startTime = previousStartDateTime.slice(11);
    const endTime = previousEndDateTime.slice(11);
    const previousStartDate = previousStartDateTime.slice(0, 10);
    const previousEndDate = previousEndDateTime.slice(0, 10);
    const endAtExclusiveMidnight =
      endTime === "00:00" && previousEndDate > previousStartDate;
    const timedEndDate = endAtExclusiveMidnight ?
      exclusiveAllDayEnd(endDate) : endDate;
    if (!timedEndDate) return null;
    return {
      startDateTime: startDate + "T" + startTime,
      endDateTime: timedEndDate + "T" + endTime,
    };
  }

  const exclusiveEnd = exclusiveAllDayEnd(endDate);
  if (!exclusiveEnd) return null;
  return {
    startDateTime: startDate + "T00:00",
    endDateTime: exclusiveEnd + "T00:00",
  };
}

export function normalizeEventTemporalEdit({
  allDay,
  startDate,
  endDate,
  startDateTime,
  endDateTime,
  originalStart,
  originalEnd,
  timeZone,
  startInstantHint = null,
  endInstantHint = null,
}) {
  if (allDay) {
    if (!validIsoDate(startDate) || !validIsoDate(endDate)) {
      return {valid: false, error: "Start and end dates are required."};
    }
    if (endDate < startDate) {
      return {valid: false, error: "End date cannot be before start date."};
    }
    return {
      valid: true,
      start: startDate,
      end: exclusiveAllDayEnd(endDate),
    };
  }

  if (!startDateTime || !endDateTime) {
    return {valid: false, error: "Start and end times are required."};
  }
  const start = editDateTimeIso(
    startDateTime, originalStart, timeZone, startInstantHint
  );
  const end = editDateTimeIso(
    endDateTime, originalEnd, timeZone, endInstantHint
  );
  if (!start || !end) {
    return {
      valid: false,
      error: "The selected local time does not exist in the Home Assistant time zone.",
    };
  }
  if (new Date(end).getTime() <= new Date(start).getTime()) {
    return {valid: false, error: "End time must be after start time."};
  }
  return {valid: true, start, end};
}
