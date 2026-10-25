import {test, expect} from "@playwright/test";
import {fileURLToPath} from "node:url";
import {readFile} from "node:fs/promises";
import {createHash} from "node:crypto";

const textForm = page => page.getByRole("region", {name: "Import pasted event details"});
const fileForm = page => page.getByRole("region", {name: "Import an image or PDF"});
const configure = (page, values) => page.evaluate(values => Object.assign(window.fixture, values), values);
const calls = page => page.evaluate(() => window.fixture.calls.filter(call => call.service.startsWith("submit_")));
const source = "School picnic October 20, 2026, 3–4pm";
const openImport = async (page, method) => {
  await page.getByRole("button", {name: "+ New import", exact: true}).click();
  await page.getByRole("button", {name: method === "text" ? "Paste event details" : "Upload image or PDF", exact: false}).click();
};

const pageErrors = new WeakMap();
test.beforeEach(async ({page}) => {
  const errors = [];
  pageErrors.set(page, errors);
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByText("No imports awaiting review.")).toBeVisible();
  await expect(page.getByRole("heading", {name: "Daylight Imports"})).toBeVisible();
  await expect(page.getByRole("button", {name: "+ New import"})).toBeVisible();
  await expect(page.getByRole("button", {name: "Refresh"})).toBeVisible();
  await expect(textForm(page)).toBeHidden();
  await expect(fileForm(page)).toBeHidden();
});
test.afterEach(async ({page}) => {
  expect(pageErrors.get(page)).toEqual([]);
});

test("header import action is available from activity, and Back restores the previous view", async ({page}) => {
  await page.getByRole("navigation", {name: "Daylight views"}).getByRole("button", {name: "Recent activity"}).click();
  await expect(page.getByText("No recent activity.", {exact: true})).toBeVisible();
  await openImport(page, "text");
  await expect(page.getByRole("heading", {name: "New import"})).toBeVisible();
  await expect(page.getByRole("navigation", {name: "Daylight views"})).toBeHidden();
  await page.getByRole("button", {name: "Back", exact: false}).click();
  await expect(page.getByText("No recent activity.", {exact: true})).toBeVisible();
  await expect(page.getByRole("button", {name: "+ New import"})).toBeVisible();
  await expect(page.getByRole("button", {name: "Refresh"})).toBeVisible();
});

test("empty paste/file validation and keyboard submission, loading, and review navigation", async ({page}) => {
  await openImport(page, "text");
  const text = textForm(page);
  const file = fileForm(page);
  await expect(text.getByText("Email setup is optional.", {exact: false})).toBeVisible();
  await text.getByRole("button", {name: "Import events", exact: true}).click();
  await expect(text.getByRole("textbox", {name: "Event details"})).toBeFocused();
  await expect(text.getByRole("status")).toContainText("Paste event details before importing.");
  await page.getByRole("button", {name: "Choose another method"}).click();
  await page.getByRole("button", {name: "Upload image or PDF"}).click();
  await file.getByRole("button", {name: "Import file", exact: true}).click();
  await expect(file.getByLabel("Image or PDF file")).toBeFocused();
  await page.getByRole("button", {name: "Choose another method"}).click();
  await page.getByRole("button", {name: "Paste event details", exact: false}).click();
  expect(await calls(page)).toEqual([]);

  await configure(page, {held: true});
  const input = text.getByRole("textbox", {name: "Event details"});
  await input.fill(source);
  await input.press("Tab");
  await expect(text.getByRole("button", {name: "Import events", exact: true})).toBeFocused();
  await page.keyboard.press("Enter");
  await expect(text.getByRole("button", {name: "Processing…", exact: true})).toBeDisabled();
  await expect(input).toBeDisabled();
  await expect(text.getByRole("status")).toBeFocused();
  await expect(text.getByRole("status")).toContainText("Extracting events for review");
  await expect.poll(() => calls(page).then(result => result.length)).toBe(1);
  await page.evaluate(() => window.fixture.release());
  await expect(page.getByRole("heading", {name: "School picnic", exact: true})).toBeVisible();
  const requests = await calls(page);
  expect(requests[0]).toMatchObject({service: "submit_text", service_data: {text: source}, return_response: true});
  await expect(page.getByRole("heading", {name: "School picnic", exact: true})).toBeVisible();
  await expect(page.getByRole("heading", {name: "Manual source", exact: true})).toBeFocused();
  await expect(text).toBeHidden();
});

for (const [filename, service, mimeType] of [
  ["media-screenshot.png", "submit_image", "image/png"],
  ["media-multipage-text.pdf", "submit_pdf", "application/pdf"],
]) {
  test(`native file input uploads ${filename} and opens review`, async ({page}) => {
    await openImport(page, "file");
    const form = fileForm(page);
    await configure(page, {held: true});
    const upload = page.waitForRequest(request => request.url().endsWith("/api/file_upload") && request.method() === "POST");
    await form.getByLabel("Image or PDF file").setInputFiles(fileURLToPath(new URL(`../fixtures/extraction/assets/${filename}`, import.meta.url)));
    await expect(form.getByText(`Selected file: ${filename}`, {exact: true})).toBeVisible();
    await form.getByRole("textbox", {name: "Supporting text (optional)"}).fill("Family calendar");
    await form.getByRole("button", {name: "Import file", exact: true}).click();
    const request = await upload;
    expect(request.headers()["content-type"]).toContain("multipart/form-data; boundary=");
    await expect.poll(() => page.evaluate(() => window.fixture.uploads.length)).toBe(1);
    const [received] = await page.evaluate(() => window.fixture.uploads);
    const bytes = await readFile(new URL(`../fixtures/extraction/assets/${filename}`, import.meta.url));
    expect(received.headers).toContain(`filename="${filename}"`);
    expect(received.headers).toContain(mimeType);
    expect(received.size).toBe(bytes.length);
    expect(received.sha256).toBe(createHash("sha256").update(bytes).digest("hex"));
    await expect(form.getByRole("status")).toContainText("Extracting events for review");
    await expect(form.getByLabel("Image or PDF file")).toBeDisabled();
    await expect.poll(() => calls(page).then(result => result.length)).toBe(1);
    expect((await calls(page))[0]).toMatchObject({service, service_data: {file_id: "a".repeat(32), text: "Family calendar"}});
    await page.evaluate(() => window.fixture.release());
    await expect(page.getByRole("heading", {name: "School picnic", exact: true})).toBeVisible();
    await expect(page.getByRole("heading", {name: "School picnic", exact: true})).toBeVisible();
  });
}

test("duplicate sources keep the draft and do not offer nonexistent review", async ({page}) => {
  await configure(page, {mode: "duplicate"});
  await openImport(page, "text");
  const form = textForm(page);
  await form.getByRole("textbox").fill(source);
  await form.getByRole("button", {name: "Import events", exact: true}).click();
  await expect(form.getByRole("status")).toContainText("This source was already submitted");
  await expect(form.getByRole("textbox")).toHaveValue(source);
  await expect(form.getByRole("button", {name: "Review events", exact: true})).toHaveCount(0);
  await expect(page.getByRole("heading", {name: "New import"})).toBeVisible();
});

for (const mode of ["failure", "disconnect", "malformed"]) {
  test(`${mode} preserves the draft, offers activity, and safely retries`, async ({page}) => {
    await configure(page, {mode});
    await openImport(page, "text");
    const form = textForm(page);
    await form.getByRole("textbox").fill(source);
    await form.getByRole("button", {name: "Import events", exact: true}).click();
    await expect(form.getByRole("status")).toContainText(mode === "failure" ? "Check the configured AI task entity" : "Check recent activity");
    await expect(form.getByRole("textbox")).toBeEnabled();
    await expect(form.getByRole("textbox")).toHaveValue(source);
    await form.getByRole("button", {name: "Check recent activity", exact: true}).click();
    await expect(page.getByText("No recent activity.", {exact: true})).toBeVisible();
    await expect(page.getByRole("navigation", {name: "Daylight views"}).getByRole("button", {name: "Recent activity", exact: true})).toBeDisabled();
    await page.getByRole("navigation", {name: "Daylight views"}).getByRole("button", {name: "Review inbox", exact: true}).click();
    await openImport(page, "text");
    await expect(form.getByRole("textbox")).toHaveValue(source);
    await configure(page, {mode: "success"});
    await form.getByRole("button", {name: "Import events", exact: true}).click();
    await expect(page.getByRole("heading", {name: "School picnic", exact: true})).toBeVisible();
    const requests = await calls(page);
    expect(requests).toHaveLength(2);
    expect(requests[0].service_data.source_id).toEqual(expect.any(String));
    expect(requests[0].service_data.source_id.trim()).not.toBe("");
    expect(requests[1].service_data.source_id).toBe(requests[0].service_data.source_id);
  });
}

test("invalid files stay local and mobile layout keeps controls within viewport", async ({page}) => {
  await openImport(page, "file");
  const uploadRequests = [];
  page.on("request", request => {
    if (new URL(request.url()).pathname === "/api/file_upload") uploadRequests.push(request.url());
  });
  const form = fileForm(page);
  await form.getByLabel("Image or PDF file").setInputFiles({name: "not-a-poster.png", mimeType: "image/png", buffer: Buffer.from("not an image")});
  await expect(form.getByRole("status")).toContainText("Choose a JPEG, PNG, WebP image or PDF.");
  await form.getByRole("button", {name: "Import file", exact: true}).click();
  await expect(form.getByRole("button", {name: "Import file", exact: true})).toBeEnabled();
  expect(await calls(page)).toEqual([]);
  expect(await page.evaluate(() => window.fixture.uploads)).toEqual([]);
  expect(uploadRequests).toEqual([]);
  for (const control of await page.locator("input:visible, textarea:visible, .manual-submission button:visible, .toolbar-new:visible").all()) {
    const box = await control.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize().width);
    expect(box.height).toBeGreaterThanOrEqual(44);
  }
});

test("upload rejection retains selection and does not call extraction", async ({page}) => {
  await openImport(page, "file");
  await page.route("**/api/file_upload", route => route.fulfill({status: 413, body: "Too large"}));
  const form = fileForm(page);
  const selected = form.getByLabel("Image or PDF file");
  await selected.setInputFiles(fileURLToPath(new URL("../fixtures/extraction/assets/media-screenshot.png", import.meta.url)));
  await form.getByRole("button", {name: "Import file", exact: true}).click();
  await expect(form.getByRole("status")).toContainText("Home Assistant rejected the file size");
  await expect(selected).toBeEnabled();
  await expect(form.getByText("Selected file: media-screenshot.png", {exact: true})).toBeVisible();
  expect(await calls(page)).toEqual([]);
});


test("automatic calendar observations precede explicit advisory approval", async ({page}) => {
  await openImport(page, "text");
  const form = textForm(page);
  await form.getByRole("textbox").fill(source);
  await form.getByRole("button", {name: "Import events", exact: true}).click();
  await expect(page.getByRole("heading", {name: "School picnic", exact: true})).toBeVisible();
  await configure(page, {holdObservation: true});
  await page.getByRole("button", {name: "Approve School picnic", exact: true}).click();
  const confirm = page.getByRole("button", {name: "Confirm approve: School picnic", exact: true});
  await expect(confirm).toBeDisabled();
  await expect(page.getByRole("button", {name: "Cancel", exact: true})).toBeFocused();
  await expect(page.getByRole("region", {name: "Calendar check results"})).toContainText("Checking selected calendars");
  expect(await page.evaluate(() => window.fixture.calls.filter(call => call.service === "approve_pending_event"))).toEqual([]);
  await page.getByRole("button", {name: "Cancel", exact: true}).click();
  await page.getByRole("button", {name: "Approve School picnic", exact: true}).click();
  await expect(confirm).toBeEnabled();
  await expect(page.getByRole("region", {name: "Calendar check results"})).toContainText("previous calendar check is still running");
  expect(await page.evaluate(() => window.fixture.calls.filter(call => call.service === "check_pending_event").length)).toBe(1);
  await page.evaluate(() => window.fixture.releaseObservation());
  await page.getByRole("button", {name: "Cancel", exact: true}).click();
  await configure(page, {holdObservation: false});
  await page.getByRole("button", {name: "Approve School picnic", exact: true}).click();
  await expect(confirm).toBeEnabled();
  await expect(page.getByRole("region", {name: "Calendar check results"})).toContainText("Scheduling conflict: Dentist (calendar.work)");
  await expect(page.getByRole("button", {name: "Cancel", exact: true})).toBeFocused();
  // A new approval attempt must get a fresh check; an unavailable calendar
  // remains advisory only after its incomplete result has been shown.
  await page.getByRole("button", {name: "Cancel", exact: true}).click();
  await configure(page, {holdObservation: false, observationError: "Calendar check incomplete: Calendar unavailable"});
  await page.getByRole("button", {name: "Approve School picnic", exact: true}).click();
  await expect(page.getByRole("region", {name: "Calendar check results"})).toContainText("Calendar check incomplete: Calendar unavailable");
  await expect(confirm).toBeEnabled();
  expect(await page.evaluate(() => window.fixture.calls.filter(call => call.service === "check_pending_event").length)).toBe(3);
  expect(await page.evaluate(() => window.fixture.calls.filter(call => call.service === "approve_pending_event"))).toEqual([]);
  await confirm.click();
  await expect(page.getByText("No imports awaiting review.")).toBeVisible();
  expect(await page.evaluate(() => window.fixture.calls.filter(call => call.service === "approve_pending_event").length)).toBe(1);
});

test("multi-event import is compact, source-grouped, and expands individual controls", async ({page}) => {
  await page.evaluate(() => {
    const base = window.fixture.pending.events[0];
    window.fixture.pending.events = [
      base,
      {...base, id: "event-2", title: "Wear Red Day", start: "2026-10-26",
        end: "2026-10-27", all_day: true, status: "pending",
        date_time_assumptions: ["Year assumed 2026"]},
    ];
  });
  await openImport(page, "text");
  await textForm(page).getByRole("textbox").fill(source);
  await textForm(page).getByRole("button", {name: "Import events", exact: true}).click();
  await expect(page.getByText("2 events in this import · 2 awaiting review")).toBeVisible();
  await expect(page.getByRole("region", {name: "Events from this import"})).toBeVisible();
  await expect(page.getByRole("button", {name: "Approve all 2"})).toBeVisible();
  await expect(page.getByRole("button", {name: "Reject all 2"})).toBeVisible();
  const folds = page.locator(".event-fold");
  await expect(folds).toHaveCount(2);
  await expect(folds.nth(1)).not.toHaveAttribute("open");
  await expect(page.getByRole("button", {name: "Approve Wear Red Day"})).toBeHidden();
  await folds.nth(1).locator("summary").first().click();
  await expect(page.getByRole("button", {name: "Approve Wear Red Day"})).toBeVisible();
  await expect(page.getByText("Year inferred — verify the date")).toBeVisible();
  await page.getByRole("button", {name: "Refresh"}).click();
  await expect(page.getByRole("button", {name: "Approve Wear Red Day"})).toBeVisible();
  await expect(page.getByRole("button", {name: "Approve School picnic"})).toBeHidden();
});
