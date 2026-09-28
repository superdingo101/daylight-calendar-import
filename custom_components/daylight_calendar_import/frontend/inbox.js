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

export function summarizeImport(item, locale) {
  const date = new Date(item.created_at);
  return {
    title: item.source_title || item.title || "Untitled import",
    type: {
      manual_text: "Text", image: "Image", pdf: "PDF", email: "Email",
    }[item.source_kind] || "Source",
    created: Number.isNaN(date.getTime()) ? "Unknown time" :
      new Intl.DateTimeFormat(locale, {dateStyle: "medium", timeStyle: "short"}).format(date),
    events: `${item.event_count} ${item.event_count === 1 ? "event" : "events"}`,
    warnings: item.warnings?.length || 0,
    duplicates: item.duplicate_events || 0,
    uncertain: Boolean(item.approval_in_flight),
  };
}
