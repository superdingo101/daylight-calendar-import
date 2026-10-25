import {test, expect} from "@playwright/test";

const CODE = "ABCD1234EFGH";
const SECRET = "private-material-must-never-reach-settings";
const errors = new WeakMap();
const actions = page => page.evaluate(() => window.hostedFixture.calls.filter(call => call.type.endsWith("/action")));
const sections = page => page.getByRole("navigation", {name: "Settings sections"});
const openCloud = async page => {
  await page.getByRole("navigation", {name: "Daylight views"}).getByRole("button", {name: "Settings", exact: true}).click();
  await sections(page).getByRole("button", {name: "Daylight Cloud", exact: true}).click();
  await expect(page.getByRole("heading", {name: "Daylight connection", exact: true})).toBeVisible();
  await expect(page.getByLabel("Installation name", {exact: true})).toBeVisible();
};
const consent = page => page.getByRole("checkbox", {name: "I agree to send selected source content to Daylight for cloud processing.", exact: true});
const assertNoStoredSecrets = async page => {
  const stored = await page.evaluate(() => JSON.stringify({
    local: Object.entries(localStorage), session: Object.entries(sessionStorage), cookies: document.cookie,
  }));
  expect(stored).not.toContain(CODE);
  expect(stored).not.toContain(SECRET);
};

test.beforeEach(async ({page}) => {
  const pageErrors = [];
  errors.set(page, pageErrors);
  page.on("pageerror", error => pageErrors.push(error.message));
  await page.goto("/");
  await expect(page.getByText("No imports awaiting review.")).toBeVisible();
  await page.evaluate(({code, secret}) => {
    const panel = document.querySelector("daylight-import-panel");
    const fallback = panel._hass.callWS;
    const state = {entry_id: "entry-1", configured: true, automatic_lifecycle: false,
      api_origin: "https://api.example.test", account_origin: "https://account.example.test",
      account_url: null, state: "disconnected", credential_status: null, credential_expires_at: null};
    const instructions = {user_code: code, verification_uri: "https://account.example.test/connect",
      expires_at: "2026-12-01T00:10:00Z", poll_interval_seconds: 5};
    const fixture = window.hostedFixture = {calls: [], state, hold: false, malformed: false, release: null};
    const connection = {connected: true, subscribeMessage: async (callback, message) => {
      fixture.subscriber = callback;
      fixture.subscription = message;
      return () => { fixture.subscriber = null; };
    }};
    panel.hass = {...panel._hass, connection, user: {id: "owner", is_admin: true}, callWS: async call => {
      if (call.type === "daylight_calendar_import/settings/get") {
        return {entry_id: "entry-1", ai_task_entity: "ai_task.openai", calendar_entity: "calendar.family",
          calendar_entities: ["calendar.family"], notifications: {enabled: false, target: null, classes: []},
          email: {enabled: false, host: "", port: 993, username: "", password_configured: false,
            mailbox: "INBOX", verify_ssl: true}};
      }
      if (!call.type?.startsWith("daylight_calendar_import/hosted/")) return fallback(call);
      fixture.calls.push(structuredClone(call));
      if (call.type.endsWith("/get")) return structuredClone(fixture.state);
      if (call.type.endsWith("/configure")) {
        fixture.state = {...fixture.state, api_origin: call.api_origin, account_origin: call.account_origin,
          automatic_lifecycle: call.automatic_lifecycle};
        return structuredClone(fixture.state);
      }
      if (call.type.endsWith("/action")) {
        if (["connect", "reconnect"].includes(call.action)) {
          fixture.state = {...fixture.state, state: call.action === "reconnect" ? "reconnect_pending" : "pairing_pending"};
          if (fixture.hold) await new Promise(resolve => { fixture.release = resolve; });
          return {...fixture.state, pairing: instructions,
            ...(fixture.malformed ? {installation_token: secret} : {})};
        }
        if (call.action === "reconcile" && fixture.state.state === "operation_finishing") fixture.state = {...fixture.state, state: "disconnected"};
        if (call.action === "disconnect") fixture.state = {...fixture.state, state: "remote_disconnect_unconfirmed"};
        return structuredClone(fixture.state);
      }
      throw new Error("Unexpected hosted fixture request");
    }};
  }, {code: CODE, secret: SECRET});
});

test.afterEach(async ({page}) => {
  expect(errors.get(page)).toEqual([]);
  await assertNoStoredSecrets(page);
});

test("native connection needs consent and confirmation, with transient approval instructions", async ({page}) => {
  await openCloud(page);
  const start = page.getByRole("button", {name: "Start new connection", exact: true});
  await expect(start).toBeDisabled();
  expect(await actions(page)).toEqual([]);
  await page.getByLabel("Installation name", {exact: true}).fill("Kitchen Home Assistant");
  await consent(page).check();
  await expect(start).toBeEnabled();
  await start.click();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toBeVisible();
  const approval = page.getByRole("link", {name: "Approve this connection", exact: true});
  await expect(approval).toHaveAttribute("href", "https://account.example.test/connect");
  await expect(approval).toHaveAttribute("rel", "noopener noreferrer");
  expect(await actions(page)).toEqual([{type: "daylight_calendar_import/hosted/action", entry_id: "entry-1",
    action: "connect", processing_consent: true, installation_label: "Kitchen Home Assistant"}]);
  await assertNoStoredSecrets(page);

  await sections(page).getByRole("button", {name: "General", exact: true}).click();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toHaveCount(0);
  await sections(page).getByRole("button", {name: "Daylight Cloud", exact: true}).click();
  await expect(page.getByText("Waiting for approval in the account portal.", {exact: false})).toBeVisible();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toHaveCount(0);
  await expect(page.getByRole("link", {name: "Approve this connection", exact: true})).toHaveCount(0);
  expect((await actions(page)).map(call => call.action)).toEqual(["connect"]);
  // Redisplaying a lost code requires a fresh deliberate consent/creation,
  // rather than replaying the previous creation automatically.
  await expect(consent(page)).not.toBeChecked();
  await expect(page.getByRole("button", {name: "Start new connection", exact: true})).toBeDisabled();

  const disconnect = page.getByRole("button", {name: "Disconnect", exact: true});
  await expect(disconnect).toBeDisabled();
  await page.getByRole("checkbox", {name: "Stop hosted work on this installation and request remote disconnection.", exact: true}).check();
  await disconnect.click();
  await expect(page.getByText("Hosted work is stopped locally. Remote disconnection is unconfirmed; reconcile it in the account portal.", {exact: true})).toBeVisible();
  expect(await actions(page)).toEqual([
    {type: "daylight_calendar_import/hosted/action", entry_id: "entry-1", action: "connect",
      processing_consent: true, installation_label: "Kitchen Home Assistant"},
    {type: "daylight_calendar_import/hosted/action", entry_id: "entry-1", action: "disconnect", confirm_disconnect: true},
  ]);
  for (const control of await page.locator("button:visible, input:visible").all()) {
    const box = await control.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize().width);
  }
});

test("an identity change discards a late one-time pairing response without retry", async ({page}) => {
  await openCloud(page);
  await page.evaluate(() => { window.hostedFixture.hold = true; });
  await consent(page).check();
  await page.getByRole("button", {name: "Start new connection", exact: true}).click();
  await expect.poll(() => page.evaluate(() => typeof window.hostedFixture.release)).toBe("function");
  await page.evaluate(() => {
    const panel = document.querySelector("daylight-import-panel");
    panel.hass = {...panel._hass, user: {id: "another-owner", is_admin: true}};
    window.hostedFixture.release();
  });
  await expect(page.getByText("Waiting for approval in the account portal.", {exact: false})).toBeVisible();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toHaveCount(0);
  await expect(page.getByRole("link", {name: "Approve this connection", exact: true})).toHaveCount(0);
  expect((await actions(page)).map(call => call.action)).toEqual(["connect"]);
  await assertNoStoredSecrets(page);
});

test("a secret-bearing malformed response remains an unconfirmed failure", async ({page}) => {
  await openCloud(page);
  await page.evaluate(() => { window.hostedFixture.malformed = true; });
  await consent(page).check();
  await page.getByRole("button", {name: "Start new connection", exact: true}).click();
  await expect(page.getByRole("alert")).toContainText("The connection request could not be confirmed.");
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toHaveCount(0);
  await expect(page.getByText(SECRET, {exact: false})).toHaveCount(0);
  expect((await actions(page)).map(call => call.action)).toEqual(["connect"]);
  await assertNoStoredSecrets(page);
});


test("settled operation recovery is explicit and does not replay creation or renewal", async ({page}) => {
  await page.evaluate(() => { window.hostedFixture.state.state = "operation_finishing"; });
  await page.getByRole("navigation", {name: "Daylight views"}).getByRole("button", {name: "Settings", exact: true}).click();
  await sections(page).getByRole("button", {name: "Daylight Cloud", exact: true}).click();
  const recover = page.getByRole("button", {name: "Check and recover connection", exact: true});
  await expect(recover).toBeEnabled();
  await expect(page.getByRole("button", {name: "Renew credentials", exact: true})).toBeDisabled();
  expect(await actions(page)).toEqual([]);
  await recover.click();
  await expect(consent(page)).not.toBeChecked();
  expect((await actions(page)).map(call => call.action)).toEqual(["reconcile"]);
});


test("opt-in maintenance refreshes safe status and clears transient approval", async ({page}) => {
  await openCloud(page);
  const automatic = page.getByRole("checkbox", {name: "Automatically maintain this connection", exact: true});
  await expect(automatic).not.toBeChecked();
  await automatic.check();
  await page.getByRole("button", {name: "Save service configuration", exact: true}).click();
  await expect(automatic).toBeChecked();
  await expect.poll(() => page.evaluate(() => typeof window.hostedFixture.subscriber)).toBe("function");
  expect(await actions(page)).toEqual([]);
  await consent(page).check();
  await page.getByRole("button", {name: "Start new connection", exact: true}).click();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toBeVisible();
  await page.getByLabel("API origin", {exact: true}).fill("https://draft-api.example.test");
  await automatic.uncheck();
  await page.getByLabel("API origin", {exact: true}).focus();
  await page.evaluate(() => {
    window.hostedFixture.state.state = "pairing_retry";
    window.hostedFixture.subscriber({entry_id: "entry-1"});
  });
  await expect(page.getByText("Approval checking was interrupted.", {exact: false})).toBeVisible();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toBeVisible();
  await expect(page.getByLabel("API origin", {exact: true})).toHaveValue("https://draft-api.example.test");
  await expect(automatic).not.toBeChecked();
  await expect(page.getByLabel("API origin", {exact: true})).toBeFocused();
  await page.evaluate(() => {
    const fixture = window.hostedFixture;
    fixture.state = {...fixture.state, state: "connected", credential_status: "current", credential_expires_at: "2026-12-01T00:00:00Z"};
    fixture.subscriber({entry_id: "entry-1"});
  });
  await expect(page.getByText("The saved installation credential has been verified.", {exact: true})).toBeVisible();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toHaveCount(0);
  expect((await actions(page)).map(call => call.action)).toEqual(["connect"]);
  await sections(page).getByRole("button", {name: "General", exact: true}).click();
  await expect.poll(() => page.evaluate(() => window.hostedFixture.subscriber)).toBe(null);
});


test("reconnect needs renewed consent, replacement confirmation and fresh transient owner approval", async ({page}) => {
  await openCloud(page);
  const reconnect = page.getByRole("button", {name: "Start owner-approved reconnect", exact: true});
  await expect(reconnect).toBeDisabled();
  await page.getByRole("checkbox", {name: "I renew my consent to cloud processing for this new owner-approved connection.", exact: true}).check();
  await expect(reconnect).toBeDisabled();
  await page.getByRole("checkbox", {name: "I understand that owner approval may replace this installation. Existing imports and calendar events will be kept.", exact: true}).check();
  await expect(reconnect).toBeEnabled();
  await reconnect.click();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toBeVisible();
  await expect(page.getByRole("button", {name: "Check pairing approval", exact: true})).toBeEnabled();
  expect(await actions(page)).toEqual([{type: "daylight_calendar_import/hosted/action", entry_id: "entry-1", action: "reconnect", processing_consent: true, installation_label: "Home Assistant", confirm_reconnect: true}]);
  await sections(page).getByRole("button", {name: "General", exact: true}).click();
  await sections(page).getByRole("button", {name: "Daylight Cloud", exact: true}).click();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toHaveCount(0);
  await expect(page.getByRole("button", {name: "Check pairing approval", exact: true})).toBeEnabled();
  expect((await actions(page)).map(call => call.action)).toEqual(["reconnect"]);
});
