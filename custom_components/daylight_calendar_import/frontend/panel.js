import {loadInbox, summarizeImport} from "./inbox.js";

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
  @media (max-width: 480px) { main { padding: 12px; } h1 { font-size: 1.35rem; } li { padding: 12px; } }
`;

function element(tag, text, className) {
  const node = document.createElement(tag);
  node.textContent = text;
  if (className) node.className = className;
  return node;
}

class DaylightImportPanel extends HTMLElement {
  constructor() {
    super();
    this.attachShadow({mode: "open"});
    this._items = [];
    this._status = "loading";
    this._loaded = false;
  }

  set hass(value) {
    this._hass = value;
    if (!this._loaded) {
      this._loaded = true;
      void this.refresh();
    }
  }

  async refresh() {
    this._status = "loading";
    this.render();
    try {
      this._items = await loadInbox(this._hass);
      this._status = "ready";
    } catch (error) {
      this._status = error instanceof Error ? error.message : "Could not load imports. Try again.";
    }
    this.render();
  }

  render() {
    const root = this.shadowRoot;
    const style = element("style", css);
    const main = document.createElement("main");
    const header = document.createElement("header");
    header.append(element("h1", "Daylight imports"));
    const refresh = element("button", "Refresh");
    refresh.type = "button";
    refresh.addEventListener("click", () => void this.refresh());
    header.append(refresh);
    main.append(header);
    if (this._status === "loading") {
      main.append(element("p", "Loading imports…", "status"));
    } else if (this._status !== "ready") {
      main.append(element("p", this._status, "error"));
    } else if (this._items.length === 0) {
      main.append(element("p", "No imports awaiting review.", "status"));
    } else {
      main.append(element("p", `${this._items.length} imports awaiting review`, "status"));
      const list = document.createElement("ul");
      for (const item of this._items) {
        const summary = summarizeImport(item, this._hass?.locale?.language);
        const card = document.createElement("li");
        card.append(element("h2", summary.title));
        card.append(element("p", `${summary.type} · ${summary.created} · ${summary.events}`));
        const attention = [];
        if (summary.warnings) attention.push(`${summary.warnings} parser warnings`);
        if (summary.duplicates) attention.push(`${summary.duplicates} duplicates skipped`);
        if (summary.uncertain) attention.push("Calendar write needs confirmation");
        if (attention.length) card.append(element("p", attention.join(" · ")));
        list.append(card);
      }
      main.append(list);
    }
    root.replaceChildren(style, main);
  }
}

customElements.define("daylight-import-panel", DaylightImportPanel);
