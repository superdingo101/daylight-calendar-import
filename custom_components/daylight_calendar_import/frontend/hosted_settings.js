/** Admin-only guided connection; bounded maintenance requires saved consent. */

const PREFIX = "daylight_calendar_import/hosted/";
const STATE_KEYS = ["entry_id", "configured", "automatic_lifecycle", "api_origin", "account_origin", "account_url", "state", "credential_status", "credential_expires_at"];
const ACTIONS = new Set(["connect", "reconnect", "poll", "reconcile", "rotate", "disconnect"]);
const SAFE_FAILURE = "The connection request could not be confirmed. Check status before making another change.";

function node(tag, text = "", className) {
  const result = document.createElement(tag);
  result.textContent = text;
  if (className) result.className = className;
  return result;
}

function closed(value, keys) {
  return value !== null && typeof value === "object" && !Array.isArray(value) &&
    Object.keys(value).length === keys.length && keys.every(key => Object.hasOwn(value, key));
}

export function trustedHostedOrigin(value) {
  if (typeof value !== "string" || value.length > 2048 || /[^\x21-\x7e]|[\\?#]/.test(value)) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || !url.hostname || url.username || url.password ||
        url.pathname !== "/" || url.search || url.hash || url.port === "0") return null;
    return url.origin;
  } catch {
    return null;
  }
}

function deadline(value) {
  return typeof value === "string" && value.length <= 128 &&
    /^\d{4}-\d\d-\d\d[Tt]\d\d:\d\d:\d\d(?:\.\d+)?(?:[Zz]|[+-]\d\d:\d\d)$/.test(value) &&
    Number.isFinite(Date.parse(value));
}

function trustedAccountLink(value, accountOrigin) {
  if (typeof value !== "string" || value.length > 2048 || /[^\x21-\x7e]|[\\?#]/.test(value)) return false;
  try {
    const url = new URL(value);
    return url.protocol === "https:" && !url.username && !url.password && !url.search && !url.hash &&
      url.origin === trustedHostedOrigin(accountOrigin);
  } catch {
    return false;
  }
}

export function validateHostedState(result, {entryId, pairing = false} = {}) {
  const hasPairing = result !== null && typeof result === "object" && Object.hasOwn(result, "pairing");
  const progressKeys = Object.hasOwn(result || {}, "reconnect_in_progress") ? ["reconnect_in_progress"] : [];
  const keys = hasPairing && pairing ? [...STATE_KEYS, ...progressKeys, "pairing"] : [...STATE_KEYS, ...progressKeys];
  if (!closed(result, keys) || typeof result.entry_id !== "string" ||
      result.entry_id.length < 1 || result.entry_id.length > 128 ||
      entryId !== undefined && result.entry_id !== entryId ||
      progressKeys.length > 0 && typeof result.reconnect_in_progress !== "boolean" ||
      typeof result.configured !== "boolean" || typeof result.automatic_lifecycle !== "boolean" ||
      ![null, "current", "overlap"].includes(result.credential_status) ||
      result.credential_expires_at !== null && !deadline(result.credential_expires_at) ||
      typeof result.state !== "string" || !/^[a-z][a-z0-9_]{0,63}$/.test(result.state)) {
    throw new Error("Unexpected Daylight connection response. Check status before retrying.");
  }
  const apiOrigin = trustedHostedOrigin(result.api_origin);
  const accountOrigin = trustedHostedOrigin(result.account_origin);
  if (result.configured ? !apiOrigin || !accountOrigin :
      result.api_origin !== null || result.account_origin !== null) {
    throw new Error("Unexpected Daylight service configuration.");
  }
  if (result.account_url !== null && !trustedAccountLink(result.account_url, accountOrigin)) {
    throw new Error("Unexpected Daylight account link.");
  }
  const safe = {...result, api_origin: apiOrigin, account_origin: accountOrigin};
  if (hasPairing) {
    const instructions = result.pairing;
    if (!closed(instructions, ["user_code", "verification_uri", "expires_at", "poll_interval_seconds"]) ||
        typeof instructions.user_code !== "string" || !/^[A-Z0-9]{12}$/.test(instructions.user_code) ||
        !trustedAccountLink(instructions.verification_uri, accountOrigin) || !deadline(instructions.expires_at) ||
        !Number.isInteger(instructions.poll_interval_seconds) ||
        instructions.poll_interval_seconds < 5 || instructions.poll_interval_seconds > 30) {
      throw new Error("Unexpected Daylight pairing instructions. Check status before retrying.");
    }
    safe.pairing = {...instructions};
  }
  return safe;
}

export async function loadHostedState(hass, entryId) {
  return validateHostedState(await hass.callWS({type: `${PREFIX}get`, ...(entryId ? {entry_id: entryId} : {})}), {entryId});
}

export async function configureHostedService(hass, entryId, apiOrigin, accountOrigin, automatic) {
  if (!trustedHostedOrigin(apiOrigin) || !trustedHostedOrigin(accountOrigin)) {
    throw new Error("Enter complete HTTPS service origins, without paths or credentials.");
  }
  // Omitted: the backend enables maintenance only for genuine first setup,
  // preserving a previous opt-out even when saved origins need repair.
  // Explicit true: the administrator chose the one-time renewal upgrade.
  if (automatic !== undefined && typeof automatic !== "boolean") {
    throw new Error("Unexpected Daylight maintenance setting.");
  }
  return validateHostedState(await hass.callWS({
    type: `${PREFIX}configure`, entry_id: entryId, api_origin: apiOrigin, account_origin: accountOrigin,
    ...(automatic === undefined ? {} : {automatic_lifecycle: automatic}),
  }), {entryId});
}

export async function runHostedAction(hass, entryId, action, input = {}) {
  if (!ACTIONS.has(action)) throw new Error("Unsupported Daylight connection action.");
  const request = {type: `${PREFIX}action`, entry_id: entryId, action};
  if (["connect", "reconnect"].includes(action)) {
    if (input.processing_consent !== true || typeof input.installation_label !== "string" ||
        input.installation_label.length < 1 || input.installation_label.length > 128) {
      throw new Error("Confirm cloud processing consent and enter an installation name.");
    }
    request.processing_consent = true;
    request.installation_label = input.installation_label;
  }
  if (action === "reconnect") {
    if (input.confirm_reconnect !== true) throw new Error("Confirm owner-approved reconnect first.");
    request.confirm_reconnect = true;
  }
  if (action === "disconnect") {
    if (input.confirm_disconnect !== true) throw new Error("Confirm local stop and disconnection first.");
    request.confirm_disconnect = true;
  }
  return validateHostedState(await hass.callWS(request), {entryId, pairing: ["connect", "reconnect"].includes(action)});
}

const EXPLANATIONS = {
  not_configured: "Configure the trusted Daylight service before connecting.",
  first_connection_required: "Connect Home Assistant to your Daylight account.",
  configuration_repair: "The saved service configuration needs repair. Check both HTTPS origins.",
  storage_repair: "Private connection storage needs repair. Preserve the original file and repair storage before continuing.",
  service_repair: "The saved connection belongs to a different or unknown service. Owner and service reconciliation is required; changing origins does not move credentials.",
  service_unavailable: "Daylight could not be reached. Check status when the service is available.",
  operation_finishing: "A previous operation is still finishing. Use Check and recover connection after it settles.",
  reconnect_pending: "A fresh owner-approved reconnect is in progress. Approve the new attempt in the account portal, then return here to finish automatically. Previous credentials and local stop remain preserved until verification.",
  recovery_required: "Verifying your saved connection…",
  repair_required: "This installation needs owner attention. Open the account portal and reconcile the existing installation.",
  reconciliation_required: "An existing installation needs explicit owner and journal reconciliation before a new connection.",
  remote_disconnect_unconfirmed: "Hosted work is stopped locally. Remote disconnection is unconfirmed; reconcile it in the account portal.",
  disconnected: "Connect Home Assistant to your Daylight account.",
  connected: "Your Home Assistant is securely connected to Daylight.",
  previous_retained: "The previous credential remains available while the saved replacement needs recovery.",
  candidate_saved: "Approval received. Finishing your secure connection…",
  pairing_pending: "Approve this connection in your Daylight account. This page will finish connecting automatically.",
  pairing_wait: "Waiting for your approval. We will check again automatically.",
  pairing_retry: "Approval checking was interrupted. This approval attempt is still live; automatic checks will resume after the retry delay.",
  fresh_pairing_required: "This approval attempt can no longer finish. Open Manage connection and choose Reconnect for a fresh code and owner approval.",
  rotation_wait: "A previous renewal may still be pending. Wait before issuing another renewal, then check status.",
  automatic_attention_required: "Automatic maintenance needs attention. Approval may have expired, an exchange or renewal may be uncertain, or retries reached a safe limit. Check and recover this connection, then use the owner account portal if reconciliation is required.",
  consent_required: "Cloud processing requires your explicit consent.",
};

function checkbox(name, text) {
  const label = node("label", "", "hosted-checkbox");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.name = name;
  input.checked = false;
  label.append(input, node("span", text));
  return {label, input};
}

export class HostedConnectionSettings {
  constructor({getHass, onBusy = () => {}, schedule = (callback, delay) => setTimeout(callback, delay), cancel = timer => clearTimeout(timer)}) {
    this.getHass = getHass;
    this.schedule = schedule;
    this.cancel = cancel;
    this.timer = null;
    this.guidedSteps = 0;
    this.advancedOpen = false;
    this.confirmation = null;
    this.managementOpen = false;
    this.diagnosticsOpen = false;
    this.guidedReconnect = false;
    this.onBusy = onBusy;
    this.element = node("section", "", "settings-card");
    this.generation = 0;
    this.entryId = null;
    this.loaded = false;
    this.busy = false;
    this.notifyBusy = false;
    this.snapshot = null;
    this.pairing = null;
    this.error = null;
    this.session = null;
    this.connection = null;
    this.connectionInvalidated = false;
    this.subscription = null;
    this.subscriptionToken = 0;
    this.refreshSequence = 0;
    this.reconnectEntry = null;
    this.onDisconnected = () => {
      const entryId = this.entryId || this.reconnectEntry;
      this.connectionInvalidated = true;
      this.clear();
      this.reconnectEntry = entryId;
    };
    this.onReady = () => {
      this.connectionInvalidated = false;
      const entryId = this.reconnectEntry;
      this.reconnectEntry = null;
      if (entryId) void this.load(entryId);
    };
    this.render();
  }

  _watchConnection(connection) {
    if (connection === this.connection) return;
    this.connection?.removeEventListener?.("disconnected", this.onDisconnected);
    this.connection?.removeEventListener?.("ready", this.onReady);
    this.connection = connection;
    this.connectionInvalidated = false;
    connection?.addEventListener?.("disconnected", this.onDisconnected);
    connection?.addEventListener?.("ready", this.onReady);
  }

  stop() {
    this.clear();
    this._watchConnection(null);
    this.session = null;
  }

  syncSession() {
    const hass = this.getHass();
    const next = {id: hass?.user?.id, admin: hass?.user?.is_admin === true, connection: hass?.connection};
    this._watchConnection(next.connection);
    const changed = this.session && (next.id !== this.session.id ||
      next.admin !== this.session.admin || next.connection !== this.session.connection);
    if (!next.admin || changed) this.clear();
    else if (next.connection?.connected === false) this.clear({preserveReconnect: true});
    this.session = next;
    return next.admin && next.connection?.connected !== false && !this.connectionInvalidated;
  }

  clear({preserveReconnect = false} = {}) {
    if (!preserveReconnect) this.reconnectEntry = null;
    this._unsubscribe();
    this._cancelGuidance();
    this.guidedSteps = 0;
    this.advancedOpen = false;
    this.confirmation = null;
    this.managementOpen = false;
    this.diagnosticsOpen = false;
    this.guidedReconnect = false;
    this.generation++;
    this.entryId = null;
    this.loaded = false;
    this.snapshot = null;
    this.pairing = null;
    this.error = null;
    const wasBusy = this.busy;
    this.busy = false;
    if (wasBusy && this.notifyBusy) this.onBusy(false);
    this.notifyBusy = false;
    this.render();
  }

  async load(entryId) {
    if (!this.syncSession() || this.busy) return;
    if (entryId !== this.entryId) this.clear();
    this.entryId = entryId;
    this.loaded = true;
    this.pairing = null;
    const generation = ++this.generation;
    this.error = null;
    this.render();
    try {
      const result = await loadHostedState(this.getHass(), entryId);
      if (!this.syncSession() || generation !== this.generation) return;
      this.snapshot = result;
      this._subscribe();
    } catch {
      if (!this.syncSession() || generation !== this.generation) return;
      this.snapshot = null;
      this.error = "Could not load the Daylight connection. Refresh to check its current state.";
    }
    if (generation === this.generation) { this.render(); this._guide(); }
  }

  async submit(action, input = {}, {preserveDraft = false} = {}) {
    if (!this.syncSession() || this.busy || !this.snapshot) return;
    const draft = preserveDraft ? this._captureDraft() : null;
    const previousState = this.snapshot.state;
    if (!preserveDraft) this.confirmation = null;
    this._cancelGuidance();
    if (["connect", "reconnect"].includes(action)) {
      this.guidedSteps = 0;
      this.guidedReconnect = action === "reconnect";
    }
    const generation = ++this.generation;
    const entryId = this.entryId;
    this.busy = true;
    this.notifyBusy = !preserveDraft;
    this.error = null;
    // Codes and links belong only to the latest explicit connection attempt.
    const previousPairing = action === "poll" ? this.pairing : null;
    this.pairing = null;
    if (!preserveDraft) this.onBusy(true);
    this.render();
    if (draft) this._restoreDraft(draft);
    try {
      const result = action === "configure" ?
        await configureHostedService(this.getHass(), entryId, input.api_origin, input.account_origin,
          input.enable_automatic === true ? true : undefined) :
        await runHostedAction(this.getHass(), entryId, action, input);
      if (!this.syncSession() || generation !== this.generation) return;
      const {pairing, ...snapshot} = result;
      this.snapshot = snapshot;
      const pending = ["pairing_pending", "pairing_wait", "pairing_retry", "reconnect_pending"];
      if (preserveDraft && this.confirmation && snapshot.state !== previousState &&
          !(pending.includes(previousState) && pending.includes(snapshot.state))) this.confirmation = null;
      if (["connect", "reconnect"].includes(action) && pairing) this.pairing = pairing;
      else if (action === "poll" && ["pairing_pending", "pairing_wait", "pairing_retry", "reconnect_pending"].includes(snapshot.state)) this.pairing = previousPairing;
    } catch {
      if (!this.syncSession() || generation !== this.generation) return;
      this.error = SAFE_FAILURE;
    } finally {
      if (generation === this.generation) {
        this.busy = false;
        if (!preserveDraft) this.onBusy(false);
        this.notifyBusy = false;
        this._subscribe();
        this.render();
        if (draft) this._restoreDraft(draft);
        this.element.querySelector("[role=alert]")?.focus();
        this._guide();
      }
    }
  }

  _cancelGuidance() {
    if (this.timer !== null) this.cancel(this.timer);
    this.timer = null;
  }

  _guide() {
    this._cancelGuidance();
    // Page-scoped assistance never issues a new pairing, rotation or disconnect.
    // The server remains authoritative for polling deadlines and uncertain exchanges.
    if (this.error || this.busy || !this.snapshot) return;
    const state = this.snapshot.state;
    if (typeof this.snapshot.reconnect_in_progress === "boolean") this.guidedReconnect = this.snapshot.reconnect_in_progress;
    else if (state === "reconnect_pending") this.guidedReconnect = true;
    if (this.snapshot.automatic_lifecycle && !this.guidedReconnect) return;
    const polling = ["pairing_pending", "pairing_wait", "pairing_retry", "reconnect_pending"].includes(state);
    const verifying = ["candidate_saved", "recovery_required"].includes(state);
    if (!polling && !verifying) return;
    if (this.guidedSteps >= 124) {
      this.error = "Automatic checks paused. Open Advanced settings & troubleshooting to check and recover the connection.";
      this.render();
      return;
    }
    if (this.pairing && Date.parse(this.pairing.expires_at) <= Date.now()) {
      // Let the controller classify expiry (including approval already exchanged).
      this.pairing = null;
    }
    const generation = this.generation;
    this.timer = this.schedule(() => {
      this.timer = null;
      if (!this.syncSession() || generation !== this.generation) return;
      this.guidedSteps++;
      void this.submit(polling ? "poll" : "reconcile", {}, {preserveDraft: true});
    }, polling ? (this.pairing?.poll_interval_seconds || 30) * 1000 : 1000);
    this.timer?.unref?.();
  }

  _unsubscribe() {
    this.subscriptionToken++;
    this.subscription?.unsubscribe?.();
    this.subscription = null;
  }

  _subscribe() {
    const connection = this.getHass()?.connection;
    if (!this.snapshot?.automatic_lifecycle || !connection?.subscribeMessage) {
      this._unsubscribe();
      return;
    }
    if (this.subscription?.connection === connection && this.subscription.entryId === this.entryId) return;
    this._unsubscribe();
    const token = this.subscriptionToken;
    const entryId = this.entryId;
    this.subscription = {connection, entryId};
    // A late subscription must be closed after navigation or session changes.
    Promise.resolve(connection.subscribeMessage(event => {
      if (token === this.subscriptionToken && closed(event, ["entry_id"]) && event.entry_id === entryId) {
        void this._refreshAutomatic();
      }
    }, {type: `${PREFIX}subscribe`, entry_id: entryId})).then(unsubscribe => {
      if (token !== this.subscriptionToken) unsubscribe();
      else this.subscription.unsubscribe = unsubscribe;
    }).catch(() => {
      if (token === this.subscriptionToken) this.subscription = null;
    });
  }

  async _refreshAutomatic() {
    if (!this.syncSession() || this.busy || !this.snapshot) return;
    const generation = this.generation;
    const sequence = ++this.refreshSequence;
    const entryId = this.entryId;
    try {
      const result = await loadHostedState(this.getHass(), entryId);
      if (!this.syncSession() || this.busy || generation !== this.generation || sequence !== this.refreshSequence) return;
      const previousPairing = this.pairing;
      if (!["pairing_pending", "pairing_wait", "pairing_retry", "reconnect_pending"].includes(result.state) ||
          this.pairing && Date.parse(this.pairing.expires_at) <= Date.now() ||
          result.api_origin !== this.snapshot.api_origin || result.account_origin !== this.snapshot.account_origin) this.pairing = null;
      const unchanged = JSON.stringify(result) === JSON.stringify(this.snapshot) && previousPairing === this.pairing;
      const draft = this._captureDraft();
      this.snapshot = result;
      this._subscribe();
      if (!unchanged || this.timer === null) this._guide();
      if (!unchanged) {
        this.render();
        this._restoreDraft(draft);
      }
    } catch {
      // Keep explicit recovery controls available after a dropped invalidation.
    }
  }

  _captureDraft() {
    // Status invalidations must not discard a user's in-progress configuration.
    const inputs = this.element.querySelectorAll("input");
    const values = [];
    let focus = null;
    const activeElement = this.element.getRootNode?.()?.activeElement || document.activeElement;
    for (const input of inputs) {
      const saved = input.name === "automatic_lifecycle" ? this.snapshot.automatic_lifecycle :
        ["api_origin", "account_origin"].includes(input.name) ? this.snapshot[input.name] || "" :
        input.type === "checkbox" ? false : "Home Assistant";
      const value = input.type === "checkbox" ? input.checked : input.value;
      if (value !== saved) values.push({name: input.name, value});
      if (input === activeElement) focus = {name: input.name, start: input.selectionStart, end: input.selectionEnd};
    }
    return {values, focus};
  }

  _restoreDraft({values, focus}) {
    for (const {name, value} of values) {
      const input = Array.from(this.element.querySelectorAll("input")).find(node => node.name === name);
      if (!input) continue;
      if (input.type === "checkbox") input.checked = value;
      else input.value = value;
      input.dispatchEvent?.(new Event("change"));
    }
    const input = focus && Array.from(this.element.querySelectorAll("input")).find(node => node.name === focus.name);
    if (input) {
      input.focus();
      if (["text", "url"].includes(input.type) && focus.start !== null) {
        // URL fields do not support selection ranges in every browser.
        try { input.setSelectionRange?.(focus.start, focus.end); } catch { /* Keep focus. */ }
      }
    }
  }

  _renderPreservingDraft() {
    // Confirmation-only rerenders must not discard unsaved service origins,
    // replacement labels or keyboard focus. No network action is performed.
    const draft = this.snapshot ? this._captureDraft() : null;
    this.render();
    if (draft) this._restoreDraft(draft);
  }

  render() {
    const section = this.element;
    const title = node("h2", "Daylight connection");
    title.tabIndex = -1;
    section.replaceChildren(title);
    if (!this.getHass()?.user?.is_admin) {
      section.append(node("p", "An administrator must manage the Daylight connection."));
      return;
    }
    if (this.error) {
      const error = node("p", this.error, "error");
      error.setAttribute("role", "alert");
      error.tabIndex = -1;
      section.append(error);
    }
    if (!this.snapshot) {
      section.append(node("p", this.error ? "Connection status is unavailable. Refresh to check again." :
        this.loaded ? "Loading connection…" : "Open this section to load connection status."));
      return;
    }
    const state = this.snapshot;
    const status = node("p", EXPLANATIONS[state.state] || "Check connection status before continuing.");
    status.setAttribute("role", "status");
    section.append(status);
    if (state.credential_expires_at) section.append(node("p", `Connection verified · Credential expires ${new Date(state.credential_expires_at).toLocaleDateString(undefined, {year: "numeric", month: "long", day: "numeric"})}`));
    section.append(node("p", "Cloud processing and hosted email are not yet available in this alpha.", "settings-help"));
    const advanced = node("details");
    advanced.open = this.advancedOpen;
    advanced.append(node("summary", "Advanced settings & troubleshooting"));
    advanced.addEventListener("toggle", () => { this.advancedOpen = advanced.open; });
    section.append(advanced);
    const management = node("details");
    management.open = this.managementOpen || this.confirmation !== null;
    management.append(node("summary", "Manage connection"));
    management.addEventListener("toggle", () => { this.managementOpen = management.open; });
    const diagnostics = node("details");
    diagnostics.open = this.diagnosticsOpen;
    diagnostics.append(node("summary", "Troubleshooting & developer settings"));
    diagnostics.addEventListener("toggle", () => { this.diagnosticsOpen = diagnostics.open; });
    if (state.configured) advanced.append(management);
    advanced.append(diagnostics);
    if (state.credential_status === "overlap") section.append(node("p", "This is an overlap credential. Ordinary access may remain available until expiry; lifecycle changes need owner repair."));
    if (this.busy) section.append(node("p", "Connection operation in progress…"));

    const configuration = document.createElement("form");
    configuration.setAttribute("data-hosted-configure", "");
    configuration.append(node("h3", "Trusted service"), node("p", "Use the HTTPS API and account origins supplied for your Daylight environment. Changing these does not transfer an existing connection.", "settings-help"));
    for (const [name, labelText] of [["api_origin", "API origin"], ["account_origin", "Account origin"]]) {
      const label = node("label", labelText);
      const input = document.createElement("input");
      input.type = "url";
      input.name = name;
      input.required = true;
      input.maxLength = 2048;
      input.value = state[name] || "";
      input.disabled = this.busy;
      label.append(input);
      configuration.append(label);
    }
    const save = node("button", "Save service configuration");
    save.type = "submit";
    save.disabled = this.busy;
    configuration.append(save);
    configuration.addEventListener("submit", event => {
      event.preventDefault();
      void this.submit("configure", {
        api_origin: configuration.elements.namedItem("api_origin").value,
        account_origin: configuration.elements.namedItem("account_origin").value,
      });
    });
    diagnostics.append(configuration);
    if (!state.configured) {
      advanced.open = true;
      diagnostics.open = true;
    }
    if (!state.configured) return;

    // The former default was opt-out. Do not silently change an existing
    // credential's persisted policy: offer a one-time upgrade instead.
    if (!state.automatic_lifecycle && state.credential_status === "current" && state.state === "connected") {
      const migrate = node("div", "", "hosted-maintenance-upgrade");
      migrate.append(node("p", "Automatic credential renewal is not enabled for this earlier connection."));
      const enable = node("button", "Enable automatic renewals");
      enable.type = "button";
      enable.disabled = this.busy;
      enable.addEventListener("click", () => void this.submit("configure", {
        api_origin: state.api_origin, account_origin: state.account_origin, enable_automatic: true,
      }));
      migrate.append(enable);
      section.append(migrate);
    }

    if (state.account_url) {
      const accountLink = node("a", "Open account portal");
      accountLink.href = state.account_url;
      accountLink.target = "_blank";
      accountLink.rel = "noopener noreferrer";
      accountLink.referrerPolicy = "no-referrer";
      accountLink.className = "hosted-account-link";
      section.append(accountLink);
    } else section.append(node("p", "Use the owner account portal for installation reconciliation. Pairing instructions provide the approval link when starting a connection.", "settings-help"));
    if (!this.pairing && ["pairing_pending", "pairing_wait", "pairing_retry", "reconnect_pending"].includes(state.state)) {
      section.append(node("p", "Already approved? Keep this page open to finish automatically. If you still need to approve, use the code you saved privately. Codes are not restored after leaving this page; if yours is unavailable, wait for this attempt to expire, then reconnect for a fresh code.", "settings-help"));
    }
    if (this.pairing) {
      const instructions = node("div");
      instructions.setAttribute("data-hosted-pairing", "");
      instructions.append(node("p", `Approval code: ${this.pairing.user_code}`));
      const approve = node("a", "Approve this connection");
      approve.href = this.pairing.verification_uri;
      approve.target = "_blank";
      approve.rel = "noopener noreferrer";
      approve.referrerPolicy = "no-referrer";
      instructions.append(node("h3", "Approve your connection"), approve, node("p", "Enter this connection code in your account, then verify with the separate code sent to your email. Return here; we will finish connecting automatically."), node("p", `Code expires ${new Date(this.pairing.expires_at).toLocaleTimeString(undefined, {hour: "numeric", minute: "2-digit"})}.`));
      section.append(instructions);
    }
    const blocked = ["configuration_repair", "storage_repair", "service_repair"].includes(state.state);
    const actions = node("div", "", "actions");
    const neededActions = [
      ...(["recovery_required", "repair_required", "reconciliation_required", "operation_finishing", "service_unavailable", "automatic_attention_required", "previous_retained", "candidate_saved"].includes(state.state)
        ? [["reconcile", "Check and recover connection"]] : []),
      ...(["pairing_pending", "pairing_wait", "pairing_retry", "reconnect_pending"].includes(state.state)
        ? [["poll", "Check pairing approval"]] : []),
      ...(["automatic_attention_required", "rotation_wait"].includes(state.state) && state.credential_status === "current"
        ? [["rotate", "Renew credentials"]] : []),
    ];
    for (const [action, label] of neededActions) {
      const button = node("button", label);
      button.type = "button";
      button.disabled = this.busy || blocked || state.state === "operation_finishing" && action !== "reconcile" || action === "rotate" &&
        !(["connected", "automatic_attention_required"].includes(state.state) && state.credential_status === "current") ||
        action === "poll" && !["pairing_pending", "pairing_wait", "pairing_retry", "reconnect_pending", "recovery_required"].includes(state.state);
      button.addEventListener("click", () => void this.submit(action));
      actions.append(button);
    }
    if (neededActions.length) diagnostics.append(actions);
    if (["disconnected", "first_connection_required", "fresh_pairing_required", "consent_required"].includes(state.state) ||
        ["pairing_pending", "pairing_wait", "pairing_retry"].includes(state.state) && this.pairing === null) {
      const connection = document.createElement("form");
      connection.setAttribute("data-hosted-connect", "");
      const label = node("label", "Installation name");
      const input = document.createElement("input");
      input.name = "installation_label";
      input.maxLength = 128;
      input.required = true;
      input.value = "Home Assistant";
      input.disabled = this.busy;
      label.append(input);
      const consent = checkbox("processing_consent", "I agree to send selected source content to Daylight for cloud processing.");
      consent.input.disabled = this.busy;
      const connect = node("button", "Start new connection");
      connect.type = "submit";
      connect.disabled = true;
      consent.input.addEventListener("change", () => { connect.disabled = this.busy || !consent.input.checked; });
      connection.append(label, consent.label, connect);
      connection.addEventListener("submit", event => {
        event.preventDefault();
        if (consent.input.checked !== true) return;
        void this.submit("connect", {processing_consent: true, installation_label: input.value});
      });
      section.append(connection);
    }
    // Owner approval is still required on the portal. A focused, deliberate
    // confirmation supplies the API booleans without a permanent consent form.
    if (state.state !== "first_connection_required" && state.state !== "not_configured") {
      const reconnect = document.createElement("form");
      reconnect.setAttribute("data-hosted-reconnect", "");
      reconnect.append(node("h3", "Reconnect Home Assistant"),
        node("p", "Repair or replace this connection using your Daylight account. Local imports and calendar events remain on this device.", "settings-help"));
      const label = node("label", "New installation name");
      const name = document.createElement("input");
      name.name = "reconnect_label";
      name.value = "Home Assistant";
      name.required = true;
      name.maxLength = 128;
      name.disabled = this.busy || blocked || this.confirmation?.kind === "reconnect";
      label.append(name);
      const start = node("button", "Reconnect");
      start.type = "button";
      start.disabled = this.busy || blocked || state.state === "operation_finishing" || this.confirmation?.kind === "reconnect";
      const showConfirmation = () => {
        if (!name.value.trim() || name.value.length > 128) return;
        this.confirmation = {kind: "reconnect", label: name.value};
        this.managementOpen = true;
        this._renderPreservingDraft();
      };
      start.addEventListener("click", showConfirmation);
      reconnect.addEventListener("submit", event => {
        event.preventDefault();
        showConfirmation();
      });
      reconnect.append(label, start);
      management.append(reconnect);
    }

    if (!["disconnected", "first_connection_required", "not_configured"].includes(state.state)) {
      const disconnect = node("div");
      disconnect.setAttribute("data-hosted-disconnect", "");
      disconnect.append(node("h3", "Disconnect Home Assistant"),
        node("p", "Stop this installation's cloud connection. Local imports and calendars remain unchanged.", "settings-help"));
      const stop = node("button", "Disconnect");
      stop.type = "button";
      stop.disabled = this.busy;
      stop.addEventListener("click", () => {
        this.confirmation = {kind: "disconnect"};
        this.managementOpen = true;
        this._renderPreservingDraft();
      });
      disconnect.append(stop);
      management.append(disconnect);
    }

    if (this.confirmation && ["reconnect", "disconnect"].includes(this.confirmation.kind)) {
      const reconnection = this.confirmation.kind === "reconnect";
      const confirmation = node("div", "", "hosted-confirmation");
      confirmation.setAttribute("data-hosted-confirmation", this.confirmation.kind);
      confirmation.setAttribute("role", "group");
      confirmation.setAttribute("aria-label", reconnection ? "Confirm reconnection" : "Confirm disconnection");
      confirmation.append(node("h3", reconnection ? "Reconnect Home Assistant?" : "Disconnect Home Assistant?"));
      confirmation.append(node("p", reconnection
        ? "You agree to let Daylight Cloud process selected source content. Approve this new installation through your Daylight account. It may replace the current connection; your local imports and calendar events are kept."
        : "This will stop hosted work and request revocation of this installation's cloud credentials. Local imports and calendar events are kept."));
      if (reconnection) confirmation.append(node("p", `Installation name: ${this.confirmation.label}`));
      const buttons = node("div", "", "actions");
      const cancel = node("button", "Cancel");
      cancel.type = "button";
      cancel.addEventListener("click", () => { this.confirmation = null; this._renderPreservingDraft(); });
      const confirm = node("button", reconnection ? "Agree & reconnect" : "Confirm disconnect");
      confirm.type = "button";
      confirm.disabled = this.busy;
      confirm.addEventListener("click", () => {
        if (!this.confirmation) return;
        if (reconnection) {
          const label = this.confirmation.label;
          if (!label || label.length > 128) return;
          void this.submit("reconnect", {processing_consent: true, installation_label: label, confirm_reconnect: true});
        } else {
          void this.submit("disconnect", {confirm_disconnect: true});
        }
      });
      buttons.append(cancel, confirm);
      confirmation.append(buttons);
      management.append(confirmation);
    }
  }
}
