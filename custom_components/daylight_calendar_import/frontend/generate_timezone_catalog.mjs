/** Reproducibly refresh committed picker metadata from pinned @vvo/tzdb.
 * Run from frontend/: npm install && npm run generate:timezones
 * Verify without rewriting: npm run generate:timezones -- --check
 */
import {readFile, writeFile} from "node:fs/promises";
import {rawTimeZones, timeZonesNames, tzdbVersion} from "@vvo/tzdb";

const version = "v6.206.0";
const expectedIana = "2026f";
if (tzdbVersion !== expectedIana) {
  throw new Error("Pinned tzdb IANA release changed; review the update before regenerating");
}
const groupMap = new Map();
rawTimeZones.forEach((zone, index) =>
  zone.group.forEach(id => groupMap.set(id, index)));
const missing = timeZonesNames.filter(id => !groupMap.has(id));
if (missing.length) throw new Error("Missing IANA names: " + missing.join(", "));
const groups = rawTimeZones.map(z => [z.countryName, z.countryCode,
  z.alternativeName, z.abbreviation, z.dstAbbreviation || "",
  z.mainCities.join(", "), z.group.join(" ")]);
const zones = timeZonesNames.map(id => [id, groupMap.get(id)]);
zones.push(["Etc/UTC", groups.length]);
groups.push(["World", "", "Coordinated Universal Time", "UTC", "", "UTC",
  "UTC GMT Z"]);
zones.sort((left, right) => left[0] < right[0] ? -1 : left[0] > right[0] ? 1 : 0);
const contents = `/** Generated timezone picker catalog from @vvo/tzdb ${version} (IANA ${tzdbVersion}).
 * Source: https://github.com/vvo/tzdb/tree/${version}
 * MIT © CodeAgain SASU. See timezone_catalog.LICENSE.
 * Never use these labels or offsets as authoritative calendar-write evidence.
 * Regenerate by updating the pinned @vvo/tzdb version and source data.
 */
export const TZDB_VERSION = ${JSON.stringify(tzdbVersion)};
export const TZDB_SOURCE_VERSION = ${JSON.stringify(version)};
export const TIMEZONE_GROUPS = ${JSON.stringify(groups)};
export const TIMEZONE_IDS = ${JSON.stringify(zones)};
`;
const target = new URL("./timezone_catalog_data.js", import.meta.url);
if (process.argv.includes("--check")) {
  if (await readFile(target, "utf8") !== contents) {
    throw new Error("Timezone catalog has drifted from pinned @vvo/tzdb metadata");
  }
} else {
  await writeFile(target, contents);
}
