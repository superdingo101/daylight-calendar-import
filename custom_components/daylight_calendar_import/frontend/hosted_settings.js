/** Explicit, admin-only Daylight connection controls. Background work is owned by the opted-in entry runtime. */

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
  const keys = hasPairing && pairing ? [...STATE_KEYS, "pairing"] : STATE_KEYS;
  if (!closed(result, keys) || typeof result.entry_id !== "string" ||
      result.entry_id.length < 1 || result.entry_id.length > 128 ||
      entryId !== undefined && result.entry_id !== entryId ||
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

export async function configureHostedService(hass, entryId, apiOrigin, accountOrigin, automatic = false) {
  if (!trustedHostedOrigin(apiOrigin) || !trustedHostedOrigin(accountOrigin)) {
    throw new Error("Enter complete HTTPS service origins, without paths or credentials.");
  }
  return validateHostedState(await hass.callWS({
    type: `${PREFIX}configure`, entry_id: entryId, api_origin: apiOrigin, account_origin: accountOrigin, automatic_lifecycle: automatic,
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
  first_connection_required: "This installation has no saved connection yet. Use Start new connection below to create its first owner-approved connection.",
  configuration_repair: "The saved service configuration needs repair. Check both HTTPS origins.",
  storage_repair: "Private connection storage needs repair. Preserve the original file and repair storage before continuing.",
  service_repair: "The saved connection belongs to a different or unknown service. Owner and service reconciliation is required; changing origins does not move credentials.",
  service_unavailable: "Daylight could not be reached. Check status when the service is available.",
  operation_finishing: "A previous operation is still finishing. Use Check and recover connection after it settles.",
  reconnect_pending: "A fresh owner-approved reconnect is in progress. Approve the new attempt in the account portal, then check approval and recover it explicitly. Previous credentials and local stop remain preserved until verification.",
  recovery_required: "Saved connection material needs verification. Check and recover it before connecting again.",
  repair_required: "This installation needs owner attention. Open the account portal and reconcile the existing installation.",
  reconciliation_required: "An existing installation needs explicit owner and journal reconciliation before a new connection.",
  remote_disconnect_unconfirmed: "Hosted work is stopped locally. Remote disconnection is unconfirmed; reconcile it in the account portal.",
  disconnected: "This installation is not connected. A previous disconnect may still require owner reconciliation before a new connection.",
  connected: "The saved installation credential has been verified.",
  previous_retained: "The previous credential remains available while the saved replacement needs recovery.",
  candidate_saved: "The new credential was saved privately. Check and recover it to finish verification.",
  pairing_pending: "Waiting for approval in the account portal. Check approval after the advertised interval.",
  pairing_wait: "Wait for the pairing interval before checking approval again.",
  pairing_retry: "Approval checking was interrupted. This approval attempt is still live; automatic checks will resume after the retry delay.",
  fresh_pairing_required: "A fresh pairing and owner approval are required. Start a new connection deliberately.",
  rotation_wait: "A previous renewal may still be pending. Wait before issuing another renewal, then check status.",
  automatic_attention_required: "Automatic maintenance needs attention. Approval may have expired, an exchange or renewal may be uncertain, or retries reached a safe limit. Check and recover this connection, then use the owner account portal if reconciliation is required.",
  consent_required: "Cloud processing requires your explicit consent.",
};

function checkbox(name, text) {
  const label = node("label");
  const input = document.createElement("input");
  input.type = "checkbox";
  input.name = name;
  input.checked = false;
  label.append(input, node("span", text));
  return {label, input};
}

export class HostedConnectionSettings {
  constructor({getHass, onBusy = () => {}}) {
    this.getHass = getHass;
    this.onBusy = onBusy;
    this.element = node("section", "", "settings-card");
    this.generation = 0;
    this.entryId = null;
    this.loaded = false;
    this.busy = false;
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
    this.generation++;
    this.entryId = null;
    this.loaded = false;
    this.snapshot = null;
    this.pairing = null;
    this.error = null;
    const wasBusy = this.busy;
    this.busy = false;
    if (wasBusy) this.onBusy(false);
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
    if (generation === this.generation) this.render();
  }

  async submit(action, input = {}) {
    if (!this.syncSession() || this.busy || !this.snapshot) return;
    const generation = ++this.generation;
    const entryId = this.entryId;
    this.busy = true;
    this.error = null;
    // Codes and links belong only to the latest explicit connection attempt.
    const previousPairing = action === "poll" ? this.pairing : null;
    this.pairing = null;
    this.onBusy(true);
    this.render();
    try {
      const result = action === "configure" ?
        await configureHostedService(this.getHass(), entryId, input.api_origin, input.account_origin, input.automatic_lifecycle === true) :
        await runHostedAction(this.getHass(), entryId, action, input);
      if (!this.syncSession() || generation !== this.generation) return;
      const {pairing, ...snapshot} = result;
      this.snapshot = snapshot;
      if (["connect", "reconnect"].includes(action) && pairing) this.pairing = pairing;
      else if (action === "poll" && ["pairing_pending", "pairing_wait", "pairing_retry", "reconnect_pending"].includes(snapshot.state)) this.pairing = previousPairing;
    } catch {
      if (!this.syncSession() || generation !== this.generation) return;
      this.error = SAFE_FAILURE;
    } finally {
      if (generation === this.generation) {
        this.busy = false;
        this.onBusy(false);
        this._subscribe();
        this.render();
        this.element.querySelector("[role=alert]")?.focus();
      }
    }
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
    if (state.credential_expires_at) section.append(node("p", `Verified credential expiry: ${state.credential_expires_at}`));
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
    const automatic = checkbox("automatic_lifecycle", "Automatically maintain this connection");
    automatic.input.checked = state.automatic_lifecycle;
    automatic.input.disabled = this.busy;
    configuration.append(automatic.label, node("p", "After you grant connection consent, automatically check approval, recover saved credentials and renew before expiry using bounded retries. This does not enable source delivery or cloud parsing.", "settings-help"));
    const save = node("button", "Save service configuration");
    save.type = "submit";
    save.disabled = this.busy;
    configuration.append(save);
    configuration.addEventListener("submit", event => {
      event.preventDefault();
      void this.submit("configure", {
        api_origin: configuration.elements.namedItem("api_origin").value,
        account_origin: configuration.elements.namedItem("account_origin").value,
        automatic_lifecycle: automatic.input.checked,
      });
    });
    section.append(configuration);
    if (!state.configured) return;

    if (state.account_url) {
      const accountLink = node("a", "Open account portal");
      accountLink.href = state.account_url;
      accountLink.target = "_blank";
      accountLink.rel = "noopener noreferrer";
      accountLink.referrerPolicy = "no-referrer";
      section.append(accountLink);
    } else section.append(node("p", "Use the owner account portal for installation reconciliation. Pairing instructions provide the approval link when starting a connection.", "settings-help"));
    if (this.pairing) {
      const instructions = node("div");
      instructions.setAttribute("data-hosted-pairing", "");
      instructions.append(node("p", `Approval code: ${this.pairing.user_code}`));
      const approve = node("a", "Approve this connection");
      approve.href = this.pairing.verification_uri;
      approve.target = "_blank";
      approve.rel = "noopener noreferrer";
      approve.referrerPolicy = "no-referrer";
      instructions.append(approve, node("p", `Expires: ${this.pairing.expires_at}. Check no more often than every ${this.pairing.poll_interval_seconds} seconds.`));
      section.append(instructions);
    }
    const blocked = ["configuration_repair", "storage_repair", "service_repair"].includes(state.state);
    const actions = node("div", "", "actions");
    for (const [action, label] of [["reconcile", "Check and recover connection"], ["poll", "Check pairing approval"], ["rotate", "Renew credentials"]]) {
      const button = node("button", label);
      button.type = "button";
      button.disabled = this.busy || blocked || state.state === "operation_finishing" && action !== "reconcile" || action === "rotate" &&
        !(["connected", "automatic_attention_required"].includes(state.state) && state.credential_status === "current") ||
        action === "poll" && !["pairing_pending", "pairing_wait", "pairing_retry", "reconnect_pending", "recovery_required"].includes(state.state);
      button.addEventListener("click", () => void this.submit(action));
      actions.append(button);
    }
    section.append(actions);
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
    if (state.state !== "first_connection_required") {
      const reconnect = document.createElement("form");
      reconnect.setAttribute("data-hosted-reconnect", "");
      reconnect.append(node("h3", "Reconnect with owner approval"), node("p", "Use a fresh approval to repair or replace this installation. The account portal verifies ownership and the current installation. Existing imports and calendar events are kept. Connections with cloud delivery history require separate owner repair.", "settings-help"));
      const reconnectLabel = node("label", "New installation name");
      const reconnectName = document.createElement("input");
      reconnectName.name = "reconnect_label";
      reconnectName.value = "Home Assistant";
      reconnectName.required = true;
      reconnectName.maxLength = 128;
      reconnectName.disabled = this.busy || blocked;
      reconnectLabel.append(reconnectName);
      const reconnectConsent = checkbox("reconnect_consent", "I renew my consent to cloud processing for this new owner-approved connection.");
      const reconnectConfirmation = checkbox("confirm_reconnect", "I understand that owner approval may replace this installation. Existing imports and calendar events will be kept.");
      reconnectConsent.input.disabled = reconnectConfirmation.input.disabled = this.busy || blocked;
      const repair = node("button", "Start owner-approved reconnect");
      repair.type = "submit";
      repair.disabled = true;
      const canReconnect = () => { repair.disabled = this.busy || blocked || state.state === "operation_finishing" || !reconnectConsent.input.checked || !reconnectConfirmation.input.checked; };
      reconnectConsent.input.addEventListener("change", canReconnect);
      reconnectConfirmation.input.addEventListener("change", canReconnect);
      reconnect.append(reconnectLabel, reconnectConsent.label, reconnectConfirmation.label, repair);
      reconnect.addEventListener("submit", event => {
        event.preventDefault();
        if (!reconnectConsent.input.checked || !reconnectConfirmation.input.checked) return;
        void this.submit("reconnect", {processing_consent: true, installation_label: reconnectName.value, confirm_reconnect: true});
      });
      section.append(reconnect);

    }

    const disconnect = document.createElement("form");
    disconnect.setAttribute("data-hosted-disconnect", "");
    const confirmation = checkbox("confirm_disconnect", "Stop hosted work on this installation and request remote disconnection.");
    confirmation.input.disabled = this.busy;
    const stop = node("button", "Disconnect");
    stop.type = "submit";
    stop.disabled = true;
    confirmation.input.addEventListener("change", () => { stop.disabled = this.busy || !confirmation.input.checked; });
    disconnect.append(confirmation.label, stop);
    disconnect.addEventListener("submit", event => {
      event.preventDefault();
      if (confirmation.input.checked === true) void this.submit("disconnect", {confirm_disconnect: true});
    });
    section.append(disconnect);
  }
}
