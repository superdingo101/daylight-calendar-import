import assert from "node:assert/strict";
import test from "node:test";

import {isSettingsErrorCode, loadSettings, saveCoreSettings, saveEmailSettings, saveCalendarIntelligenceSettings, saveNotificationSettings, settingsErrorMessage} from "./settings.js";

const snapshot = {
  entry_id: "entry-1",
  ai_task_entity: "ai_task.openai",
  calendar_entity: "calendar.family",
  calendar_entities: ["calendar.family"],
  notifications: {enabled: false, target: null, classes: []},
  email: {
    enabled: false,
    host: "",
    port: 993,
    username: "",
    password_configured: false,
    mailbox: "INBOX",
    verify_ssl: true,
  },
};

test("settings client loads secret-safe settings", async () => {
  const calls = [];
  const hass = {callWS: async message => {
    calls.push(message);
    return snapshot;
  }};

  assert.deepEqual(await loadSettings(hass), snapshot);
  assert.deepEqual(calls, [{type: "daylight_calendar_import/settings/get"}]);
});

test("settings client sends only changed core fields", async () => {
  const calls = [];
  const hass = {callWS: async message => {
    calls.push(message);
    return message.ai_task_entity ?
      {...snapshot, ai_task_entity: message.ai_task_entity} :
      {...snapshot, calendar_entity: message.calendar_entity,
        calendar_entities: message.calendar_entities};
  }};

  const general = await saveCoreSettings(hass, {
    entry_id: "entry-1",
    ai_task_entity: "ai_task.google",
  });
  const calendars = await saveCoreSettings(hass, {
    entry_id: "entry-1",
    calendar_entity: "calendar.work",
    calendar_entities: ["calendar.work"],
  });

  assert.equal(general.ai_task_entity, "ai_task.google");
  assert.equal(calendars.calendar_entity, "calendar.work");
  assert.deepEqual(calls, [
    {
      type: "daylight_calendar_import/settings/core/update",
      entry_id: "entry-1",
      ai_task_entity: "ai_task.google",
    },
    {
      type: "daylight_calendar_import/settings/core/update",
      entry_id: "entry-1",
      calendar_entity: "calendar.work",
      calendar_entities: ["calendar.work"],
    },
  ]);
});

test("settings client omits untouched optional email fields", async () => {
  const calls = [];
  const hass = {callWS: async message => {
    calls.push(message);
    return snapshot;
  }};

  await saveEmailSettings(hass, {
    entry_id: "entry-1",
    enabled: false,
    password: undefined,
  });

  assert.deepEqual(calls[0], {
    type: "daylight_calendar_import/settings/email/update",
    entry_id: "entry-1",
    enabled: false,
  });
});

test("settings client rejects malformed responses", async () => {
  const hass = {callWS: async () => ({entry_id: "entry-1"})};
  await assert.rejects(
    () => loadSettings(hass),
    /unexpected response/,
  );
});


test("settings client recognizes Home Assistant websocket errors", () => {
  const error = {code: "reload_failed", message: "Saved, but reload failed."};
  assert.equal(isSettingsErrorCode(error, "reload_failed"), true);
  assert.equal(isSettingsErrorCode(error, "cannot_connect"), false);
  assert.equal(isSettingsErrorCode(new Error("reload_failed"), "reload_failed"), false);
  assert.equal(settingsErrorMessage(error, "fallback"), "Saved, but reload failed.");
  assert.equal(settingsErrorMessage(new Error("Network failed"), "fallback"), "Network failed");
  assert.equal(settingsErrorMessage("Disconnected", "fallback"), "Disconnected");
  assert.equal(settingsErrorMessage(null, "fallback"), "fallback");
});

test("calendar intelligence settings only send explicit changes", async () => {
  const calls = [];
  const hass = {callWS: async value => {
    calls.push(value);
    return {...snapshot, calendar_aliases: value.calendar_aliases ?? {},
      conflict_calendar_entities: value.conflict_calendar_entities ?? []};
  }};
  const result = await saveCalendarIntelligenceSettings(hass, {
    entry_id: "entry-1",
    calendar_aliases: {kids: "calendar.family"},
    conflict_calendar_entities: ["calendar.work"],
  });
  assert.deepEqual(calls, [{
    type: "daylight_calendar_import/settings/calendar_intelligence/update",
    entry_id: "entry-1",
    calendar_aliases: {kids: "calendar.family"},
    conflict_calendar_entities: ["calendar.work"],
  }]);
  assert.deepEqual(result.calendar_aliases, {kids: "calendar.family"});
});


test("notification client sends only the explicit opt-in policy", async () => {
  const calls = [];
  const hass = {callWS: async message => {
    calls.push(message);
    return {entry_id: "entry-1", ai_task_entity: "ai_task.initial",
      calendar_entity: "calendar.family", calendar_entities: ["calendar.family"],
      notifications: {enabled: true, target: "notify.phone",
        classes: ["review_ready"]}, email: {}};
  }};
  await saveNotificationSettings(hass, {
    entry_id: "entry-1", notifications: {
      enabled: true, target: "notify.phone", classes: ["review_ready"],
    },
    expected_notifications: snapshot.notifications,
  });
  assert.deepEqual(calls, [{
    type: "daylight_calendar_import/settings/notifications/update",
    entry_id: "entry-1", notifications: {
      enabled: true, target: "notify.phone", classes: ["review_ready"],
    },
    expected_notifications: snapshot.notifications,
  }]);
});


test("settings client rejects missing or malformed notification policy", async () => {
  for (const notifications of [undefined, {enabled: "yes", classes: [], target: null},
    {enabled: true, classes: ["secret"], target: "notify.phone"}]) {
    const response = {...snapshot, notifications};
    const hass = {callWS: async () => response};
    await assert.rejects(() => loadSettings(hass), /unexpected response/);
    await assert.rejects(() => saveNotificationSettings(hass, {
      entry_id: "entry-1", notifications: snapshot.notifications,
      expected_notifications: snapshot.notifications,
    }), /unexpected response/);
  }
});


test("notification snapshot validation permits Unicode decimal digits but rejects invalid edges", async () => {
  for (const target of ["notify.phone١", "notify.phone_١"]) {
    const valid = {...snapshot, notifications: {
      enabled: true, target, classes: ["review_ready"],
    }};
    assert.equal((await loadSettings({callWS: async () => valid})).notifications.target, target);
  }
  for (const target of ["notify._phone", "notify.phone_"]) {
    const invalid = {...snapshot, notifications: {
      enabled: true, target, classes: ["review_ready"],
    }};
    await assert.rejects(() => loadSettings({callWS: async () => invalid}),
      /unexpected response/);
  }
});


test("notification target length uses Unicode code points as on Home Assistant", async () => {
  const target = "notify." + "𝟘".repeat(121);
  assert.equal(Array.from(target).length, 128);
  assert.ok(target.length > 128);
  const valid = {...snapshot, notifications: {
    enabled: true, classes: ["review_ready"], target,
  }};
  assert.equal((await loadSettings({callWS: async () => valid})).notifications.target, target);
  const invalid = {...valid, notifications: {...valid.notifications, target: target + "𝟘"}};
  await assert.rejects(() => loadSettings({callWS: async () => invalid}),
    /unexpected response/);
});
