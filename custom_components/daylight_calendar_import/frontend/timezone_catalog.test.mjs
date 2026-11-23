import assert from "node:assert/strict";
import test from "node:test";
import {allTimezones, findTimezone, searchTimezones, timezoneDescription,
  timezoneHeading, validSuggestedTimezones} from "./timezone_catalog.js";
import {TZDB_SOURCE_VERSION, TZDB_VERSION} from "./timezone_catalog_data.js";

test("pinned offline catalog includes canonical world zones and keeps identities distinct", () => {
  assert.equal(TZDB_SOURCE_VERSION, "v6.206.0");
  assert.equal(TZDB_VERSION, "2026f");
  const zones = allTimezones();
  assert.ok(zones.length >= 400);
  assert.equal(new Set(zones.map(zone => zone.id)).size, zones.length);
  assert.equal(findTimezone("America/New_York").city, "New York");
  assert.equal(findTimezone("America/Detroit").id, "America/Detroit");
  assert.equal(findTimezone("Australia/Adelaide").country, "Australia");
  assert.equal(findTimezone("Etc/UTC").id, "Etc/UTC");
  assert.equal(findTimezone("not-a-real-zone"), null);
});

test("search understands city, country, alias, IANA ID and ambiguous abbreviation", () => {
  for (const q of ["New York", "Eastern", "US/Eastern", "America/New_York"]) {
    assert.ok(searchTimezones(q).some(z => z.id === "America/New_York"), q);
  }
  assert.equal(searchTimezones("America/New_York")[0].id, "America/New_York");
  assert.ok(searchTimezones("united kingdom").some(z => z.id === "Europe/London"));
  assert.ok(searchTimezones("Kathmandu").some(z => z.id === "Asia/Kathmandu"));
  const cst = searchTimezones("CST");
  assert.ok(cst.length > 1, "CST must not silently identify one location");
  assert.ok(cst.some(z => z.id === "America/Chicago"));
  assert.ok(cst.some(z => z.id === "Asia/Shanghai"));
  assert.deepEqual(searchTimezones("nonexistent city 3810382"), []);
  assert.equal(searchTimezones("").length, allTimezones().length);
});

test("event-date offset and daylight label come from Intl, not catalog static offsets", () => {
  assert.match(timezoneDescription("America/New_York", "2028-07-10T16:30:00"),
    /Eastern Daylight Time \(UTC-04:00 on 2028-07-10\)/);
  assert.match(timezoneDescription("America/New_York", "2028-01-10T16:30:00"),
    /Eastern Standard Time \(UTC-05:00 on 2028-01-10\)/);
  assert.match(timezoneDescription("Asia/Kathmandu", "2028-07-10T12:00:00"),
    /UTC\+05:45/);
  assert.match(timezoneDescription("Australia/Adelaide", "2028-12-10T12:00:00"),
    /UTC\+10:30/);
  assert.equal(timezoneDescription("not-a-zone", "2028-07-10"), null);
  assert.equal(timezoneHeading(findTimezone("America/Los_Angeles")),
    "Los Angeles, United States");
});

test("invalid dates and unsupported browser formatting cannot fabricate event offsets", () => {
  assert.match(timezoneDescription("Europe/London", "not-a-date"),
    /offset unavailable/);
  const original = Intl.DateTimeFormat;
  try {
    Intl.DateTimeFormat = () => { throw new Error("no Intl date formatter"); };
    assert.match(timezoneDescription("America/New_York", "2028-07-10"),
      /offset unavailable/);
  } finally {
    Intl.DateTimeFormat = original;
  }
});

test("only valid, explicitly identified IANA suggestions are rendered", () => {
  assert.deepEqual(validSuggestedTimezones([
    "America/New_York", "America/New_York", "not-a-zone", "Asia/Kathmandu",
  ]).map(z => z.id), ["America/New_York", "Asia/Kathmandu"]);
  assert.deepEqual(validSuggestedTimezones(null), []);
});
