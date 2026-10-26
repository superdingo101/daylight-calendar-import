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
  await page.getByText("Advanced settings & troubleshooting", {exact: true}).click();
  await page.getByText("Troubleshooting & developer settings", {exact: true}).click();
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
        // Simulate the backend's first-time-only default. A configuration
        // repair reports configured:false despite already saved preferences.
        const firstSetup = fixture.state.state === "not_configured";
        const automatic = Object.hasOwn(call, "automatic_lifecycle")
          ? call.automatic_lifecycle
          : firstSetup ? true : fixture.state.automatic_lifecycle;
        fixture.state = {...fixture.state, configured: true,
          state: ["not_configured", "configuration_repair"].includes(fixture.state.state) ? "disconnected" : fixture.state.state,
          api_origin: call.api_origin, account_origin: call.account_origin,
          automatic_lifecycle: automatic};
        return structuredClone(fixture.state);
      }
      if (call.type.endsWith("/action")) {
        if (call.action === "reconnect" && fixture.pristine) {
          fixture.state = {...fixture.state, state: "first_connection_required"};
          return structuredClone(fixture.state);
        }
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
  await expect(page.getByText("Approve this connection in your Daylight account.", {exact: false})).toBeVisible();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toHaveCount(0);
  await expect(page.getByRole("link", {name: "Approve this connection", exact: true})).toHaveCount(0);
  expect((await actions(page)).map(call => call.action)).toEqual(["connect"]);
  // Redisplaying a lost code requires a fresh deliberate consent/creation,
  // rather than replaying the previous creation automatically.
  await expect(consent(page)).not.toBeChecked();
  await expect(page.getByRole("button", {name: "Start new connection", exact: true})).toBeDisabled();

  await page.getByText("Advanced settings & troubleshooting", {exact: true}).click();
  await page.getByText("Manage connection", {exact: true}).click();
  const disconnect = page.getByRole("button", {name: "Disconnect", exact: true});
  await disconnect.click();
  await expect(page.getByText("Disconnect Home Assistant?", {exact: true})).toBeVisible();
  expect((await actions(page)).length).toBe(1);
  await page.getByRole("button", {name: "Confirm disconnect", exact: true}).click();
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
  await expect(page.getByText("Approve this connection in your Daylight account.", {exact: false})).toBeVisible();
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
  await page.getByText("Advanced settings & troubleshooting", {exact: true}).click();
  await page.getByText("Troubleshooting & developer settings", {exact: true}).click();
  const recover = page.getByRole("button", {name: "Check and recover connection", exact: true});
  await expect(recover).toBeEnabled();
  await expect(page.getByRole("button", {name: "Renew credentials", exact: true})).toHaveCount(0);
  expect(await actions(page)).toEqual([]);
  await recover.click();
  await expect(consent(page)).not.toBeChecked();
  expect((await actions(page)).map(call => call.action)).toEqual(["reconcile"]);
});


test("automatic maintenance keeps drafts during safe status updates without a user toggle", async ({page}) => {
  await page.evaluate(() => { window.hostedFixture.state.automatic_lifecycle = true; });
  await openCloud(page);
  await expect(page.getByRole("checkbox", {name: "Automatically maintain this connection"})).toHaveCount(0);
  await expect.poll(() => page.evaluate(() => typeof window.hostedFixture.subscriber)).toBe("function");
  expect(await actions(page)).toEqual([]);
  await consent(page).check();
  await page.getByRole("button", {name: "Start new connection", exact: true}).click();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toBeVisible();
  await page.getByLabel("API origin", {exact: true}).fill("https://draft-api.example.test");
  await page.getByLabel("API origin", {exact: true}).focus();
  await page.evaluate(() => {
    window.hostedFixture.state.state = "pairing_retry";
    window.hostedFixture.subscriber({entry_id: "entry-1"});
  });
  await expect(page.getByText("Approval checking was interrupted.", {exact: false})).toBeVisible();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toBeVisible();
  await expect(page.getByLabel("API origin", {exact: true})).toHaveValue("https://draft-api.example.test");
  await expect(page.getByLabel("API origin", {exact: true})).toBeFocused();
  await page.evaluate(() => {
    const fixture = window.hostedFixture;
    fixture.state = {...fixture.state, state: "connected", credential_status: "current", credential_expires_at: "2026-12-01T00:00:00Z"};
    fixture.subscriber({entry_id: "entry-1"});
  });
  await expect(page.getByText("Your Home Assistant is securely connected to Daylight.", {exact: true})).toBeVisible();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toHaveCount(0);
  expect((await actions(page)).map(call => call.action)).toEqual(["connect"]);
  await sections(page).getByRole("button", {name: "General", exact: true}).click();
  await expect.poll(() => page.evaluate(() => window.hostedFixture.subscriber)).toBe(null);
});

test("a legacy connection keeps its opt-out until the one-time renewal upgrade", async ({page}) => {
  await page.evaluate(() => {
    window.hostedFixture.state = {...window.hostedFixture.state,
      state: "connected", credential_status: "current", credential_expires_at: "2027-01-01T00:00:00Z"};
  });
  await page.getByRole("navigation", {name: "Daylight views"}).getByRole("button", {name: "Settings", exact: true}).click();
  await sections(page).getByRole("button", {name: "Daylight Cloud", exact: true}).click();
  await page.getByText("Advanced settings & troubleshooting", {exact: true}).click();
  await page.getByText("Troubleshooting & developer settings", {exact: true}).click();
  await page.getByRole("button", {name: "Save service configuration", exact: true}).click();
  await expect.poll(() => page.evaluate(() => window.hostedFixture.calls.some(c => c.type.endsWith("/configure")))).toBe(true);
  expect(await page.evaluate(() => Object.hasOwn(window.hostedFixture.calls.filter(c => c.type.endsWith("/configure")).at(-1), "automatic_lifecycle"))).toBe(false);
  await page.getByRole("button", {name: "Enable automatic renewals", exact: true}).click();
  await expect.poll(() => page.evaluate(() => window.hostedFixture.state.automatic_lifecycle)).toBe(true);
  await expect(page.getByRole("button", {name: "Enable automatic renewals", exact: true})).toHaveCount(0);
});

test("repairing saved origins cannot silently override a legacy maintenance opt-out", async ({page}) => {
  await page.evaluate(() => {
    window.hostedFixture.state = {...window.hostedFixture.state, state: "configuration_repair",
      configured: false, api_origin: null, account_origin: null, automatic_lifecycle: false};
  });
  await page.getByRole("navigation", {name: "Daylight views"}).getByRole("button", {name: "Settings", exact: true}).click();
  await sections(page).getByRole("button", {name: "Daylight Cloud", exact: true}).click();
  await page.getByLabel("API origin", {exact: true}).fill("https://api.example.test");
  await page.getByLabel("Account origin", {exact: true}).fill("https://account.example.test");
  await page.getByRole("button", {name: "Save service configuration", exact: true}).click();
  await expect.poll(() => page.evaluate(() => window.hostedFixture.state.configured)).toBe(true);
  expect(await page.evaluate(() => window.hostedFixture.state.automatic_lifecycle)).toBe(false);
  expect(await page.evaluate(() => Object.hasOwn(window.hostedFixture.calls.find(c => c.type.endsWith("/configure")), "automatic_lifecycle"))).toBe(false);
});

test("reconnect uses concise confirmation and fresh transient owner approval", async ({page}) => {
  await openCloud(page);
  await page.getByText("Manage connection", {exact: true}).click();
  await expect(page.getByRole("checkbox", {name: "I renew my consent"})).toHaveCount(0);
  const nameField = page.getByLabel("New installation name", {exact: true});
  await nameField.fill("Kitchen");
  await page.getByLabel("API origin", {exact: true}).fill("https://unsaved.example.test");
  const beforeEnter = page.url();
  await nameField.press("Enter");
  await expect(page.getByText("Reconnect Home Assistant?", {exact: true})).toBeVisible();
  expect(await actions(page)).toEqual([]);
  await expect(page).toHaveURL(beforeEnter);
  await page.getByRole("button", {name: "Cancel", exact: true}).click();
  expect(await actions(page)).toEqual([]);
  await expect(nameField).toHaveValue("Kitchen");
  await expect(page.getByLabel("API origin", {exact: true})).toHaveValue("https://unsaved.example.test");
  await page.getByRole("button", {name: "Reconnect", exact: true}).click();
  await expect(nameField).toBeDisabled();
  await expect(page.getByText("Installation name: Kitchen", {exact: true})).toBeVisible();
  await page.getByRole("button", {name: "Agree & reconnect", exact: true}).click();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toBeVisible();
  await expect(page.getByRole("button", {name: "Check pairing approval", exact: true})).toBeEnabled();
  expect(await actions(page)).toEqual([{type: "daylight_calendar_import/hosted/action", entry_id: "entry-1", action: "reconnect", processing_consent: true, installation_label: "Kitchen", confirm_reconnect: true}]);
  await sections(page).getByRole("button", {name: "General", exact: true}).click();
  await sections(page).getByRole("button", {name: "Daylight Cloud", exact: true}).click();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toHaveCount(0);
  await page.getByText("Advanced settings & troubleshooting", {exact: true}).click();
  await page.getByText("Troubleshooting & developer settings", {exact: true}).click();
  await expect(page.getByRole("button", {name: "Check pairing approval", exact: true})).toBeEnabled();
  expect((await actions(page)).map(call => call.action)).toEqual(["reconnect"]);
});

test("pristine reconnect remains read-only and leads to first connection without reconfiguration", async ({page}) => {
  await page.evaluate(() => { window.hostedFixture.pristine = true; });
  await openCloud(page);
  await page.getByText("Manage connection", {exact: true}).click();
  await page.getByRole("button", {name: "Reconnect", exact: true}).click();
  await page.getByRole("button", {name: "Agree & reconnect", exact: true}).click();
  await expect(page.getByText("Connect Home Assistant to your Daylight account.", {exact: false})).toBeVisible();
  await expect(page.getByRole("button", {name: "Reconnect", exact: true})).toHaveCount(0);
  await consent(page).check();
  await page.getByRole("button", {name: "Start new connection", exact: true}).click();
  await expect(page.getByText(`Approval code: ${CODE}`, {exact: true})).toBeVisible();
  expect((await actions(page)).map(call => call.action)).toEqual(["reconnect", "connect"]);
});


test("mobile consent checkbox aligns with its text and the account link has breathing room", async ({page}) => {
  await openCloud(page);
  const consentBox = consent(page);
  const position = await consentBox.evaluate(input => {
    const left = input.getBoundingClientRect();
    const text = input.closest("label").querySelector("span").getBoundingClientRect();
    return {checkboxRight: left.right, textLeft: text.left,
      checkboxTop: left.top, textTop: text.top};
  });
  expect(position.checkboxRight).toBeLessThan(position.textLeft);
  expect(Math.abs(position.checkboxTop - position.textTop)).toBeLessThan(22);
  await page.evaluate(async () => {
    window.hostedFixture.state = {...window.hostedFixture.state, state: "connected",
      automatic_lifecycle: true, credential_status: "current", credential_expires_at: "2027-01-01T00:00:00Z",
      account_url: "https://account.example.test/account"};
    const panel = document.querySelector("daylight-import-panel");
    await panel._hostedSettings.load("entry-1");
  });
  const portal = page.getByRole("link", {name: "Open account portal", exact: true});
  await expect(portal).toBeVisible();
  expect(await portal.evaluate(a => parseFloat(getComputedStyle(a).marginTop))).toBeGreaterThanOrEqual(16);
  await expect(page.getByRole("checkbox", {name: "Automatically maintain this connection"})).toHaveCount(0);
});

test("guided approval completes automatically with advanced controls collapsed", async ({page}) => {
  await page.clock.install();
  await openCloud(page);
  await page.getByText("Advanced settings & troubleshooting", {exact: true}).click();
  await expect(page.getByLabel("API origin", {exact: true})).toBeHidden();
  await consent(page).check();
  await page.getByRole("button", {name: "Start new connection", exact: true}).click();
  await expect(page.getByText("Approval code: " + CODE, {exact: true})).toBeVisible();
  await page.evaluate(() => {
    const panel = document.querySelector("daylight-import-panel");
    const previous = panel._hass.callWS;
    panel.hass = {...panel._hass, callWS: async call => {
      if (call.action === "poll") window.hostedFixture.state.state = "candidate_saved";
      if (call.action === "reconcile") window.hostedFixture.state = {...window.hostedFixture.state, state: "connected", credential_status: "current"};
      return previous(call);
    }};
  });
  await page.clock.fastForward(5000);
  await expect.poll(async () => (await actions(page)).map(call => call.action)).toContain("poll");
  await page.clock.fastForward(1000);
  await expect(page.getByText("Your Home Assistant is securely connected to Daylight.", {exact: true})).toBeVisible();
  await expect(page.getByText("Approval code: " + CODE, {exact: true})).toHaveCount(0);
  await expect(page.getByRole("button", {name: "Renew credentials", exact: true})).toBeHidden();
  await expect(consent(page)).toHaveCount(0);
  expect(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth)).toBe(false);
  await assertNoStoredSecrets(page);
});

test("automatic checks keep Advanced drafts and caret while approval is pending", async ({page}) => {
  await page.clock.install();
  await openCloud(page);
  await consent(page).check();
  await page.getByRole("button", {name: "Start new connection", exact: true}).click();
  const origin = page.getByLabel("API origin", {exact: true});
  await origin.fill("https://draft-api.example.test");
  await origin.focus();
  await page.getByText("Manage connection", {exact: true}).click();
  const replacement = page.getByLabel("New installation name", {exact: true});
  await replacement.fill("Replacement draft");
  await replacement.focus();
  await replacement.evaluate(input => input.setSelectionRange(2, 8));
  await page.clock.fastForward(5000);
  await expect.poll(async () => (await actions(page)).map(call => call.action)).toContain("poll");
  await expect(origin).toHaveValue("https://draft-api.example.test");
  await expect(replacement).toHaveValue("Replacement draft");
  await expect(replacement).toBeFocused();
  expect(await replacement.evaluate(input => [input.selectionStart, input.selectionEnd])).toEqual([2, 8]);
});


for (const resumeState of ["pairing_wait", "candidate_saved", "reconnect_pending"]) {
  test(`returning to saved reconnect ${resumeState} finishes with no recovery clicks`, async ({page}) => {
    await page.clock.install();
    await openCloud(page);
    await sections(page).getByRole("button", {name: "General", exact: true}).click();
    await page.evaluate(state => {
      const fixture = window.hostedFixture;
      fixture.state = {...fixture.state, automatic_lifecycle: true, reconnect_in_progress: true, state};
      const panel = document.querySelector("daylight-import-panel");
      const previous = panel._hass.callWS;
      panel.hass = {...panel._hass, callWS: async call => {
        if (call.action === "poll") fixture.state.state = "candidate_saved";
        if (call.action === "reconcile") fixture.state = {...fixture.state, reconnect_in_progress: false, state: "connected", credential_status: "current"};
        return previous(call);
      }};
    }, resumeState);
    await sections(page).getByRole("button", {name: "Daylight Cloud", exact: true}).click();
    await expect(page.getByText("Approval code: " + CODE, {exact: true})).toHaveCount(0);
    await expect(page.getByRole("button", {name: "Start new connection", exact: true})).toHaveCount(0);
    if (resumeState !== "candidate_saved") {
      await expect(page.getByText(/Already approved/)).toBeVisible();
      await page.clock.fastForward(30000);
      await expect.poll(async () => (await actions(page)).map(call => call.action)).toContain("poll");
    }
    await page.clock.fastForward(1000);
    await expect(page.getByText("Your Home Assistant is securely connected to Daylight.", {exact: true})).toBeVisible();
    expect((await actions(page)).map(call => call.action)).toEqual(resumeState === "candidate_saved" ? ["reconcile"] : ["poll", "reconcile"]);
    await assertNoStoredSecrets(page);
  });
}
