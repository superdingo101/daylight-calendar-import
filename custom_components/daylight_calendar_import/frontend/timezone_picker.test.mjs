import assert from "node:assert/strict";
import test from "node:test";

class FakeNode {
  constructor(tag) {
    this.tag = tag; this.children = []; this.attributes = {};
    this.value = ""; this.disabled = false; this.events = {};
  }
  append(...children) { this.children.push(...children); }
  replaceChildren(...children) { this.children = children; }
  setAttribute(key, value) { this.attributes[key] = value; }
  removeAttribute(key) { delete this.attributes[key]; }
  addEventListener(key, callback) { this.events[key] = callback; }
  focus() { globalThis.focused = this; }
  fire(key) { this.events[key]?.({type: key}); }
  querySelectorAll(tag) {
    return [this, ...this.children.flatMap(n => n.querySelectorAll(tag))]
      .filter(n => n.tag === tag);
  }
}
globalThis.document = {createElement: tag => new FakeNode(tag)};
const {createTimezonePicker} = await import("./timezone_picker.js");

test("picker begins empty, cancels without changing and commits an explicit city choice", () => {
  const changes = [];
  const picker = createTimezonePicker({
    label: "Arrival timezone", wallDate: "2028-07-10T16:30:00",
    onChange: value => changes.push(value),
  });
  assert.equal(picker.value, "");
  assert.match(picker.trigger.textContent, /Choose timezone/);
  picker.trigger.fire("click");
  const dialog = picker.element.querySelectorAll("dialog")[0];
  assert.ok("open" in dialog.attributes);
  const search = dialog.querySelectorAll("input")[0];
  const list = dialog.querySelectorAll("select")[0];
  const buttons = dialog.querySelectorAll("button");
  assert.equal(list.children.length, 420);
  assert.equal(globalThis.focused, search);
  search.value = "New York";
  search.fire("input");
  assert.ok(list.children.some(option => option.value === "America/New_York"));
  list.value = "America/New_York";
  list.fire("change");
  assert.equal(picker.value, "", "highlighting does not confirm a timezone");
  buttons.find(button => button.textContent === "Cancel").fire("click");
  assert.deepEqual(changes, []);
  assert.equal(picker.value, "");
  assert.equal(globalThis.focused, picker.trigger);
  picker.trigger.fire("click");
  const refreshed = dialog.querySelectorAll("select")[0];
  refreshed.value = "America/New_York";
  refreshed.fire("change");
  buttons.find(button => button.textContent === "Use selected timezone").fire("click");
  assert.equal(picker.value, "America/New_York");
  assert.deepEqual(changes, ["America/New_York"]);
  assert.match(picker.trigger.textContent, /New York, United States/);
  assert.match(picker.element.querySelectorAll("span").at(-1).textContent,
    /UTC-04:00/);
});

test("suggestions are advisory and never preselect or invent invalid timezone identifiers", () => {
  const changes = [];
  const picker = createTimezonePicker({
    label: "Departure timezone", wallDate: "2028-07-10T08:00:00",
    suggestions: ["not-valid", "America/Los_Angeles"],
    onChange: value => changes.push(value),
  });
  assert.equal(picker.value, "");
  picker.trigger.fire("click");
  const dialog = picker.element.querySelectorAll("dialog")[0];
  const options = dialog.querySelectorAll("button")
    .filter(button => button.className === "timezone-picker-suggestion");
  assert.equal(options.length, 1);
  assert.equal(picker.value, "");
  assert.deepEqual(changes, []);
  options[0].fire("click");
  assert.deepEqual(changes, ["America/Los_Angeles"]);
  picker.setSuggestions(["Asia/Kathmandu", "Asia/Kathmandu", "bad-zone"]);
  picker.trigger.fire("click");
  const next = dialog.querySelectorAll("button")
    .filter(button => button.className === "timezone-picker-suggestion");
  assert.equal(next.length, 1);
  next[0].fire("click");
  assert.equal(picker.value, "Asia/Kathmandu");
  picker.clear();
  assert.equal(picker.value, "");
  assert.deepEqual(changes, ["America/Los_Angeles", "Asia/Kathmandu", ""]);
});

test("search can browse all zones without Intl.supportedValuesOf or query", () => {
  const original = Intl.supportedValuesOf;
  try {
    Intl.supportedValuesOf = undefined;
    const picker = createTimezonePicker({
      label: "Event timezone", wallDate: "2028-01-10T08:00:00",
    });
    picker.trigger.fire("click");
    const dialog = picker.element.querySelectorAll("dialog")[0];
    const options = dialog.querySelectorAll("option");
    assert.equal(options.length, 420);
    assert.ok(options.some(option => option.value === "Australia/Adelaide"));
    assert.ok(options.some(option => option.value === "Etc/UTC"));
  } finally {
    Intl.supportedValuesOf = original;
  }
});


test("Escape and native cancel close the dialog without selecting a timezone", () => {
  const changes = [];
  const picker = createTimezonePicker({
    label: "Arrival timezone", wallDate: "2028-07-10T16:30:00",
    onChange: id => changes.push(id),
  });
  picker.trigger.fire("click");
  const dialog = picker.element.querySelectorAll("dialog")[0];
  assert.ok("open" in dialog.attributes);
  const search = dialog.querySelectorAll("input")[0];
  search.value = "New York";
  search.fire("input");
  const select = dialog.querySelectorAll("select")[0];
  select.value = "America/New_York";
  select.fire("change");
  let prevented = false;
  dialog.events.keydown({key: "Escape", preventDefault: () => { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal("open" in dialog.attributes, false);
  assert.equal(picker.value, "");
  assert.deepEqual(changes, []);
  assert.equal(globalThis.focused, picker.trigger);

  picker.trigger.fire("click");
  assert.ok("open" in dialog.attributes);
  prevented = false;
  dialog.events.cancel({preventDefault: () => { prevented = true; }});
  assert.equal(prevented, true);
  assert.equal("open" in dialog.attributes, false);
  assert.equal(picker.value, "");
  assert.deepEqual(changes, []);
});
