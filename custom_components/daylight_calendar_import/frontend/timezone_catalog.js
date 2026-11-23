/** Offline timezone identity/search metadata; runtime Intl is only for display hints. */
import {TIMEZONE_GROUPS, TIMEZONE_IDS} from "./timezone_catalog_data.js";

function normal(value) {
  return String(value || "").normalize("NFKD").replace(/[\u0300-\u036f]/g, "")
    .toLocaleLowerCase("en").replace(/[_/]+/g, " ").trim();
}
function cityFromId(id) {
  return id.split("/").at(-1).replaceAll("_", " ");
}
const ZONES = TIMEZONE_IDS.map(([id, groupIndex]) => {
  const [country, countryCode, name, standard, daylight, cities, aliases] =
    TIMEZONE_GROUPS[groupIndex];
  const city = cityFromId(id);
  const zone = {id, city, country, countryCode, name, standard, daylight};
  return Object.freeze({
    ...zone,
    search: normal([id, city, country, countryCode, name, standard, daylight,
      cities, aliases].join(" ")),
  });
});
const ZONE_MAP = new Map(ZONES.map(zone => [zone.id, zone]));

export function allTimezones() {
  return ZONES;
}

export function findTimezone(id) {
  return ZONE_MAP.get(id) || null;
}

/** Every row is a real IANA identifier, never a user-supplied string. */
export function searchTimezones(query) {
  const term = normal(query);
  if (!term) return ZONES;
  const matches = ZONES.filter(zone => zone.search.includes(term));
  // Exact identifiers first; never auto-resolve a short ambiguous abbreviation.
  return matches.sort((a, b) =>
    Number(b.id === query) - Number(a.id === query) ||
    Number(normal(b.city) === term) - Number(normal(a.city) === term) ||
    a.city.localeCompare(b.city) || a.id.localeCompare(b.id));
}

export function timezoneHeading(zone) {
  return zone.city + (zone.country && zone.country !== "World" ?
    ", " + zone.country : "");
}

function formatZoneAtDate(id, wallDate, locale) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(wallDate || "");
  if (!match) return null;
  // Noon UTC is a date-specific catalog hint. Exact local instants and folds
  // always come from the backend preview, never from this display label.
  const instant = new Date(Date.UTC(Number(match[1]), Number(match[2]) - 1,
    Number(match[3]), 12));
  if (Number.isNaN(instant.getTime())) return null;
  try {
    const parts = variant => new Intl.DateTimeFormat(locale || "en-US", {
      timeZone: id, timeZoneName: variant,
    }).formatToParts(instant).find(part => part.type === "timeZoneName")?.value;
    const offset = parts("longOffset") || "";
    const name = parts("long") || "";
    return {offset: offset.replace(/^GMT/, "UTC"), name};
  } catch {
    return null;
  }
}

/** Display-only labels. Never use them to determine timestamps. */
export function timezoneDescription(id, wallDate, locale = "en-US") {
  const zone = findTimezone(id);
  if (!zone) return null;
  const value = formatZoneAtDate(id, wallDate, locale);
  if (!value) return zone.name + " · event-date offset unavailable";
  return value.name + " (" + value.offset + " on " + wallDate.slice(0, 10) + ")";
}

/** Suggested IDs must come from independent backend geographic evidence. */
export function validSuggestedTimezones(ids) {
  if (!Array.isArray(ids)) return [];
  return [...new Set(ids)].map(findTimezone).filter(Boolean);
}
