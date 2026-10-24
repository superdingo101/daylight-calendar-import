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
    const state = {entry_id: "entry-1", configured: true,
      api_origin: "https://api.example.test", account_origin: "https://account.example.test",
      account_url: null, state: "disconnected", credential_status: null, credential_expires_at: null};
    const instructions = {user_code: code, verification_uri: "https://account.example.test/connect",
      expires_at: "2026-12-01T00:10:00Z", poll_interval_seconds: 5};
    const fixture = window.hostedFixture = {calls: [], state, hold: false, malformed: false, release: null};
    panel.hass = {...panel._hass, user: {id: "owner", is_admin: true}, callWS: async call => {
      if (call.type === "daylight_calendar_import/settings/get") {
        return {entry_id: "entry-1", ai_task_entity: "ai_task.openai", calendar_entity: "calendar.family",
          calendar_entities: ["calendar.family"], notifications: {enabled: false, target: null, classes: []},
          email: {enabled: false, host: "", port: 993, username: "", password_configured: false,
            mailbox: "INBOX", verify_ssl: true}};
      }
      if (!call.type?.startsWith("daylight_calendar_import/hosted/")) return fallback(call);
      fixture.calls.push(structuredClone(call));
      if (call.type.endsWith("/get")) return structuredClone(fixture.state);
      if (call.type.endsWith("/action")) {
        if (call.action === "connect") {
          fixture.state = {...fixture.state, state: "pairing_pending"};
          if (fixture.hold) await new Promise(resolve => { fixture.release = resolve; });
          return {...fixture.state, pairing: instructions,
            ...(fixture.malformed ? {installation_token: secret} : {})};
        }
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
