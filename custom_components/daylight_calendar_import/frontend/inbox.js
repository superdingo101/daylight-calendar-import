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
