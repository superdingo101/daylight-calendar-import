/** Mobile-first timezone picker: native dialog and scrollable select, no UI framework. */
import {allTimezones, findTimezone, searchTimezones, timezoneDescription,
  timezoneHeading, validSuggestedTimezones} from "./timezone_catalog.js";

let nextDescriptionId = 0;

function node(tag, text) {
  const item = document.createElement(tag);
  if (text !== undefined) item.textContent = text;
  return item;
}

/** Returns one independent, explicitly confirmed endpoint selection. */
export function createTimezonePicker({label, wallDate, locale, value = "",
  onChange = () => {}, suggestions = []}) {
  const root = node("div");
  root.className = "timezone-field";
  const fieldLabel = node("span", label);
  fieldLabel.className = "timezone-field-label";
  const trigger = node("button", "Choose timezone");
  trigger.type = "button";
  trigger.className = "timezone-field-trigger";
  trigger.setAttribute("aria-label", label + ": choose timezone");
  const description = node("span", "No timezone selected");
  description.className = "timezone-field-description";
  description.id = "daylight-timezone-description-" + ++nextDescriptionId;
  trigger.setAttribute("aria-describedby", description.id);
  const dialog = node("dialog");
  dialog.className = "timezone-picker-dialog";
  dialog.setAttribute("aria-label", "Select " + label.toLowerCase());
  const title = node("h3", "Select " + label.toLowerCase());
  const guidance = node("p", "Choose a city or search by country, timezone name, or IANA identifier.");
  const suggestionSection = node("div");
  suggestionSection.className = "timezone-picker-suggestions";
  const searchLabel = node("label", "Search timezones");
  const search = node("input");
  search.type = "search";
  search.placeholder = "City, country, region, or timezone";
  search.setAttribute("aria-label", "Search timezones for " + label.toLowerCase());
  searchLabel.append(search);
  const resultCount = node("p");
  resultCount.className = "timezone-picker-count";
  resultCount.setAttribute("role", "status");
  const selectLabel = node("label", "Available timezones");
  const choices = node("select");
  choices.className = "timezone-picker-list";
  choices.size = 9;
  choices.setAttribute("aria-label", "Available timezones for " + label.toLowerCase());
  selectLabel.append(choices);
  const detail = node("p");
  detail.className = "timezone-picker-detail";
  detail.setAttribute("aria-live", "polite");
  const actions = node("div");
  actions.className = "timezone-picker-actions";
  const cancel = node("button", "Cancel");
  cancel.type = "button";
  const choose = node("button", "Use selected timezone");
  choose.type = "button";
  choose.className = "event-primary";
  choose.disabled = true;
  actions.append(cancel, choose);
  dialog.append(title, guidance, suggestionSection, searchLabel, resultCount,
    selectLabel, detail, actions);
  root.append(fieldLabel, trigger, description, dialog);

  let confirmed = findTimezone(value)?.id || "";
  let proposed = "";
  let proposedSuggestions = validSuggestedTimezones(suggestions);

  function refreshValue() {
    const selected = findTimezone(confirmed);
    trigger.textContent = selected ? timezoneHeading(selected) : "Choose timezone";
    trigger.setAttribute("aria-label", label + ": " +
      (selected ? trigger.textContent : "choose timezone"));
    description.textContent = selected ?
      timezoneDescription(confirmed, wallDate, locale) : "No timezone selected";
  }
  function close() {
    if (typeof dialog.close === "function" && dialog.open) dialog.close();
    else dialog.removeAttribute("open");
    trigger.focus();
  }
  function commit(id) {
    const candidate = findTimezone(id);
    if (!candidate) return;
    confirmed = candidate.id;
    refreshValue();
    close();
    onChange(confirmed);
  }
  function drawChoices() {
    const found = searchTimezones(search.value);
    choices.replaceChildren();
    const empty = node("option", "Choose a timezone from the list");
    empty.value = "";
    choices.append(empty);
    for (const zone of found) {
      const option = node("option", timezoneHeading(zone) + " — " + zone.name +
        " (" + zone.id + ")");
      option.value = zone.id;
      choices.append(option);
    }
    const stillVisible = found.some(zone => zone.id === proposed);
    choices.value = stillVisible ? proposed : "";
    if (!stillVisible) proposed = "";
    choose.disabled = !proposed;
    detail.textContent = proposed ? timezoneDescription(proposed, wallDate, locale) :
      "Offsets are shown for the event date; exact instants are checked before confirmation.";
    resultCount.textContent = found.length + " timezone choices";
  }
  function drawSuggestions() {
    suggestionSection.replaceChildren();
    if (!proposedSuggestions.length) return;
    suggestionSection.append(node("p", "Suggested from extracted location (not confirmed)"));
    for (const zone of proposedSuggestions) {
      const button = node("button", timezoneHeading(zone));
      button.type = "button";
      button.className = "timezone-picker-suggestion";
      button.setAttribute("aria-label", "Select suggested " + timezoneHeading(zone));
      button.addEventListener("click", () => commit(zone.id));
      suggestionSection.append(button);
    }
  }
  trigger.addEventListener("click", () => {
    proposed = confirmed;
    search.value = "";
    drawSuggestions();
    drawChoices();
    if (typeof dialog.showModal === "function") dialog.showModal();
    else dialog.setAttribute("open", "");
    search.focus();
  });
  search.addEventListener("input", drawChoices);
  choices.addEventListener("change", () => {
    proposed = choices.value;
    choose.disabled = !findTimezone(proposed);
    detail.textContent = proposed ? timezoneDescription(proposed, wallDate, locale) : "";
  });
  choose.addEventListener("click", () => commit(proposed));
  cancel.addEventListener("click", close);
  // Escape must dismiss the dialog in HA's nested shadow-root environment.
  // Handle both native cancel and keydown, since some embedded browsers
  // deliver one without automatically closing the other.
  const dismiss = event => {
    event.preventDefault?.();
    proposed = "";
    close();
  };
  dialog.addEventListener("cancel", dismiss);
  dialog.addEventListener("keydown", event => {
    if (event.key === "Escape") dismiss(event);
  });
  refreshValue();

  return {
    element: root,
    trigger,
    get value() { return confirmed; },
    setSuggestions(ids) {
      proposedSuggestions = validSuggestedTimezones(ids);
      drawSuggestions();
    },
    clear() {
      confirmed = "";
      refreshValue();
      onChange("");
    },
  };
}
