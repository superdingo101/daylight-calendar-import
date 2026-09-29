import {decideEvent, loadInbox, loadImport, saveEvent, summarizeImport} from "./inbox.js";

const css = `
  :host { display: block; color: var(--primary-text-color); font-family: var(--paper-font-body1_-_font-family, sans-serif); }
  main { max-width: 820px; margin: 0 auto; padding: 20px; }
  header { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  h1 { font-size: 1.65rem; line-height: 1.25; }
  button { cursor: pointer; border: 1px solid var(--divider-color); border-radius: 8px; background: var(--card-background-color); color: inherit; padding: 10px 14px; font: inherit; }
  button:focus-visible { outline: 3px solid var(--primary-color); outline-offset: 2px; }
  ul { list-style: none; padding: 0; display: grid; gap: 12px; }
  li { border: 1px solid var(--divider-color); border-radius: 12px; padding: 16px; background: var(--card-background-color); }
  h2 { font-size: 1.15rem; margin: 0 0 8px; overflow-wrap: anywhere; }
  p { margin: 6px 0; overflow-wrap: anywhere; }
  .error { color: var(--error-color); }
  .source { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 18rem; overflow: auto; }
  .detail-event { border-top: 1px solid var(--divider-color); padding: 12px 0; }
  form { display: grid; gap: 12px; }
  label { display: grid; gap: 4px; }
  input, textarea { box-sizing: border-box; width: 100%; padding: 8px; font: inherit; color: inherit; background: var(--card-background-color); border: 1px solid var(--divider-color); border-radius: 6px; }
  input[type=checkbox] { width: auto; }
  textarea { min-height: 10rem; resize: vertical; }
  .actions { display: flex; gap: 8px; flex-wrap: wrap; }
  @media (max-width: 480px) { main { padding: 12px; } h1 { font-size: 1.35rem; } li { padding: 12px; } }
`;

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

function eventRange(event) {
  if (!event.all_day) return `${event.start} – ${event.end}`;
  const end = new Date(`${event.end}T00:00:00Z`);
  if (Number.isNaN(end.getTime())) return `${event.start} – ${event.end} (exclusive end)`;
  end.setUTCDate(end.getUTCDate() - 1);
  const lastDay = end.toISOString().slice(0, 10);
  return event.start === lastDay ? event.start : `${event.start} – ${lastDay}`;
}

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
    this._batchAction = null;
    this._batchResults = [];
    this._batchContext = null;
    const style = element("style", css);
    const main = document.createElement("main");
    const header = document.createElement("header");
    header.append(element("h1", "Daylight imports"));
    const refresh = element("button", "Refresh");
    this._refreshButton = refresh;
    refresh.type = "button";
    refresh.addEventListener("click", () => {
      if (!this._saving && !this._editingId && !this._batchAction) void (this._selectedId ? this.showImport(this._selectedId) : this.refresh());
    });
    header.append(refresh);
    this._content = document.createElement("div");
    this._announcement = document.createElement("div");
    this._announcement.setAttribute("aria-live", "polite");
    main.append(header, this._announcement, this._content);
    this.shadowRoot.append(style, main);
  }

  set hass(value) {
    this._hass = value;
    if (!this._loaded) {
      this._loaded = true;
      void this.refresh();
    }
  }

  async refresh() {
    if (this._saving || this._editingId || this._batchAction) return;
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
    this.render();
    if (this._returnFocusId) {
      const button = Array.from(this._content.querySelectorAll("button"))
        .find((candidate) => candidate.dataset.pendingId === this._returnFocusId);
      (button || this._refreshButton).focus();
      this._returnFocusId = null;
    }
  }

  async showImport(id) {
    if (this._saving || this._editingId || this._batchAction) return;
    const generation = ++this._generation;
    this._selectedId = id;
    this._detail = null;
    this._editingId = null;
    this._editError = null;
    this._decision = null;
    this._decisionError = null;
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
    if (this._saving || this._editingId || this._decision || this._batchAction) return;
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
        results.push({id: event.id, title: event.title, range: eventRange(event), outcome: "success"});
      } catch (error) {
        const reason = typeof error?.message === "string" ? error.message : "Review action failed";
        results.push({id: event.id, title: event.title, range: eventRange(event),
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
      (this._content.querySelector("button") || this._refreshButton).focus();
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

  async saveEdit(event, form) {
    if (this._saving) return;
    const fields = form.elements;
    const draft = {
      title: fields.namedItem("title").value,
      start: fields.namedItem("start").value,
      end: fields.namedItem("end").value,
      all_day: fields.namedItem("all_day").checked,
      location: fields.namedItem("location").value,
      description: fields.namedItem("description").value,
      confidence: event.confidence,
    };
    const generation = this._generation;
    const pendingId = this._selectedId;
    this._saving = true;
    this._editError = null;
    this._refreshButton.disabled = true;
    for (const button of this._content.querySelectorAll("button")) button.disabled = true;
    for (const tag of ["input", "textarea", "button"]) {
      for (const control of form.querySelectorAll(tag)) control.disabled = true;
    }
    try {
      await saveEvent(this._hass, pendingId, event, draft);
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
      form.querySelector(".error")?.remove();
      const message = element("p", this._editError, "error");
      message.setAttribute("role", "alert");
      message.tabIndex = -1;
      form.prepend(message);
      this._announcement.replaceChildren(element("span", this._editError));
      for (const tag of ["input", "textarea", "button"]) {
        for (const control of form.querySelectorAll(tag)) control.disabled = false;
      }
      message.focus();
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
    const start = field("start", "Start (ISO date or date and time with UTC offset)", event.start);
    const end = field("end", "End (exclusive for all-day events)", event.end);
    start.required = end.required = true;
    field("location", "Location", event.location);
    field("description", "Description and meeting join details", event.description, "textarea");
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
    this._refreshButton.disabled = this._saving || Boolean(this._editingId || this._decision || this._batchAction);
    const content = document.createDocumentFragment();
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
      back.disabled = Boolean(this._editingId || this._decision || this._batchAction);
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
      back.disabled = Boolean(this._editingId || this._decision || this._batchAction);
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
      const source = element("p", detail.source_text || "No source text available.", "source");
      source.tabIndex = 0;
      source.setAttribute("role", "region");
      source.setAttribute("aria-label", "Source text");
      content.append(source);
      content.append(element("h2", "Events"));
      const ready = detail.events.filter(event => event.status === "pending");
      if (ready.length > 1 && !this._editingId && !this._decision) {
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
        card.append(element("p", `${eventRange(event)}${event.all_day ? " · All day" : ""}`));
        card.append(element("p", `Calendar: ${event.calendar_entity || "Default"} · Status: ${event.status}`));
        if (typeof event.confidence === "number") {
          card.append(element("p", `AI extraction confidence: ${Math.round(event.confidence * 100)}% (estimate)`));
        }
        if (event.location) card.append(element("p", `Location: ${event.location}`));
        if (event.description) card.append(element("p", event.description));
        if (event.status === "pending" && !this._editingId && !this._decision && !this._batchAction) {
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
        const summary = summarizeImport(item, this._hass?.locale?.language);
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
    this._content.replaceChildren(content);
    this._announcement.replaceChildren();
    if (this._status === "loading") this._announcement.append(element("span", "Loading imports…"));
    else if (this._status !== "ready") this._announcement.append(element("span", this._status));
    else this._announcement.append(element("span", this._selectedId ? "Import detail loaded" : "Inbox loaded"));
  }
}

customElements.define("daylight-import-panel", DaylightImportPanel);
