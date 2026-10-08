import {allDayEditToTimedRange, classifyEventTimeModel, dateOnlyFromTimed, editDateTimeIso, editDateTimeValue, instantEditDateTimeIso, instantEditDateTimeValue, normalizeEventTemporalEdit, timedEditToAllDayRange, visibleAllDayEnd} from "./event_datetime.js";
import {decideEvent, formatDateTime, formatEventRange, loadActivity, loadActivityDetail, loadInbox, loadImport, resolveEvent, saveEvent, summarizeImport} from "./inbox.js";
import {isSettingsErrorCode, loadSettings, saveCoreSettings, saveEmailSettings, saveCalendarIntelligenceSettings, settingsErrorMessage} from "./settings.js";

const css = `
  :host {
    display: block;
    color: var(--primary-text-color);
    font-family: var(--paper-font-body1_-_font-family, sans-serif);
    --daylight-safe-top: 0px;
    --daylight-safe-right: 0px;
    --daylight-safe-bottom: 0px;
    --daylight-safe-left: 0px;
  }
  :host([data-own-safe-area]) {
    --daylight-safe-top: var(--safe-area-inset-top, env(safe-area-inset-top, 0px));
    --daylight-safe-right: var(
      --safe-area-content-inset-right,
      var(--safe-area-inset-right, env(safe-area-inset-right, 0px))
    );
    --daylight-safe-bottom: var(--safe-area-inset-bottom, env(safe-area-inset-bottom, 0px));
    --daylight-safe-left: var(
      --safe-area-content-inset-left,
      var(--safe-area-inset-left, env(safe-area-inset-left, 0px))
    );
  }
  .topbar {
    box-sizing: border-box;
    min-height: calc(var(--header-height, 56px) + var(--daylight-safe-top));
    display: flex;
    align-items: center;
    gap: 12px;
    padding-top: var(--daylight-safe-top);
    padding-right: max(16px, var(--daylight-safe-right));
    padding-left: max(16px, var(--daylight-safe-left));
    background: var(--app-header-background-color, var(--primary-background-color));
    color: var(--app-header-text-color, var(--primary-text-color));
    border-bottom: var(--app-header-border-bottom, 1px solid var(--divider-color));
  }
  .topbar h1 { flex: 1; min-width: 0; margin: 0; font-size: 20px; font-weight: 400; line-height: 1.25; overflow-wrap: anywhere; }
  main {
    box-sizing: border-box;
    width: 100%;
    max-width: 820px;
    min-width: 0;
    margin: 0 auto;
    padding: 20px;
    padding-right: max(20px, var(--daylight-safe-right));
    padding-bottom: max(20px, var(--daylight-safe-bottom));
    padding-left: max(20px, var(--daylight-safe-left));
  }
  button { box-sizing: border-box; min-height: 44px; max-width: 100%; overflow-wrap: anywhere; cursor: pointer; border: 1px solid var(--divider-color); border-radius: 8px; background: var(--card-background-color); color: inherit; padding: 10px 14px; font: inherit; }
  button:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
  .topbar button { background: transparent; color: inherit; border-color: currentColor; }
  .topbar button:focus-visible { outline-color: currentColor; }
  .topbar .menu-button {
    flex: 0 0 auto;
    width: 44px;
    min-width: 44px;
    padding: 10px;
    border: 0;
    border-radius: 50%;
    display: inline-flex;
    align-items: center;
    justify-content: center;
  }
  .menu-button[hidden] { display: none; }
  .menu-button svg { width: 24px; height: 24px; fill: currentColor; }
  ul { list-style: none; padding: 0; display: grid; gap: 12px; }
  li { min-width: 0; border: 1px solid var(--divider-color); border-radius: 12px; padding: 16px; background: var(--card-background-color); overflow-wrap: anywhere; }
  h2 { font-size: 1.15rem; margin: 0 0 8px; overflow-wrap: anywhere; }
  p { margin: 6px 0; overflow-wrap: anywhere; }
  .error { color: var(--error-color); }
  .source { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 18rem; overflow: auto; }
  .detail-event { border-top: 1px solid var(--divider-color); padding: 12px 0; }
  form { display: grid; gap: 12px; }
  label { display: grid; gap: 4px; }
  input, textarea, select { box-sizing: border-box; width: 100%; padding: 8px; font: inherit; color: inherit; background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 6px; }
  input[type=checkbox] { width: auto; }
  textarea { min-height: 10rem; resize: vertical; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  nav[aria-label="Daylight views"] {
    display: flex;
    flex-wrap: nowrap;
    gap: 0;
    margin: -4px 0 20px;
    overflow-x: auto;
    border-bottom: 1px solid var(--divider-color);
  }
  nav[aria-label="Daylight views"] button {
    position: relative;
    min-height: 48px;
    padding: 10px 16px;
    border: 0;
    border-radius: 0;
    background: transparent;
    white-space: nowrap;
  }
  nav[aria-label="Daylight views"] button[aria-current="page"] {
    color: var(--primary-color);
    font-weight: 500;
  }
  nav[aria-label="Daylight views"] button[aria-current="page"]::after {
    content: "";
    position: absolute;
    left: 8px;
    right: 8px;
    bottom: 0;
    height: 2px;
    background: currentColor;
  }
  .settings-tabs {
    display: flex;
    gap: 8px;
    margin: 0 0 16px;
    flex-wrap: wrap;
  }
  .settings-tabs button[aria-current="page"] {
    border-color: var(--primary-color);
    color: var(--primary-color);
  }
  .settings-card {
    border: 1px solid var(--divider-color);
    border-radius: 12px;
    padding: 16px;
    background: var(--card-background-color);
  }
  .settings-card + .settings-card { margin-top: 16px; }
  .settings-card fieldset {
    min-width: 0;
    margin: 12px 0;
    padding: 12px;
    border: 1px solid var(--divider-color);
    border-radius: 8px;
  }
  .settings-card fieldset label {
    min-width: 0;
    display: flex;
    grid-template-columns: none;
    align-items: center;
    gap: 8px;
    padding: 6px 0;
  }
  .settings-card fieldset label span {
    min-width: 0;
    overflow-wrap: anywhere;
  }
  .settings-help { color: var(--secondary-text-color); }
  @media (max-width: 480px) {
    .topbar {
      padding-right: max(8px, var(--daylight-safe-right));
      padding-left: max(8px, var(--daylight-safe-left));
    }
    main {
      padding: 12px;
      padding-right: max(12px, var(--daylight-safe-right));
      padding-bottom: max(12px, var(--daylight-safe-bottom));
      padding-left: max(12px, var(--daylight-safe-left));
    }
    li { padding: 12px; }
    .actions button, .detail-event > button { flex: 1 1 100%; width: 100%; }
    nav[aria-label="Daylight views"] button {
      flex: 0 0 auto;
      width: auto;
    }
  }
`;

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function menuButton() {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "menu-button";
  button.hidden = true;
  const svgNamespace = "http://www.w3.org/2000/svg";
  const icon = document.createElementNS(svgNamespace, "svg");
  icon.setAttribute("viewBox", "0 0 24 24");
  icon.setAttribute("aria-hidden", "true");
  const path = document.createElementNS(svgNamespace, "path");
  path.setAttribute("d", "M3,6H21V8H3V6M3,11H21V13H3V11M3,16H21V18H3V16Z");
  icon.appendChild(path);
  button.appendChild(icon);
  return button;
}

function coreVersionParts(hass) {
  const match = /^(\d{4})\.(\d{1,2})/.exec(hass?.config?.version || "");
  return match ? [Number(match[1]), Number(match[2])] : null;
}

function coreVersionAtLeast(hass, year, month) {
  const parts = coreVersionParts(hass);
  return Boolean(parts && (parts[0] > year || (parts[0] === year && parts[1] >= month)));
}

function panelOwnsSafeArea(hass) {
  const parts = coreVersionParts(hass);
  return Boolean(parts && !coreVersionAtLeast(hass, 2026, 8));
}

function shouldShowMenuButton(narrow, hass) {
  if (hass?.kioskMode !== false) return false;
  const externalSidebarOwnsToggle =
    coreVersionAtLeast(hass, 2026, 10) &&
    hass?.auth?.external?.config?.hasSidebar === true;
  if (externalSidebarOwnsToggle) return false;
  return Boolean(narrow) || hass?.dockedSidebar === "always_hidden";
}

function eventRange(event, hass) {
  return formatEventRange(event, hass?.locale, hass?.config?.time_zone);
}

function activityTime(value, hass) {
  return formatDateTime(value, hass?.locale, hass?.config?.time_zone);
}

function activityLabel(type) {
  return ({discovered: "Discovered", processing: "Processing",
    review_ready: "Awaiting review", duplicate: "Duplicate", failed: "Failed",
    event_rejected: "Rejected", calendar_write_started: "Calendar write started",
    calendar_created: "Calendar created",
    calendar_write_uncertain: "Calendar write needs confirmation",
    mixed: "Mixed event outcomes"})[type] || type.replaceAll("_", " ");
}

function syncTimedDuration(start, end, event, timeZone) {
  const model = classifyEventTimeModel(event.start, event.end, timeZone);
  const readDuration = () => {
    const startIso = editDateTimeIso(
      start.value, event.start, timeZone, start._daylightInstantHint || null, model
    );
    const endIso = editDateTimeIso(
      end.value, event.end, timeZone, end._daylightInstantHint || null, model
    );
    if (!startIso || !endIso) return null;
    const startValue = new Date(startIso);
    const endValue = new Date(endIso);
    if (Number.isNaN(startValue.getTime()) || Number.isNaN(endValue.getTime())) return null;
    return endValue.getTime() - startValue.getTime();
  };
  let duration = readDuration();
  const refresh = () => {
    duration = readDuration();
  };
  start.addEventListener("change", () => {
    start._daylightInstantHint = null;
    const startIso = editDateTimeIso(start.value, event.start, timeZone, null, model);
    if (!startIso || duration === null) return;
    const nextStart = new Date(startIso);
    if (Number.isNaN(nextStart.getTime())) return;
    const nextEnd = new Date(nextStart.getTime() + duration);
    end.value = instantEditDateTimeValue(nextEnd, event.end, timeZone, model);
    end._daylightInstantHint = instantEditDateTimeIso(
      nextEnd, event.end, timeZone, model
    );
  });
  end.addEventListener("change", () => {
    end._daylightInstantHint = null;
    refresh();
  });
  return {refresh};
}

function syncDateDuration(start, end) {
  const dayValue = value => {
    const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value || "");
    if (!match) return null;
    const [year, month, day] = match.slice(1).map(Number);
    const millis = Date.UTC(year, month - 1, day);
    const date = new Date(millis);
    return date.getUTCFullYear() === year &&
      date.getUTCMonth() === month - 1 &&
      date.getUTCDate() === day ? millis : null;
  };
  const readDuration = () => {
    const startValue = dayValue(start.value);
    const endValue = dayValue(end.value);
    return startValue === null || endValue === null ? null : endValue - startValue;
  };
  let duration = readDuration();
  const refresh = () => {
    duration = readDuration();
  };
  start.addEventListener("change", () => {
    const nextStart = dayValue(start.value);
    if (duration === null || nextStart === null) return;
    end.value = new Date(nextStart + duration).toISOString().slice(0, 10);
  });
  end.addEventListener("change", refresh);
  return {refresh};
}

function setEditDateMode(form, allDay) {
  const timedFields = form.querySelector(".timed-event-fields");
  const allDayFields = form.querySelector(".all-day-event-fields");
  if (!timedFields || !allDayFields) return;
  timedFields.hidden = allDay;
  allDayFields.hidden = !allDay;
  for (const input of timedFields.querySelectorAll("input")) {
    input.disabled = allDay;
    input.required = !allDay;
  }
  for (const input of allDayFields.querySelectorAll("input")) {
    input.disabled = !allDay;
    input.required = allDay;
  }
}

function clearEditError(form) {
  const error = form.querySelector(".error");
  if (!error) return;
  if (typeof error.remove === "function") {
    error.remove();
    return;
  }
  form.replaceChildren(
    ...Array.from(form.children || []).filter(child => child !== error)
  );
}

function showEditError(form, message) {
  clearEditError(form);
  const error = element("p", message, "error");
  error.setAttribute("role", "alert");
  error.tabIndex = -1;
  form.prepend(error);
  error.focus();
  return error;
}

function entityChoices(hass, domain, requiredFeature, configured = []) {
  const configuredIds = new Set(configured);
  const byId = new Map();
  for (const state of Object.values(hass?.states || {})) {
    if (!state?.entity_id?.startsWith(`${domain}.`)) continue;
    const supported = Number(state.attributes?.supported_features || 0);
    if ((supported & requiredFeature) !== requiredFeature) continue;
    const available = state.state !== "unavailable";
    if (!available && !configuredIds.has(state.entity_id)) continue;
    const friendly = state.attributes?.friendly_name;
    byId.set(state.entity_id, {
      id: state.entity_id,
      label: friendly ? `${friendly} (${state.entity_id})` : state.entity_id,
      available,
    });
  }
  for (const entityId of configuredIds) {
    if (!byId.has(entityId)) {
      byId.set(entityId, {
        id: entityId,
        label: `${entityId} (configured, currently unavailable)`,
        available: false,
      });
    }
  }
  return [...byId.values()].sort((left, right) =>
    left.label.localeCompare(right.label));
}

function appendOptions(select, choices, selected) {
  for (const choice of choices) {
    const option = document.createElement("option");
    option.value = choice.id;
    option.textContent = choice.label;
    option.selected = choice.id === selected;
    select.append(option);
  }
  select.value = selected || choices[0]?.id || "";
}

function setSettingsFormBusy(form, busy) {
  if (!busy) return;
  form.setAttribute("aria-busy", "true");
  for (const tag of ["input", "select", "textarea", "button"]) {
    for (const control of form.querySelectorAll(tag)) control.disabled = true;
  }
}

function aliasesEquivalent(left, right) {
  // Exact comparison only: Python's NFKC/split/casefold normalization cannot
  // be reproduced faithfully by a handful of JavaScript substitutions.
  if (!left || !right || typeof left !== "object" || typeof right !== "object") return false;
  const keys = Object.keys(left);
  return keys.length === Object.keys(right).length &&
    keys.every(key => Object.hasOwn(right, key) && right[key] === left[key]);
}

function conflictsEquivalent(left, right) {
  return Array.isArray(left) && Array.isArray(right) &&
    left.length === right.length && left.every(value => right.includes(value));
}

function settingsPatchMatches(settings, patch) {
  return Object.entries(patch).every(([key, value]) => {
    const current = settings?.[key];
    if (key === "calendar_aliases") return aliasesEquivalent(value, current);
    return Array.isArray(value) ?
      Array.isArray(current) && value.length === current.length &&
        value.every((item, index) => item === current[index]) :
      value && typeof value === "object" ?
        current && typeof current === "object" &&
        Object.keys(value).length === Object.keys(current).length &&
        Object.keys(value).every(key => current[key] === value[key]) :
        value === current;
  });
}


function parseCalendarAliases(source) {
  const entries = new Map();
  for (const line of source.split(/\r?\n/)) {
    // JavaScript trim() removes FEFF, but Python's alias normalizer does not.
    // Strip only the explicit " = " separator, never the alias itself.
    if (!line) continue;
    const delimiter = line.lastIndexOf(" = ");
    const index = delimiter >= 0 ? delimiter : line.indexOf("=");
    if (index < 1) return null;
    const name = line.slice(0, index);
    const target = line.slice(index + (delimiter >= 0 ? 3 : 1));
    if (!name || /[\r\n]/.test(name) ||
        !/^calendar\.[a-z0-9_]+$/.test(target) ||
        entries.has(name)) return null;
    entries.set(name, target);
  }
  return Object.fromEntries(entries);
}


function emailDraftMatches(settings, draft) {
  const email = settings?.email;
  if (!email || !draft) return false;
  if (draft.password) return false;
  return Object.entries(draft).every(([key, value]) =>
    key === "password" || value === email[key]);
}

function settingsDraftMatches(settings, tab, draft) {
  return tab === "email" ?
    emailDraftMatches(settings, draft) :
    settingsPatchMatches(settings, draft);
}

function emailSaveConfirmed(previousEmail, reconciledEmail, draft) {
  if (!draft.enabled) return reconciledEmail?.enabled === false;
  if (!emailDraftMatches({email: reconciledEmail}, {...draft, password: ""})) {
    return false;
  }
  if (!draft.password) return true;
  return previousEmail?.password_configured !== true &&
    reconciledEmail?.password_configured === true;
}

function isDefinitiveSettingsError(error) {
  return typeof error === "object" && error !== null &&
    typeof error.code === "string" && typeof error.message === "string";
}

const SETTINGS_RUNTIME_UNCERTAIN_WARNING =
  "Settings were saved, but Daylight could not confirm that the integration " +
  "reloaded after the connection was lost. Restart Home Assistant before " +
  "relying on the new settings.";

export class DaylightImportPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._items = [];
    this._status = "loading";
    this._loaded = false;
    this._generation = 0;
    this._selectedId = null;
    this._detail = null;
    this._returnFocusId = null;
    this._editingId = null;
    this._editError = null;
    this._saving = false;
    this._decision = null;
    this._decisionError = null;
    this._resolution = null;
    this._batchAction = null;
    this._batchResults = [];
    this._batchContext = null;
    this._view = "inbox";
    this._activity = [];
    this._activityId = null;
    this._activityDetail = null;
    this._settings = null;
    this._settingsTab = "general";
    this._settingsError = null;
    this._settingsReloadWarning = null;
    this._settingsDrafts = {general: null, calendars: null, routing: null, email: null};
    this._routingRawDraft = null;
    this._settingsSaving = false;
    const style = element("style", css);
    const header = document.createElement("header");
    header.className = "topbar";
    const menu = menuButton();
    this._menuButton = menu;
    menu.addEventListener("click", () => {
      this.dispatchEvent(new Event("hass-toggle-menu", {bubbles: true, composed: true}));
    });
    header.append(menu, element("h1", "Daylight imports"));
    const refresh = element("button", "Refresh", "toolbar-refresh");
    this._refreshButton = refresh;
    refresh.type = "button";
    refresh.addEventListener("click", () => {
      if (!this._saving && !this._settingsSaving && !this._editingId &&
          !this._batchAction && !this._resolution) void (
        this._view === "settings" ? this.showSettings(this._settingsTab, true) :
          this._view === "activity" ? this.showActivity(this._activityId) :
            this._selectedId ? this.showImport(this._selectedId) : this.refresh());
    });
    header.append(refresh);
    this._content = document.createElement("div");
    this._announcement = document.createElement("div");
    this._announcement.setAttribute("aria-live", "polite");
    const main = document.createElement("main");
    main.append(this._announcement, this._content);
    this.shadowRoot.append(style, header, main);
  }

  set narrow(value) {
    this._narrow = Boolean(value);
    this._syncMenuButton();
  }

  get narrow() {
    return Boolean(this._narrow);
  }

  _syncMenuButton() {
    this._menuButton.hidden = !shouldShowMenuButton(this._narrow, this._hass);
    const label = this._hass?.localize?.("ui.sidebar.sidebar_toggle") || "Toggle sidebar";
    this._menuButton.setAttribute("aria-label", label);
    this._menuButton.title = label;
    if (panelOwnsSafeArea(this._hass)) this.setAttribute("data-own-safe-area", "");
    else this.removeAttribute("data-own-safe-area");
  }

  set hass(value) {
    this._hass = value;
    this._syncMenuButton();
    if (!this._loaded) {
      this._loaded = true;
      void this.refresh();
    }
  }

  async refresh() {
    if (this._saving || this._editingId || this._batchAction || this._resolution) return;
    this._batchResults = [];
    this._batchContext = null;
    const generation = ++this._generation;
    this._status = "loading";
    this.render();
    try {
      const items = await loadInbox(this._hass);
      if (generation !== this._generation) return;
      this._items = items;
      this._status = "ready";
    } catch (error) {
      if (generation !== this._generation) return;
      this._status = error instanceof Error ? error.message : "Could not load imports. Try again.";
    }
    const focusedNavigation = Array.from(this._content.querySelectorAll("nav button"))
      .find(button => button === this.shadowRoot.activeElement)?.textContent;
    this.render();
    if (focusedNavigation) {
      Array.from(this._content.querySelectorAll("nav button"))
        .find(button => button.textContent === focusedNavigation && !button.disabled)?.focus();
    }
    if (this._returnFocusId) {
      const button = Array.from(this._content.querySelectorAll("button"))
        .find((candidate) => candidate.dataset.pendingId === this._returnFocusId);
      (button || this._refreshButton).focus();
      this._returnFocusId = null;
    }
  }

  async showActivity(id = null) {
    if (this._saving || this._settingsSaving || this._editingId || this._decision || this._batchAction || this._resolution) return;
    this._clearEmailDraftPassword();
    const generation = ++this._generation;
    this._view = "activity";
    this._activityId = id;
    this._activityDetail = null;
    this._status = "loading";
    this.render();
    try {
      const result = id ? await loadActivityDetail(this._hass, id) : await loadActivity(this._hass);
      if (generation !== this._generation) return;
      if (id) this._activityDetail = result;
      else this._activity = result;
      this._status = "ready";
    } catch (error) {
      if (generation !== this._generation) return;
      this._status = error instanceof Error ? error.message : "Could not load activity. Try again.";
    }
    this.render();
    (this._content.querySelector("h2") || this._content.querySelector("button") || this._refreshButton)?.focus();
  }

  async showReview() {
    if (this._saving || this._settingsSaving) return;
    this._clearEmailDraftPassword();
    this._view = "inbox";
    this._selectedId = null;
    this._detail = null;
    this._activityId = null;
    this._activityDetail = null;
    const refresh = this.refresh();
    this._refreshButton.focus();
    await refresh;
  }

  async showSettings(tab = this._settingsTab, reload = false) {
    if (this._saving || this._settingsSaving || this._editingId ||
        this._decision || this._batchAction || this._resolution) return;
    const generation = ++this._generation;
    this._view = "settings";
    this._settingsTab = tab;
    this._selectedId = null;
    this._detail = null;
    this._activityId = null;
    this._activityDetail = null;
    this._settingsError = null;
    if (this._settings && !reload) {
      this._status = "ready";
      this._announcement.replaceChildren();
      this.render();
      this._content.querySelector("h2")?.focus();
      return;
    }

    this._status = "loading";
    this._announcement.replaceChildren(element("span", "Loading settings…"));
    this.render();
    this._content.querySelector("[data-settings-loading]")?.focus();
    try {
      const settings = await loadSettings(this._hass);
      if (generation !== this._generation) return;
      this._settings = settings;
      for (const tab of ["general", "calendars", "routing", "email"]) {
        if (this._settingsDrafts[tab] &&
            settingsDraftMatches(settings, tab, this._settingsDrafts[tab])) {
          this._settingsDrafts[tab] = null;
        }
      }
      this._reconcileRoutingDraft(settings);
      this._status = "ready";
    } catch (error) {
      if (generation !== this._generation) return;
      this._settings = null;
      this._status = typeof error?.message === "string" ?
        error.message : "Could not load settings. Try again.";
    }
    this._announcement.replaceChildren();
    this.render();
    const settingsLoadError = this._content.querySelector("[data-settings-load-error]");
    (settingsLoadError || this._content.querySelector("h2") || this._refreshButton)?.focus();
  }

  _reconcileRoutingDraft(settings) {
    // Keep the same reconciliation semantics for manual refreshes and network
    // failures after saves: only fields with actual local edits stay drafted.
    const raw = this._routingRawDraft;
    if (!raw) return;
    const parsed = parseCalendarAliases(raw.aliasesText);
    const persistedAliases = settings.calendar_aliases ?? {};
    const persistedConflicts = settings.conflict_calendar_entities ??
      [settings.calendar_entity];
    const aliasesMatch = parsed && aliasesEquivalent(parsed, persistedAliases);
    const conflictsMatch = conflictsEquivalent(raw.conflictCalendarEntities, persistedConflicts);

    if (!raw.aliasesDirty || aliasesMatch) {
      raw.aliasesText = Object.entries(persistedAliases)
        .map(([name, target]) => `${name} = ${target}`).join("\n");
      raw.aliasesDirty = false;
    }
    if (!raw.conflictsDirty || conflictsMatch) {
      raw.conflictCalendarEntities = [...persistedConflicts];
      raw.conflictsDirty = false;
    }
    if (!raw.aliasesDirty && !raw.conflictsDirty) {
      this._routingRawDraft = null;
      this._settingsDrafts.routing = null;
      return;
    }
    const patch = {};
    const pendingAliases = parseCalendarAliases(raw.aliasesText);
    if (raw.aliasesDirty && pendingAliases) patch.calendar_aliases = pendingAliases;
    if (raw.conflictsDirty) {
      patch.conflict_calendar_entities = [...raw.conflictCalendarEntities];
    }
    this._settingsDrafts.routing = Object.keys(patch).length ? patch : null;
  }

  async _saveSettings(tab, patch, save, successMessage, fallbackMessage) {
    const changesPersistedSettings = !settingsPatchMatches(this._settings, patch);
    this._settingsSaving = true;
    this._settingsError = null;
    this._announcement.replaceChildren(element("span", "Saving settings…"));
    this.render();
    this._content.querySelector("[data-settings-saving]")?.focus();
    try {
      this._settings = await save();
      this._settingsDrafts[tab] = null;
      if (changesPersistedSettings) this._settingsReloadWarning = null;
      this._announcement.replaceChildren(element("span", successMessage));
    } catch (error) {
      this._announcement.replaceChildren();
      const message = settingsErrorMessage(error, fallbackMessage);
      if (isSettingsErrorCode(error, "reload_failed")) {
        this._settingsReloadWarning = message;
        this._settings = {...this._settings, ...patch};
        this._settingsDrafts[tab] = null;
        try {
          this._settings = await loadSettings(this._hass);
          if (tab === "routing") this._reconcileRoutingDraft(this._settings);
        } catch (refreshError) {
          this._settingsError = settingsErrorMessage(
            refreshError,
            "The saved settings could not be refreshed. Try again after restarting Home Assistant.",
          );
        }
      } else if (isDefinitiveSettingsError(error)) {
        this._settingsError = message;
      } else {
        try {
          const reconciled = await loadSettings(this._hass);
          this._settings = reconciled;
          if (tab === "routing") this._reconcileRoutingDraft(reconciled);
          if (settingsPatchMatches(reconciled, patch)) {
            this._settingsDrafts[tab] = null;
            if (changesPersistedSettings) {
              this._settingsReloadWarning = SETTINGS_RUNTIME_UNCERTAIN_WARNING;
            }
            this._announcement.replaceChildren(element("span", successMessage));
          } else {
            this._settingsError = message;
          }
        } catch {
          this._settingsError =
            `${message} Daylight could not confirm whether the change was saved. ` +
            "The submitted values are being kept until settings can be refreshed.";
        }
      }
    } finally {
      this._settingsSaving = false;
      this.render();
      (this._content.querySelector("[data-settings-save-error]") ||
        this._content.querySelector("[data-settings-reload-warning]") ||
        this._content.querySelector("h2") || this._refreshButton)?.focus();
    }
  }

  _setSettingsDraft(tab, patch) {
    this._settingsDrafts[tab] =
      settingsDraftMatches(this._settings, tab, patch) ? null : patch;
  }

  _clearEmailDraftPassword() {
    const passwordInput = Array.from(this._content.querySelectorAll("input"))
      .find(input => input.name === "email_password");
    if (passwordInput) passwordInput.value = "";

    const draft = this._settingsDrafts.email;
    if (!draft?.password) return;
    const sanitized = {...draft, password: ""};
    this._settingsDrafts.email =
      settingsDraftMatches(this._settings, "email", sanitized) ?
        null : sanitized;
  }

  disconnectedCallback() {
    this._clearEmailDraftPassword();
  }

  async saveGeneralSettings(form) {
    if (this._settingsSaving || !this._settings) return;
    const aiTaskEntity = form.elements.namedItem("ai_task_entity").value;
    const entryId = this._settings.entry_id;
    const patch = {ai_task_entity: aiTaskEntity};
    this._setSettingsDraft("general", patch);
    await this._saveSettings(
      "general",
      patch,
      () => saveCoreSettings(this._hass, {entry_id: entryId, ...patch}),
      "General settings saved",
      "Could not save general settings.",
    );
  }

  async saveCalendarSettings(form) {
    if (this._settingsSaving || !this._settings) return;
    const fields = form.elements;
    const allowed = Array.from(form.querySelectorAll("input"))
      .filter(input => input.type === "checkbox" && input.checked)
      .map(input => input.value);
    const defaultCalendar = fields.namedItem("calendar_entity").value;
    const patch = {
      calendar_entity: defaultCalendar,
      calendar_entities: allowed,
    };
    this._setSettingsDraft("calendars", patch);
    if (!allowed.includes(defaultCalendar)) {
      this._settingsError = "The default calendar must also be selected as writable.";
      this._announcement.replaceChildren();
      this.render();
      this._content.querySelector("[data-settings-save-error]")?.focus();
      return;
    }

    const entryId = this._settings.entry_id;
    await this._saveSettings(
      "calendars",
      patch,
      () => saveCoreSettings(this._hass, {entry_id: entryId, ...patch}),
      "Calendar settings saved",
      "Could not save calendar settings.",
    );
  }

  settingsSubnav() {
    const nav = element("nav", "", "settings-tabs");
    nav.setAttribute("aria-label", "Settings sections");
    for (const [tab, label] of [
      ["general", "General"],
      ["calendars", "Calendars"],
      ["routing", "Routing"],
      ["email", "Email"],
    ]) {
      const button = element("button", label);
      button.type = "button";
      button.disabled = this._settingsSaving;
      if (this._settingsTab === tab) button.setAttribute("aria-current", "page");
      button.addEventListener("click", () => {
        if (this._settingsSaving || this._settingsTab === tab) return;
        this._settingsTab = tab;
        this._settingsError = null;
        this._announcement.replaceChildren();
        this.render();
        this._content.querySelector("h2")?.focus();
      });
      nav.append(button);
    }
    return nav;
  }

  generalSettingsView() {
    const section = element("section", "", "settings-card");
    const heading = element("h2", "General");
    heading.tabIndex = -1;
    section.append(
      heading,
      element("p", "Choose the AI Task entity Daylight uses to extract calendar events.", "settings-help"),
    );
    const form = document.createElement("form");
    const label = element("label", "AI Task entity");
    const select = document.createElement("select");
    select.name = "ai_task_entity";
    const selectedAi = this._settingsDrafts.general?.ai_task_entity ??
      this._settings.ai_task_entity;
    const choices = entityChoices(
      this._hass, "ai_task", 1, [this._settings.ai_task_entity, selectedAi],
    );
    appendOptions(select, choices, selectedAi);
    select.addEventListener("change", () => {
      this._setSettingsDraft("general", {ai_task_entity: select.value});
    });
    label.append(select);
    const save = element("button", this._settingsSaving ? "Saving…" : "Save general settings");
    save.type = "submit";
    save.disabled = this._settingsSaving || choices.length === 0;
    form.append(label, save);
    form.addEventListener("submit", event => {
      event.preventDefault();
      void this.saveGeneralSettings(form);
    });
    setSettingsFormBusy(form, this._settingsSaving);
    section.append(form);
    return section;
  }

  calendarSettingsView() {
    const section = element("section", "", "settings-card");
    const heading = element("h2", "Calendars");
    heading.tabIndex = -1;
    section.append(
      heading,
      element("p", "Choose the default destination and the calendars available during review.", "settings-help"),
    );
    const form = document.createElement("form");
    const draft = this._settingsDrafts.calendars;
    const selectedDefault = draft?.calendar_entity || this._settings.calendar_entity;
    const selectedAllowed = draft?.calendar_entities || this._settings.calendar_entities;
    const defaultChoices = entityChoices(
      this._hass, "calendar", 1, [selectedDefault],
    );
    const writableChoices = entityChoices(
      this._hass, "calendar", 1, [...selectedAllowed, selectedDefault],
    );

    const updateCalendarDraft = () => {
      const allowed = Array.from(form.querySelectorAll("input"))
        .filter(input => input.type === "checkbox" && input.checked)
        .map(input => input.value);
      this._setSettingsDraft("calendars", {
        calendar_entity: defaultSelect.value,
        calendar_entities: allowed,
      });
    };

    const defaultLabel = element("label", "Default calendar");
    const defaultSelect = document.createElement("select");
    defaultSelect.name = "calendar_entity";
    appendOptions(defaultSelect, defaultChoices, selectedDefault);
    defaultSelect.addEventListener("change", updateCalendarDraft);
    defaultLabel.append(defaultSelect);
    form.append(defaultLabel);

    const fieldset = document.createElement("fieldset");
    fieldset.append(element("legend", "Writable calendars"));
    for (const choice of writableChoices) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "calendar_entities";
      input.value = choice.id;
      input.checked = selectedAllowed.includes(choice.id);
      input.addEventListener("change", updateCalendarDraft);
      label.append(input, element("span", choice.label));
      fieldset.append(label);
    }
    form.append(fieldset);
    const save = element("button", this._settingsSaving ? "Saving…" : "Save calendar settings");
    save.type = "submit";
    save.disabled = this._settingsSaving ||
      defaultChoices.length === 0 || writableChoices.length === 0;
    form.append(save);
    form.addEventListener("submit", event => {
      event.preventDefault();
      void this.saveCalendarSettings(form);
    });
    setSettingsFormBusy(form, this._settingsSaving);
    section.append(form);
    return section;
  }


  routingSettingsView() {
    const section = element("section", "", "settings-card");
    const heading = element("h2", "Calendar routing & conflict checks");
    heading.tabIndex = -1;
    section.append(
      heading,
      element("p", "Map exact aliases used in forwarded messages to writable calendars. " +
        "Conflict calendars are read-only observation targets; they do not grant write access.", "settings-help"),
    );
    const form = document.createElement("form");
    const draft = this._settingsDrafts.routing;
    const aliases = draft?.calendar_aliases ?? this._settings.calendar_aliases ?? {};
    const selectedConflicts = this._routingRawDraft?.conflictCalendarEntities ??
      draft?.conflict_calendar_entities ??
      this._settings.conflict_calendar_entities ?? [this._settings.calendar_entity];
    const aliasLabel = element("label", "Calendar aliases (one per line: name = calendar.entity)");
    const textarea = document.createElement("textarea");
    textarea.name = "calendar_aliases";
    textarea.rows = 5;
    textarea.placeholder = "Kids = calendar.kids";
    textarea.value = this._routingRawDraft?.aliasesText ??
      Object.entries(aliases).map(([name, entity]) => `${name} = ${entity}`).join("\n");
    const recordRoutingDraft = () => {
      const conflicts = Array.from(form.querySelectorAll("input"))
        .filter(input => input.checked).map(input => input.value);
      const parsed = parseCalendarAliases(textarea.value);
      const aliasesDirty = !parsed ||
        !aliasesEquivalent(parsed, this._settings.calendar_aliases ?? {});
      const conflictsDirty = !conflictsEquivalent(conflicts,
        this._settings.conflict_calendar_entities ?? [this._settings.calendar_entity]);
      this._routingRawDraft = {
        aliasesText: textarea.value,
        conflictCalendarEntities: conflicts,
        aliasesDirty,
        conflictsDirty,
      };
      const patch = {};
      if (aliasesDirty && parsed) patch.calendar_aliases = parsed;
      if (conflictsDirty) patch.conflict_calendar_entities = conflicts;
      this._settingsDrafts.routing = Object.keys(patch).length ? patch : null;
    };
    textarea.addEventListener("input", recordRoutingDraft);
    textarea.addEventListener("change", recordRoutingDraft);
    aliasLabel.append(textarea);
    form.append(aliasLabel);
    const fieldset = document.createElement("fieldset");
    fieldset.append(element("legend", "Calendars checked for conflicts (read-only)"));
    const choices = entityChoices(this._hass, "calendar", 0,
      [...selectedConflicts, ...this._settings.calendar_entities]);
    for (const choice of choices) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.name = "conflict_calendar_entities";
      input.value = choice.id;
      input.checked = selectedConflicts.includes(choice.id);
      input.addEventListener("change", recordRoutingDraft);
      label.append(input, element("span", choice.label));
      fieldset.append(label);
    }
    form.append(fieldset);
    const save = element("button", this._settingsSaving ? "Saving…" : "Save routing settings");
    save.type = "submit";
    save.disabled = this._settingsSaving;
    form.append(save);
    form.addEventListener("submit", event => {
      event.preventDefault();
      void this.saveRoutingSettings(form);
    });
    setSettingsFormBusy(form, this._settingsSaving);
    section.append(form);
    return section;
  }

  async saveRoutingSettings(form) {
    if (this._settingsSaving || !this._settings) return;
    const textarea = form.elements.namedItem("calendar_aliases");
    const parsed = parseCalendarAliases(textarea.value);
    this._routingRawDraft = {
      aliasesText: textarea.value,
      conflictCalendarEntities: Array.from(form.querySelectorAll("input"))
        .filter(input => input.checked).map(input => input.value),
      aliasesDirty: !parsed ||
        !aliasesEquivalent(parsed, this._settings.calendar_aliases ?? {}),
      conflictsDirty: !conflictsEquivalent(
        Array.from(form.querySelectorAll("input"))
          .filter(input => input.checked).map(input => input.value),
        this._settings.conflict_calendar_entities ?? [this._settings.calendar_entity]),

    };
    if (!parsed) {
      showEditError(form, "Use one alias per line in the format: Kids = calendar.family");
      return;
    }
    const writable = new Set(this._settings.calendar_entities);
    if (Object.values(parsed).some(target => !writable.has(target))) {
      showEditError(form, "Every alias must point to a selected writable calendar.");
      return;
    }
    const selected = Array.from(form.querySelectorAll("input"))
      .filter(input => input.checked).map(input => input.value);
    const patch = {};
    if (!aliasesEquivalent(parsed, this._settings.calendar_aliases ?? {})) {
      patch.calendar_aliases = parsed;
    }
    const current = this._settings.conflict_calendar_entities ?? [this._settings.calendar_entity];
    if (!conflictsEquivalent(selected, current)) {
      patch.conflict_calendar_entities = selected;
    }
    if (Object.keys(patch).length === 0) {
      this._settingsDrafts.routing = null;
      this._routingRawDraft = null;
      this._settingsError = null;
      this._announcement.replaceChildren();
      this.render();
      return;
    }
    this._setSettingsDraft("routing", patch);
    await this._saveSettings(
      "routing", patch,
      () => saveCalendarIntelligenceSettings(
        this._hass, {entry_id: this._settings.entry_id, ...patch},
      ),
      "Calendar routing settings saved",
      "Could not save calendar routing settings.",
    );
    if (!this._settingsDrafts.routing) this._routingRawDraft = null;
  }

  _emailDraftFromForm(form) {
    const fields = form.elements;
    return {
      enabled: fields.namedItem("email_enabled").checked,
      host: fields.namedItem("email_host").value.trim(),
      port: Number(fields.namedItem("email_port").value),
      username: fields.namedItem("email_username").value.trim(),
      password: fields.namedItem("email_password").value,
      mailbox: fields.namedItem("email_mailbox").value.trim(),
      verify_ssl: fields.namedItem("email_verify_ssl").checked,
    };
  }

  async saveEmailSettingsForm(form) {
    if (this._settingsSaving || !this._settings) return;
    const draft = this._emailDraftFromForm(form);
    this._setSettingsDraft("email", draft);
    const entryId = this._settings.entry_id;
    const previousEmail = this._settings.email;
    const payload = {entry_id: entryId, enabled: draft.enabled};
    if (draft.enabled) {
      Object.assign(payload, {
        host: draft.host,
        port: draft.port,
        username: draft.username,
        password: draft.password,
        mailbox: draft.mailbox,
        verify_ssl: draft.verify_ssl,
      });
    }

    this._settingsSaving = true;
    this._settingsError = null;
    this._announcement.replaceChildren(element("span", "Saving settings…"));
    this.render();
    this._content.querySelector("[data-settings-saving]")?.focus();
    try {
      this._settings = await saveEmailSettings(this._hass, payload);
      this._settingsDrafts.email = null;
      this._settingsReloadWarning = null;
      this._announcement.replaceChildren(element(
        "span",
        draft.enabled ? "Direct IMAP settings saved" : "Direct IMAP disabled",
      ));
    } catch (error) {
      this._announcement.replaceChildren();
      const message = settingsErrorMessage(
        error, "Could not save Direct IMAP settings.",
      );
      if (isSettingsErrorCode(error, "reload_failed")) {
        this._settingsReloadWarning = message;
        const visibleEmail = draft.enabled ? {
          enabled: true,
          host: draft.host,
          port: draft.port,
          username: draft.username,
          mailbox: draft.mailbox,
          verify_ssl: draft.verify_ssl,
          password_configured:
            previousEmail.password_configured || Boolean(draft.password),
        } : {enabled: false};
        this._settings = {
          ...this._settings,
          email: {...this._settings.email, ...visibleEmail},
        };
        this._settingsDrafts.email = null;
        try {
          this._settings = await loadSettings(this._hass);
        } catch (refreshError) {
          this._settingsError = settingsErrorMessage(
            refreshError,
            "The saved settings could not be refreshed. Try again after restarting Home Assistant.",
          );
        }
      } else if (isDefinitiveSettingsError(error)) {
        this._settingsError = message;
      } else {
        try {
          const reconciled = await loadSettings(this._hass);
          this._settings = reconciled;
          if (emailSaveConfirmed(previousEmail, reconciled.email, draft)) {
            this._settingsDrafts.email = null;
            this._settingsReloadWarning = SETTINGS_RUNTIME_UNCERTAIN_WARNING;
            this._announcement.replaceChildren(element(
              "span",
              draft.enabled ? "Direct IMAP settings saved" : "Direct IMAP disabled",
            ));
          } else {
            this._settingsError = message;
          }
        } catch {
          this._settingsError =
            `${message} Daylight could not confirm whether the change was saved. ` +
            "The submitted values are being kept until settings can be refreshed.";
        }
      }
    } finally {
      this._settingsSaving = false;
      this.render();
      (this._content.querySelector("[data-settings-save-error]") ||
        this._content.querySelector("[data-settings-reload-warning]") ||
        this._content.querySelector("h2") || this._refreshButton)?.focus();
    }
  }

  emailSettingsView() {
    const section = element("section", "", "settings-card");
    const heading = element("h2", "Email");
    heading.tabIndex = -1;
    section.append(
      heading,
      element(
        "p",
        "Configure optional Direct IMAP ingestion. Daylight reads every unread message in the configured mailbox, so a dedicated address or folder is recommended.",
        "settings-help",
      ),
    );

    const form = document.createElement("form");
    const email = this._settings.email;
    const draft = this._settingsDrafts.email;
    const value = (key, fallback) => draft?.[key] ?? fallback;

    const enabledLabel = document.createElement("label");
    const enabled = document.createElement("input");
    enabled.type = "checkbox";
    enabled.name = "email_enabled";
    enabled.checked = value("enabled", email.enabled);
    enabledLabel.append(enabled, element("span", "Enable Direct IMAP ingestion"));
    form.append(enabledLabel);

    const connection = document.createElement("fieldset");
    connection.append(element("legend", "Connection"));
    connection.disabled = !enabled.checked;
    form.append(connection);

    const textField = (name, labelText, field, fallback, type = "text") => {
      const label = element("label", labelText);
      const input = document.createElement("input");
      input.type = type;
      input.name = name;
      input.value = value(field, fallback) ?? "";
      label.append(input);
      connection.append(label);
      return input;
    };

    const host = textField("email_host", "IMAP host", "host", email.host);
    host.autocomplete = "off";

    const port = textField(
      "email_port", "IMAP port", "port", email.port ?? 993, "number",
    );
    port.min = "1";
    port.max = "65535";
    port.step = "1";

    const username = textField(
      "email_username", "Username", "username", email.username,
    );
    username.autocomplete = "username";

    const password = textField(
      "email_password", "Password / app password", "password", "", "password",
    );
    password.autocomplete = "new-password";
    password.placeholder = email.password_configured ?
      "Configured — leave blank to keep existing password" :
      "Enter password or app password";

    textField(
      "email_mailbox", "Mailbox / folder", "mailbox", email.mailbox ?? "INBOX",
    );

    const sslLabel = document.createElement("label");
    const verifySsl = document.createElement("input");
    verifySsl.type = "checkbox";
    verifySsl.name = "email_verify_ssl";
    verifySsl.checked = value("verify_ssl", email.verify_ssl) !== false;
    sslLabel.append(verifySsl, element("span", "Verify TLS certificate"));
    connection.append(sslLabel);

    const updateDraft = () => {
      connection.disabled = !enabled.checked;
      this._setSettingsDraft("email", this._emailDraftFromForm(form));
    };
    enabled.addEventListener("change", updateDraft);
    verifySsl.addEventListener("change", updateDraft);
    for (const input of [host, port, username, password,
      form.elements.namedItem("email_mailbox")]) {
      input.addEventListener("input", updateDraft);
    }

    if (email.password_configured) {
      connection.append(element(
        "p",
        "A password is stored securely in the Home Assistant config entry. Leave the field blank to keep it.",
        "settings-help",
      ));
    }

    const save = element(
      "button",
      this._settingsSaving && enabled.checked ?
        "Validating and saving…" :
        this._settingsSaving ? "Saving…" : "Save email settings",
    );
    save.type = "submit";
    save.disabled = this._settingsSaving;
    form.append(save);
    form.addEventListener("submit", event => {
      event.preventDefault();
      void this.saveEmailSettingsForm(form);
    });
    setSettingsFormBusy(form, this._settingsSaving);
    section.append(form);
    return section;
  }

  async showImport(id) {
    if (this._saving || this._editingId || this._batchAction || this._resolution) return;
    this._clearEmailDraftPassword();
    const generation = ++this._generation;
    this._selectedId = id;
    this._detail = null;
    this._editingId = null;
    this._editError = null;
    this._decision = null;
    this._decisionError = null;
    this._resolution = null;
    this._batchAction = null;
    this._batchResults = [];
    this._batchContext = null;
    this._status = "loading";
    this.render();
    this._content.querySelector("button")?.focus();
    try {
      const detail = await loadImport(this._hass, id);
      if (generation !== this._generation) return;
      this._detail = detail;
      this._status = "ready";
    } catch (error) {
      if (generation !== this._generation) return;
      this._status = error instanceof Error ? error.message : "Could not load import. Try again.";
    }
    this.render();
    (this._content.querySelector("h2") || this._content.querySelector("button"))?.focus();
  }

  showInbox() {
    if (this._saving || this._settingsSaving || this._editingId || this._decision || this._batchAction || this._resolution) return;
    this._clearEmailDraftPassword();
    this._batchResults = [];
    this._batchContext = null;
    this._returnFocusId = this._selectedId;
    ++this._generation;
    this._selectedId = null;
    this._detail = null;
    void this.refresh();
  }

  async runBatch(action) {
    if (this._saving || this._batchAction !== action) return;
    const pendingId = this._selectedId;
    const events = this._detail.events.filter(event => event.status === "pending");
    this._batchContext = {pendingId, action, title: this._detail.source_title || "Import"};
    this._saving = true;
    this._refreshButton.disabled = true;
    for (const button of this._content.querySelectorAll("button")) button.disabled = true;
    const results = [];
    for (const event of events) {
      try {
        await decideEvent(this._hass, pendingId, event, action);
        results.push({id: event.id, title: event.title, range: eventRange(event, this._hass), outcome: "success"});
      } catch (error) {
        const reason = typeof error?.message === "string" ? error.message : "Review action failed";
        results.push({id: event.id, title: event.title, range: eventRange(event, this._hass),
          outcome: action === "approve" ?
            `Approval outcome unknown; check the calendar before retrying. ${reason}` : reason});
      }
    }
    this._batchResults = results;
    this._batchAction = null;
    this._saving = false;
    try {
      this._items = await loadInbox(this._hass);
      if (this._items.some(item => item.id === pendingId)) {
        this._detail = await loadImport(this._hass, pendingId);
      } else {
        this._selectedId = null;
        this._detail = null;
      }
      this._status = "ready";
    } catch (error) {
      this._detail = null;
      this._status = typeof error?.message === "string" ? error.message : "Could not reload imports after bulk review.";
    }
    this.render();
    this._content.querySelector(".batch-results")?.focus();
  }

  async runDecision(event, action) {
    if (this._saving || this._decision?.id !== event.id || this._decision.action !== action) return;
    const pendingId = this._selectedId;
    const generation = this._generation;
    this._saving = true;
    this._refreshButton.disabled = true;
    for (const button of this._content.querySelectorAll("button")) button.disabled = true;
    try {
      await decideEvent(this._hass, pendingId, event, action);
      if (generation !== this._generation) return;
      this._saving = false;
      this._decision = null;
      this._selectedId = null;
      this._detail = null;
      await this.refresh();
      if (this._status === "ready") {
        this._announcement.replaceChildren(element("span", `Event ${action === "approve" ? "approved" : "rejected"}`));
      }
      (Array.from(this._content.querySelectorAll("button")).find(button => button.dataset.pendingId) || this._refreshButton).focus();
    } catch (error) {
      if (generation !== this._generation) return;
      this._decisionError = typeof error?.message === "string" ? error.message : "Could not complete review action.";
      try {
        this._detail = await loadImport(this._hass, pendingId);
      } catch {
        this._detail = null;
        this._status = `Could not reload import after review action: ${this._decisionError}`;
      }
      if (generation !== this._generation) return;
      this._saving = false;
      this._decision = null;
      this.render();
      const alert = this._content.querySelector(".error");
      if (alert) { alert.tabIndex = -1; alert.focus(); }
      else this._content.querySelector("button")?.focus();
    } finally {
      this._saving = false;
      this._refreshButton.disabled = Boolean(this._editingId || this._decision);
    }
  }

  async runResolution(event, resolution) {
    if (this._saving || this._resolution?.id !== event.id || this._resolution.choice !== resolution) return;
    const pendingId = this._selectedId;
    const generation = this._generation;
    this._saving = true;
    this._refreshButton.disabled = true;
    for (const button of this._content.querySelectorAll("button")) button.disabled = true;
    let saved = false;
    try {
      await resolveEvent(this._hass, pendingId, event, resolution);
      saved = true;
      if (generation !== this._generation) return;
      this._resolution = null;
      this._saving = false;
      this._items = await loadInbox(this._hass);
      if (this._items.some(item => item.id === pendingId)) {
        this._detail = await loadImport(this._hass, pendingId);
      } else {
        this._selectedId = null;
        this._detail = null;
      }
      if (generation !== this._generation) return;
      this._status = "ready";
      this._batchResults = [];
      this._batchContext = null;
      this.render();
      this._announcement.replaceChildren(element("span", "Recovery choice saved"));
      (Array.from(this._content.querySelectorAll("button"))
        .find(button => button.dataset.eventId === event.id) ||
        Array.from(this._content.querySelectorAll("button")).find(button => button.dataset.pendingId) ||
        this._refreshButton).focus();
    } catch (error) {
      if (generation !== this._generation) return;
      const reason = typeof error?.message === "string" ? error.message : "Could not save recovery choice.";
      if (saved) {
        this._resolution = null;
        this._detail = null;
        this._selectedId = null;
        this._batchResults = [];
        this._batchContext = null;
        this._status = `Recovery choice saved, but the view could not reload: ${reason}. Refresh to see the current state.`;
        this.render();
        this._refreshButton.focus();
        return;
      }
      this._decisionError = `Recovery outcome unknown; check the current event before retrying. ${reason}`;
      try {
        this._items = await loadInbox(this._hass);
        if (this._items.some(item => item.id === pendingId)) {
          this._detail = await loadImport(this._hass, pendingId);
        } else {
          this._selectedId = null;
          this._detail = null;
          this._status = this._decisionError;
        }
      } catch {
        this._selectedId = null;
        this._detail = null;
        this._status = `Could not reload imports after recovery: ${this._decisionError}`;
      }
      if (generation !== this._generation) return;
      this._resolution = null;
      this.render();
      const alert = this._content.querySelector(".error");
      if (alert) { alert.tabIndex = -1; alert.focus(); }
      else this._refreshButton.focus();
    } finally {
      this._saving = false;
      this._refreshButton.disabled = false;
    }
  }

  async saveEdit(event, form) {
    if (this._saving) return;
    const fields = form.elements;
    const allDay = fields.namedItem("all_day").checked;
    const temporal = normalizeEventTemporalEdit({
      allDay,
      startDate: fields.namedItem("start_date").value,
      endDate: fields.namedItem("end_date").value,
      startDateTime: fields.namedItem("start").value,
      endDateTime: fields.namedItem("end").value,
      originalStart: event.start,
      originalEnd: event.end,
      timeZone: this._hass?.config?.time_zone,
      startInstantHint: fields.namedItem("start")._daylightInstantHint || null,
      endInstantHint: fields.namedItem("end")._daylightInstantHint || null,
    });
    if (!temporal.valid) {
      this._editError = temporal.error;
      this._announcement.replaceChildren(element("span", temporal.error));
      showEditError(form, temporal.error);
      return;
    }

    clearEditError(form);
    const draft = {
      title: fields.namedItem("title").value,
      start: temporal.start,
      end: temporal.end,
      all_day: allDay,
      location: fields.namedItem("location").value,
      description: fields.namedItem("description").value,
      confidence: event.confidence,
    };
    const calendarEntity = fields.namedItem("calendar_entity")?.value ||
      event.calendar_entity || this._detail?.default_calendar || null;
    const generation = this._generation;
    const pendingId = this._selectedId;
    this._saving = true;
    this._editError = null;
    this._refreshButton.disabled = true;
    for (const button of this._content.querySelectorAll("button")) button.disabled = true;
    for (const tag of ["input", "textarea", "select", "button"]) {
      for (const control of form.querySelectorAll(tag)) control.disabled = true;
    }
    try {
      await saveEvent(this._hass, pendingId, event, draft, calendarEntity);
      if (generation !== this._generation) return;
      this._editingId = null;
      this._decisionError = null;
      this._detail = await loadImport(this._hass, pendingId);
      if (generation !== this._generation) return;
      this.render();
      this._announcement.replaceChildren(element("span", "Event saved"));
      this.focusEvent(event.id);
    } catch (error) {
      if (generation !== this._generation) return;
      this._editError = typeof error?.message === "string" ? error.message : "Could not save event.";
      if (this._editingId === null) {
        this._status = `Event saved, but the detail could not be reloaded: ${this._editError}`;
        this._detail = null;
        this.render();
        this._content.querySelector("button")?.focus();
        return;
      }
      this._announcement.replaceChildren(element("span", this._editError));
      for (const tag of ["input", "textarea", "select", "button"]) {
        for (const control of form.querySelectorAll(tag)) control.disabled = false;
      }
      setEditDateMode(form, fields.namedItem("all_day").checked);
      showEditError(form, this._editError);
    } finally {
      this._saving = false;
      this._refreshButton.disabled = Boolean(this._editingId);
    }
  }

  focusEvent(id) {
    (Array.from(this._content.querySelectorAll("button"))
      .find(button => button.dataset.eventId === id) ||
      this._content.querySelector("h2") || this._content.querySelector("button"))?.focus();
  }

  editForm(event) {
    const form = document.createElement("form");
    const field = (name, title, value, tag = "input") => {
      const label = element("label", title);
      const input = document.createElement(tag);
      input.name = name;
      input.value = value ?? "";
      label.append(input);
      form.append(label);
      return input;
    };
    field("title", "Title", event.title).required = true;
    const allDay = field("all_day", "All day", "");
    allDay.type = "checkbox";
    allDay.checked = event.all_day;

    const timeZone = this._hass?.config?.time_zone;
    const timedFields = document.createElement("div");
    timedFields.className = "timed-event-fields";
    const timedField = (name, title, value) => {
      const label = element("label", title);
      const input = document.createElement("input");
      input.name = name;
      input.type = "datetime-local";
      input.step = "60";
      input.value = value;
      label.append(input);
      timedFields.append(label);
      return input;
    };
    const start = timedField(
      "start",
      "Start",
      event.all_day ? event.start + "T00:00" : editDateTimeValue(event.start, timeZone),
    );
    const end = timedField(
      "end",
      "End",
      event.all_day ? event.end + "T00:00" : editDateTimeValue(event.end, timeZone),
    );
    const timedSync = syncTimedDuration(start, end, event, timeZone);

    const allDayFields = document.createElement("div");
    allDayFields.className = "all-day-event-fields";
    const dateField = (name, title, value) => {
      const label = element("label", title);
      const input = document.createElement("input");
      input.name = name;
      input.type = "date";
      input.value = value;
      label.append(input);
      allDayFields.append(label);
      return input;
    };
    const startDate = dateField(
      "start_date",
      "Start date",
      event.all_day ? event.start : dateOnlyFromTimed(event.start, timeZone),
    );
    const endDate = dateField(
      "end_date",
      "End date",
      event.all_day ? visibleAllDayEnd(event.end) : dateOnlyFromTimed(event.end, timeZone),
    );
    const dateSync = syncDateDuration(startDate, endDate);
    form.append(timedFields, allDayFields);
    setEditDateMode(form, allDay.checked);

    let preserveTimedTimes = !event.all_day;
    let preservedTimedRange = null;
    allDay.addEventListener("change", () => {
      if (allDay.checked) {
        const temporal = normalizeEventTemporalEdit({
          allDay: false,
          startDateTime: start.value,
          endDateTime: end.value,
          originalStart: event.start,
          originalEnd: event.end,
          timeZone,
          startInstantHint: start._daylightInstantHint || null,
          endInstantHint: end._daylightInstantHint || null,
        });
        const range = temporal.valid ?
          timedEditToAllDayRange(start.value, end.value) : null;
        if (!range) {
          allDay.checked = false;
          setEditDateMode(form, false);
          const message = temporal.valid ?
            "Fix the start and end times before switching to all day." :
            temporal.error;
          this._editError = message;
          this._announcement.replaceChildren(element("span", message));
          showEditError(form, message);
          return;
        }
        preservedTimedRange = {
          startDateTime: start.value,
          endDateTime: end.value,
          startInstant: temporal.start,
          endInstant: temporal.end,
        };
        startDate.value = range.startDate;
        endDate.value = range.endDate;
        dateSync.refresh();
      } else {
        const range = allDayEditToTimedRange(
          startDate.value,
          endDate.value,
          start.value,
          end.value,
          preserveTimedTimes,
        );
        if (!range) {
          allDay.checked = true;
          setEditDateMode(form, true);
          const message = "Fix the start and end dates before switching to timed.";
          this._editError = message;
          this._announcement.replaceChildren(element("span", message));
          showEditError(form, message);
          return;
        }
        start.value = range.startDateTime;
        end.value = range.endDateTime;
        const restoresPreservedRange = preservedTimedRange &&
          preservedTimedRange.startDateTime === range.startDateTime &&
          preservedTimedRange.endDateTime === range.endDateTime;
        start._daylightInstantHint = restoresPreservedRange ?
          preservedTimedRange.startInstant : null;
        end._daylightInstantHint = restoresPreservedRange ?
          preservedTimedRange.endInstant : null;
        timedSync.refresh();
        preserveTimedTimes = true;
      }
      this._editError = null;
      clearEditError(form);
      setEditDateMode(form, allDay.checked);
    });

    field("location", "Location", event.location);
    field("description", "Description and meeting join details", event.description, "textarea");
    const selectedCalendar = event.calendar_entity || this._detail?.default_calendar || "";
    const calendars = [...new Set([
      ...(this._detail?.allowed_calendars || []),
      selectedCalendar,
    ].filter(Boolean))];
    if (calendars.length) {
      const label = element("label", "Calendar");
      const select = document.createElement("select");
      select.name = "calendar_entity";
      for (const calendar of calendars) {
        const option = document.createElement("option");
        option.value = calendar;
        const friendly = this._hass?.states?.[calendar]?.attributes?.friendly_name;
        option.textContent = friendly ? `${friendly} (${calendar})` : calendar;
        select.append(option);
      }
      select.value = selectedCalendar || calendars[0];
      label.append(select);
      form.append(label);
    }
    const actions = element("div", "", "actions");
    const save = element("button", "Save");
    save.type = "submit";
    const cancel = element("button", "Cancel");
    cancel.type = "button";
    cancel.addEventListener("click", () => { if (!this._saving) {
      this._editingId = null; this.render(); this.focusEvent(event.id);
    } });
    actions.append(save, cancel);
    form.append(actions);
    form.addEventListener("submit", (submitEvent) => {
      submitEvent.preventDefault();
      void this.saveEdit(event, form);
    });
    return form;
  }

  render() {
    this._refreshButton.disabled = this._saving || this._settingsSaving ||
      Boolean(this._editingId || this._decision || this._batchAction || this._resolution);
    const content = document.createDocumentFragment();
    const navigation = element("nav", "", "actions");
    navigation.setAttribute("aria-label", "Daylight views");
    const review = element("button", "Review inbox");
    review.type = "button";
    review.disabled = this._view === "inbox" || this._saving || this._settingsSaving;
    if (this._view === "inbox") review.setAttribute("aria-current", "page");
    review.addEventListener("click", () => void this.showReview());
    const activity = element("button", "Recent activity");
    activity.type = "button";
    activity.disabled = this._view === "activity" || this._saving || this._settingsSaving ||
      Boolean(this._editingId || this._decision || this._batchAction || this._resolution);
    if (this._view === "activity") activity.setAttribute("aria-current", "page");
    activity.addEventListener("click", () => void this.showActivity());
    navigation.append(review, activity);
    if (this._hass?.user?.is_admin === true) {
      const settings = element("button", "Settings");
      settings.type = "button";
      settings.disabled = this._view === "settings" || this._saving || this._settingsSaving ||
        Boolean(this._editingId || this._decision || this._batchAction || this._resolution);
      if (this._view === "settings") settings.setAttribute("aria-current", "page");
      settings.addEventListener("click", () => void this.showSettings());
      navigation.append(settings);
    }
    if (this._view === "settings") {
      content.append(navigation);
      if (this._status === "loading") {
        const loading = element("p", "Loading settings…", "status");
        loading.setAttribute("role", "status");
        loading.setAttribute("data-settings-loading", "");
        loading.tabIndex = -1;
        content.append(loading);
      } else if (this._status !== "ready" || !this._settings) {
        const error = element("p", this._status, "error");
        error.setAttribute("role", "alert");
        error.setAttribute("data-settings-load-error", "");
        error.tabIndex = -1;
        content.append(error);
      } else {
        content.append(this.settingsSubnav());
        if (this._settingsSaving) {
          const saving = element("p", "Saving settings…", "status");
          saving.setAttribute("role", "status");
          saving.setAttribute("data-settings-saving", "");
          saving.tabIndex = -1;
          content.append(saving);
        }
        if (this._settingsReloadWarning) {
          const warning = element("p", this._settingsReloadWarning, "error");
          warning.setAttribute("role", "alert");
          warning.setAttribute("data-settings-reload-warning", "");
          warning.tabIndex = -1;
          content.append(warning);
        }
        if (this._settingsError) {
          const error = element("p", this._settingsError, "error");
          error.setAttribute("role", "alert");
          error.setAttribute("data-settings-save-error", "");
          error.tabIndex = -1;
          content.append(error);
        }
        content.append(
          this._settingsTab === "calendars" ?
            this.calendarSettingsView() :
            this._settingsTab === "routing" ?
              this.routingSettingsView() :
            this._settingsTab === "email" ?
              this.emailSettingsView() : this.generalSettingsView(),
        );
      }
      this._content.replaceChildren(content);
      return;
    }
    if (this._view === "activity") {
      content.append(navigation);
      if (this._activityId) {
        const back = element("button", "Back to recent activity");
        back.type = "button";
        back.addEventListener("click", () => void this.showActivity());
        content.append(back);
      }
      if (this._status === "loading") content.append(element("p", "Loading activity…", "status"));
      else if (this._status !== "ready") content.append(element("p", this._status, "error"));
      else if (this._activityDetail) {
        const item = this._activityDetail;
        const heading = element("h2", item.source_title || item.title || "Import activity");
        heading.tabIndex = -1;
        content.append(heading, element("p", `Current status: ${activityLabel(item.status)}`),
          element("p", `Calendar created: ${item.created_count ?? 0} · Rejected: ${item.rejected_count ?? 0}`));
        if (item.guidance) content.append(element("p", item.guidance));
        const list = document.createElement("ul");
        for (const transition of item.transitions) {
          const row = element("li", `${activityTime(transition.at, this._hass)} · ${activityLabel(transition.type)}${transition.event_id ? ` · Event ID ${transition.event_id}` : ""}`);
          list.append(row);
        }
        content.append(list);
      } else if (this._activity.length === 0) content.append(element("p", "No recent activity.", "status"));
      else {
        const heading = element("h2", "Recent activity");
        heading.tabIndex = -1;
        content.append(heading);
        const list = document.createElement("ul");
        for (const item of this._activity) {
          const row = document.createElement("li");
          const open = element("button", item.source_title || item.title || "Import");
          open.type = "button";
          open.dataset.activityId = item.id;
          open.addEventListener("click", () => void this.showActivity(item.id));
          row.append(open, element("p", `${activityTime(item.created_at, this._hass)} · ${activityLabel(item.status)} · Calendar created: ${item.created_count ?? 0} · Rejected: ${item.rejected_count ?? 0}`));
          list.append(row);
        }
        content.append(list);
      }
      this._content.replaceChildren(content);
      this._announcement.replaceChildren(element("span", this._status === "loading" ? "Loading activity…" :
        this._status === "ready" ? "Activity loaded" : this._status));
      return;
    }
    if (this._batchResults.length) {
      const summary = element("section", "", "batch-results");
      summary.tabIndex = -1;
      summary.setAttribute("role", "status");
      summary.append(element("h2", `Bulk ${this._batchContext.action} results for ${this._batchContext.title}`));
      for (const result of this._batchResults) {
        summary.append(element("p", `${result.title} (${result.range}; event ID ${result.id}): ${result.outcome === "success" ? "Done" : result.outcome}`));
      }
      const dismiss = element("button", "Dismiss results");
      dismiss.type = "button";
      dismiss.addEventListener("click", () => {
        this._batchResults = [];
        this._batchContext = null;
        this.render();
        (this._content.querySelector("h2") || this._refreshButton).focus();
      });
      summary.append(dismiss);
      content.append(summary);
    }
    if (this._selectedId && !this._detail) {
      const back = element("button", "Back to inbox");
      back.type = "button";
      back.disabled = Boolean(this._editingId || this._decision || this._batchAction || this._resolution);
      back.addEventListener("click", () => this.showInbox());
      content.append(back);
    }
    if (this._status === "loading") {
      const loading = element("p", "Loading imports…", "status");
      content.append(loading);
    } else if (this._status !== "ready") {
      const error = element("p", this._status, "error");
      content.append(error);
    } else if (this._selectedId && this._detail) {
      const detail = this._detail;
      const back = element("button", "Back to inbox");
      back.type = "button";
      back.disabled = Boolean(this._editingId || this._decision || this._batchAction || this._resolution);
      back.addEventListener("click", () => this.showInbox());
      const heading = element("h2", detail.source_title || "Import detail");
      heading.tabIndex = -1;
      content.append(back, heading);
      if (this._decisionError) {
        const error = element("p", this._decisionError, "error");
        error.setAttribute("role", "alert");
        content.append(error);
      }
      content.append(element("p", `Source: ${detail.source_kind || "Text"}`));
      for (const warning of detail.warnings || []) content.append(element("p", `Warning: ${warning}`));
      if (detail.duplicate_events) content.append(element("p", `${detail.duplicate_events} duplicates skipped`));
      content.append(element("h2", "Source context"));
      if (detail.source_kind === "email") {
        const source = element("div", "", "source");
        source.tabIndex = 0;
        source.setAttribute("role", "region");
        source.setAttribute("aria-label", "Email source details");
        source.append(
          element("p", `Email title: ${detail.source_title || "No subject"}`),
          element("p", `Email sender: ${detail.source_sender || "Unknown sender"}`),
          element("p", `Received: ${activityTime(detail.created_at, this._hass)}`),
        );
        content.append(source);
      } else {
        const source = element("p", detail.source_text || "No source text available.", "source");
        source.tabIndex = 0;
        source.setAttribute("role", "region");
        source.setAttribute("aria-label", "Source text");
        content.append(source);
      }
      content.append(element("h2", "Events"));
      const ready = detail.events.filter(event => event.status === "pending");
      if (ready.length > 1 && !this._editingId && !this._decision && !this._resolution) {
        const bulk = element("div", "", "actions");
        if (this._batchAction) {
          bulk.append(element("p", `${this._batchAction === "approve" ? "Approve" : "Reject"} all ${ready.length} pending events? Each result will be shown separately.`));
          const confirm = element("button", `Confirm ${this._batchAction} all`);
          confirm.type = "button";
          confirm.addEventListener("click", () => void this.runBatch(this._batchAction));
          const cancel = element("button", "Cancel bulk review");
          cancel.type = "button";
          cancel.addEventListener("click", () => { if (this._saving) return;
            const action = this._batchAction;
            this._batchAction = null;
            this.render();
            Array.from(this._content.querySelectorAll("button"))
              .find(button => button.dataset.batchAction === action)?.focus();
          });
          bulk.append(confirm, cancel);
        } else {
          for (const action of ["approve", "reject"]) {
            const button = element("button", `${action === "approve" ? "Approve" : "Reject"} all ${ready.length}`);
            button.type = "button";
            button.dataset.batchAction = action;
            button.addEventListener("click", () => {
              if (this._saving) return;
              this._batchResults = [];
              this._batchContext = null;
              this._decisionError = null;
              this._batchAction = action;
              this.render();
              this._content.querySelector(".actions button")?.focus();
            });
            bulk.append(button);
          }
        }
        content.append(bulk);
      }
      for (const event of detail.events) {
        const card = element("section", "", "detail-event");
        if (this._editingId === event.id) {
          card.append(this.editForm(event));
          content.append(card);
          continue;
        }
        card.append(element("h3", event.title || "Untitled event"));
        card.append(element("p", `${eventRange(event, this._hass)}${event.all_day ? " · All day" : ""}`));
        card.append(element("p", `Calendar: ${event.calendar_entity || "Default"} · Status: ${event.status}`));
        if (typeof event.confidence === "number") {
          card.append(element("p", `AI extraction confidence: ${Math.round(event.confidence * 100)}% (estimate)`));
        }
        if (event.location) card.append(element("p", `Location: ${event.location}`));
        if (event.description) card.append(element("p", event.description));
        if (event.status === "write_uncertain") {
          card.append(element("p", "Calendar write could not be confirmed. Check the destination calendar before choosing an outcome. Retrying without checking may create a duplicate."));
          if (this._resolution?.id === event.id) {
            const actions = element("div", "", "actions");
            const labels = {created: "It was created", not_created: "It was not created — return to review",
              discard: "Discard without a calendar event"};
            card.append(element("p", `Confirm: ${labels[this._resolution.choice]}?`));
            const confirm = element("button", "Confirm recovery choice");
            confirm.type = "button";
            confirm.addEventListener("click", () => void this.runResolution(event, this._resolution.choice));
            const cancel = element("button", "Cancel recovery");
            cancel.type = "button";
            cancel.addEventListener("click", () => {
              if (this._saving) return;
              const choice = this._resolution.choice;
              this._resolution = null;
              this.render();
              Array.from(this._content.querySelectorAll("button"))
                .find(button => button.dataset.eventId === event.id && button.dataset.resolution === choice)?.focus();
            });
            actions.append(confirm, cancel);
            card.append(actions);
          } else if (!this._editingId && !this._decision && !this._batchAction && !this._resolution) {
            const choices = [{value: "created", label: "It was created"},
              {value: "not_created", label: "It was not created — return to review"},
              {value: "discard", label: "Discard without a calendar event"}];
            for (const choice of choices) {
              const button = element("button", choice.label);
              button.type = "button";
              button.dataset.eventId = event.id;
              button.dataset.resolution = choice.value;
              button.addEventListener("click", () => {
                this._decisionError = null;
                this._resolution = {id: event.id, choice: choice.value};
                this.render();
                this._content.querySelector(".actions button")?.focus();
              });
              card.append(button);
            }
          }
        }
        if (event.status === "pending" && !this._editingId && !this._decision && !this._batchAction && !this._resolution) {
          const edit = element("button", `Edit ${event.title}`);
          edit.type = "button";
          edit.dataset.eventId = event.id;
          edit.addEventListener("click", () => { if (this._saving || this._editingId) return;
            this._decisionError = null;
            this._editingId = event.id; this.render();
            this._content.querySelector("form input")?.focus(); });
          card.append(edit);
          for (const action of ["approve", "reject"]) {
            const button = element("button", `${action === "approve" ? "Approve" : "Reject"} ${event.title}`);
            button.type = "button";
            button.dataset.action = action;
            button.dataset.eventId = event.id;
            button.addEventListener("click", () => {
              if (this._saving || this._editingId || this._decision) return;
              this._decision = {id: event.id, action};
              this._decisionError = null;
              this.render();
              this._content.querySelector(".actions button")?.focus();
            });
            card.append(button);
          }
        } else if (this._decision?.id === event.id) {
          const action = this._decision.action;
          const actions = element("div", "", "actions");
          const confirm = element("button", `Confirm ${action}: ${event.title}`);
          confirm.type = "button";
          confirm.addEventListener("click", () => void this.runDecision(event, action));
          const cancel = element("button", "Cancel");
          cancel.type = "button";
          cancel.addEventListener("click", () => {
            if (this._saving) return;
            this._decision = null;
            this._decisionError = null;
            this.render();
            (Array.from(this._content.querySelectorAll("button"))
              .find(button => button.dataset.eventId === event.id &&
                button.dataset.action === action) || this._refreshButton).focus();
          });
          actions.append(confirm, cancel);
          card.append(actions);
        }
        content.append(card);
      }
    } else if (this._items.length === 0) {
      content.append(element("p", "No imports awaiting review.", "status"));
    } else {
      content.append(element("p", `${this._items.length} imports awaiting review`, "status"));
      const list = document.createElement("ul");
      for (const item of this._items) {
        const summary = summarizeImport(item, this._hass?.locale, this._hass?.config?.time_zone);
        const card = document.createElement("li");
        const open = element("button", summary.title);
        open.type = "button";
        open.dataset.pendingId = item.id;
        open.addEventListener("click", () => void this.showImport(item.id));
        const heading = document.createElement("h2");
        heading.append(open);
        card.append(heading);
        card.append(element("p", `${summary.type} · ${summary.created} · ${summary.events}`));
        const attention = [];
        if (summary.warnings) attention.push(`${summary.warnings} parser warnings`);
        if (summary.duplicates) attention.push(`${summary.duplicates} duplicates skipped`);
        if (summary.uncertain) attention.push("Calendar write needs confirmation");
        if (attention.length) card.append(element("p", attention.join(" · ")));
        list.append(card);
      }
      content.append(list);
    }
    content.append(navigation);
    this._content.replaceChildren(content);
    this._announcement.replaceChildren();
    if (this._status === "loading") this._announcement.append(element("span", "Loading imports…"));
    else if (this._status !== "ready") this._announcement.append(element("span", this._status));
    else this._announcement.append(element("span", this._selectedId ? "Import detail loaded" : "Inbox loaded"));
  }
}

customElements.define("daylight-import-panel", DaylightImportPanel);
