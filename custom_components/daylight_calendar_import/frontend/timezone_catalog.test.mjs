import assert from "node:assert/strict";
import test from "node:test";
import {execFileSync} from "node:child_process";
import {mkdtempSync, mkdirSync, readFileSync, writeFileSync, rmSync, copyFileSync} from "node:fs";
import {tmpdir} from "node:os";
import {join} from "node:path";
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


test("committed catalog order uses locale-independent identifier ordering", () => {
  const ids = allTimezones().map(zone => zone.id);
  assert.deepEqual(ids, [...ids].sort());
  assert.ok(ids.indexOf("America/Matamoros") < ids.indexOf("America/Mazatlan"));
});


test("catalog generator produces identical bytes under English and Estonian defaults", () => {
  // Exercise the actual generator without installing development dependencies
  // in normal offline frontend tests. The two IDs reverse in Estonian collation.
  const root = mkdtempSync(join(tmpdir(), "daylight-tzdb-"));
  try {
    const pkg = join(root, "node_modules", "@vvo", "tzdb");
    mkdirSync(pkg, {recursive: true});
    writeFileSync(join(pkg, "package.json"), JSON.stringify({type: "module", main: "index.js"}));
    writeFileSync(join(pkg, "index.js"), `
export const tzdbVersion = "2026f";
export const timeZonesNames = ["America/Mazatlan", "America/Matamoros"];
export const rawTimeZones = timeZonesNames.map(id => ({
  group: [id], countryName: "Mexico", countryCode: "MX",
  alternativeName: "Mexico Time", abbreviation: "MT", mainCities: [id],
}));
`);
    const generator = join(root, "generate_timezone_catalog.mjs");
    copyFileSync(new URL("./generate_timezone_catalog.mjs", import.meta.url), generator);
    const target = join(root, "timezone_catalog_data.js");
    const run = (locale, ...args) => execFileSync(process.execPath, [generator, ...args],
      {env: {...process.env, LANG: locale, LC_ALL: locale}});
    run("en_US.UTF-8");
    const english = readFileSync(target, "utf8");
    run("et_EE.UTF-8", "--check");
    run("et_EE.UTF-8");
    assert.equal(readFileSync(target, "utf8"), english);
    assert.match(english, /export const TIMEZONE_IDS = \[\["America\/Matamoros",1\],\["America\/Mazatlan",0\],\["Etc\/UTC",2\]\];/);
  } finally {
    rmSync(root, {recursive: true, force: true});
  }
});
