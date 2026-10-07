import assert from "node:assert/strict";
import test from "node:test";

import {
  dateOnlyFromTimed,
  editDateTimeIso,
  editDateTimeValue,
  exclusiveAllDayEnd,
  instantEditDateTimeIso,
  instantEditDateTimeValue,
  normalizeEventTemporalEdit,
  visibleAllDayEnd,
} from "./event_datetime.js";

test("timed values preserve unchanged timestamps and follow the HA zone after edits", () => {
  const zone = "America/Los_Angeles";
  const original = "2026-03-08T01:30:00-08:00";
  assert.equal(editDateTimeValue(original, zone), "2026-03-08T01:30");
  assert.equal(editDateTimeIso("2026-03-08T01:30", original, zone), original);
  assert.equal(
    editDateTimeIso("2026-03-08T04:30", original, zone),
    "2026-03-08T04:30:00-07:00",
  );
});

test("nonexistent spring-forward wall times are rejected instead of silently shifted", () => {
  assert.equal(
    editDateTimeIso(
      "2026-03-08T02:30",
      "2026-03-08T01:30:00-08:00",
      "America/Los_Angeles",
    ),
    null,
  );
});

test("ambiguous fall-back wall times prefer the original named-zone offset", () => {
  const zone = "America/Los_Angeles";
  assert.equal(
    editDateTimeIso("2026-11-01T01:30", "2026-11-01T00:30:00-07:00", zone),
    "2026-11-01T01:30:00-07:00",
  );
  assert.equal(
    editDateTimeIso("2026-11-01T01:30", "2026-11-01T02:30:00-08:00", zone),
    "2026-11-01T01:30:00-08:00",
  );
});

test("generated fold hints preserve the exact duration-synced occurrence", () => {
  const zone = "America/Los_Angeles";
  const originalEnd = "2026-11-01T01:30:00-08:00";
  const firstFoldInstant = new Date("2026-11-01T08:30:00Z");
  const hint = instantEditDateTimeIso(firstFoldInstant, originalEnd, zone);

  assert.equal(hint, "2026-11-01T01:30:00-07:00");
  assert.equal(
    instantEditDateTimeValue(firstFoldInstant, originalEnd, zone),
    "2026-11-01T01:30",
  );
  assert.equal(
    editDateTimeIso("2026-11-01T01:30", originalEnd, zone, hint),
    "2026-11-01T01:30:00-07:00",
  );
  assert.equal(
    editDateTimeIso("2026-11-01T01:45", originalEnd, zone, hint),
    "2026-11-01T01:45:00-08:00",
  );
});

test("fixed-offset events keep their fixed offset when it does not match the HA zone", () => {
  assert.equal(
    editDateTimeIso(
      "2026-10-01T11:15",
      "2026-10-01T10:00:00-04:00",
      "America/Los_Angeles",
    ),
    "2026-10-01T11:15:00-04:00",
  );
});

test("duration shifts render in the event's time model", () => {
  assert.equal(
    instantEditDateTimeValue(
      new Date("2026-03-08T12:30:00Z"),
      "2026-03-08T03:30:00-07:00",
      "America/Los_Angeles",
    ),
    "2026-03-08T05:30",
  );
});

test("all-day editor shows inclusive ends while storing exclusive ends", () => {
  assert.equal(visibleAllDayEnd("2026-12-13"), "2026-12-12");
  assert.equal(exclusiveAllDayEnd("2026-12-12"), "2026-12-13");
  assert.equal(visibleAllDayEnd("2027-01-01"), "2026-12-31");
  assert.equal(exclusiveAllDayEnd("2026-12-31"), "2027-01-01");
});

test("timed values provide a date for timed-to-all-day conversion", () => {
  assert.equal(
    dateOnlyFromTimed("2026-12-05T23:30:00-08:00", "America/Los_Angeles"),
    "2026-12-05",
  );
});

test("temporal normalization follows card-style range validation", () => {
  assert.deepEqual(
    normalizeEventTemporalEdit({
      allDay: true,
      startDate: "2026-12-24",
      endDate: "2026-12-26",
    }),
    {valid: true, start: "2026-12-24", end: "2026-12-27"},
  );
  assert.deepEqual(
    normalizeEventTemporalEdit({
      allDay: true,
      startDate: "2026-12-26",
      endDate: "2026-12-24",
    }),
    {valid: false, error: "End date cannot be before start date."},
  );
  assert.deepEqual(
    normalizeEventTemporalEdit({
      allDay: false,
      startDateTime: "2026-10-01T11:00",
      endDateTime: "2026-10-01T10:00",
      originalStart: "2026-10-01T10:00:00-07:00",
      originalEnd: "2026-10-01T11:00:00-07:00",
      timeZone: "America/Los_Angeles",
    }),
    {valid: false, error: "End time must be after start time."},
  );
});

test("temporal normalization honors an exact generated fold hint", () => {
  assert.deepEqual(
    normalizeEventTemporalEdit({
      allDay: false,
      startDateTime: "2026-11-01T00:30",
      endDateTime: "2026-11-01T01:30",
      originalStart: "2026-11-01T01:30:00-07:00",
      originalEnd: "2026-11-01T01:30:00-08:00",
      timeZone: "America/Los_Angeles",
      endInstantHint: "2026-11-01T01:30:00-07:00",
    }),
    {
      valid: true,
      start: "2026-11-01T00:30:00-07:00",
      end: "2026-11-01T01:30:00-07:00",
    },
  );
});

test("temporal normalization rejects nonexistent active times", () => {
  assert.deepEqual(
    normalizeEventTemporalEdit({
      allDay: false,
      startDateTime: "2026-03-08T02:30",
      endDateTime: "2026-03-08T04:30",
      originalStart: "2026-03-08T01:30:00-08:00",
      originalEnd: "2026-03-08T03:30:00-07:00",
      timeZone: "America/Los_Angeles",
    }),
    {
      valid: false,
      error: "The selected local time does not exist in the Home Assistant time zone.",
    },
  );
});
