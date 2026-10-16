import {test, expect} from "@playwright/test";
import {fileURLToPath} from "node:url";
import {readFile} from "node:fs/promises";
import {createHash} from "node:crypto";

const textForm = page => page.getByRole("region", {name: "Import pasted event details"});
const fileForm = page => page.getByRole("region", {name: "Import an image or PDF"});
const configure = (page, values) => page.evaluate(values => Object.assign(window.fixture, values), values);
const calls = page => page.evaluate(() => window.fixture.calls.filter(call => call.service.startsWith("submit_")));
const source = "School picnic October 20, 2026, 3–4pm";

const pageErrors = new WeakMap();
test.beforeEach(async ({page}) => {
  const errors = [];
  pageErrors.set(page, errors);
  page.on("pageerror", error => errors.push(error.message));
  await page.goto("/");
  await expect(page.getByText("No imports awaiting review.")).toBeVisible();
  await expect(textForm(page).getByText("Email setup is optional.", {exact: false})).toBeVisible();
});
test.afterEach(async ({page}) => {
  expect(pageErrors.get(page)).toEqual([]);
});

test("empty paste/file validation and keyboard submission, loading, and review navigation", async ({page}) => {
  const text = textForm(page);
  const file = fileForm(page);
  await text.getByRole("button", {name: "Import events", exact: true}).click();
  await expect(text.getByRole("textbox", {name: "Event details"})).toBeFocused();
  await expect(text.getByRole("status")).toContainText("Paste event details before importing.");
  await file.getByRole("button", {name: "Import file", exact: true}).click();
  await expect(file.getByLabel("Image or PDF file")).toBeFocused();
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
  await expect(text.getByRole("status")).toContainText("1 event is ready for review");
  await expect(input).toHaveValue(source);
  const requests = await calls(page);
  expect(requests[0]).toMatchObject({service: "submit_text", service_data: {text: source}, return_response: true});
  await text.getByRole("button", {name: "Review events", exact: true}).click();
  await expect(page.getByRole("heading", {name: "School picnic", exact: true})).toBeVisible();
  await expect(page.getByRole("heading", {name: "Manual source", exact: true})).toBeFocused();
  await expect(text).toBeHidden();
});

for (const [filename, service, mimeType] of [
  ["media-screenshot.png", "submit_image", "image/png"],
  ["media-multipage-text.pdf", "submit_pdf", "application/pdf"],
]) {
  test(`native file input uploads ${filename} and opens review`, async ({page}) => {
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
    await expect(form.getByRole("status")).toContainText("1 event is ready for review");
    await expect(form.getByLabel("Image or PDF file")).toBeEnabled();
    await form.getByRole("button", {name: "Review events", exact: true}).click();
    await expect(page.getByRole("heading", {name: "School picnic", exact: true})).toBeVisible();
  });
}

test("duplicate sources keep the draft and do not offer nonexistent review", async ({page}) => {
  await configure(page, {mode: "duplicate"});
  const form = textForm(page);
  await form.getByRole("textbox").fill(source);
  await form.getByRole("button", {name: "Import events", exact: true}).click();
  await expect(form.getByRole("status")).toContainText("This source was already submitted");
  await expect(form.getByRole("textbox")).toHaveValue(source);
  await expect(form.getByRole("button", {name: "Review events", exact: true})).toHaveCount(0);
  await expect(page.getByText("No imports awaiting review.")).toBeVisible();
});

for (const mode of ["failure", "disconnect", "malformed"]) {
  test(`${mode} preserves the draft, offers activity, and safely retries`, async ({page}) => {
    await configure(page, {mode});
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
    await expect(form.getByRole("textbox")).toHaveValue(source);
    await configure(page, {mode: "success"});
    await form.getByRole("button", {name: "Import events", exact: true}).click();
    await expect(form.getByRole("status")).toContainText("1 event is ready for review");
    const requests = await calls(page);
    expect(requests).toHaveLength(2);
    expect(requests[0].service_data.source_id).toEqual(expect.any(String));
    expect(requests[0].service_data.source_id.trim()).not.toBe("");
    expect(requests[1].service_data.source_id).toBe(requests[0].service_data.source_id);
  });
}

test("invalid files stay local and mobile layout keeps controls within viewport", async ({page}) => {
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
  for (const control of await page.locator("input, textarea, .manual-submission button").all()) {
    const box = await control.boundingBox();
    expect(box.x).toBeGreaterThanOrEqual(0);
    expect(box.x + box.width).toBeLessThanOrEqual(page.viewportSize().width);
    expect(box.height).toBeGreaterThanOrEqual(44);
  }
});

test("upload rejection retains selection and does not call extraction", async ({page}) => {
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
