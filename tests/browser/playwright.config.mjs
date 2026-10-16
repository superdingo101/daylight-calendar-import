import {defineConfig, devices} from "@playwright/test";

export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.mjs",
  forbidOnly: Boolean(process.env.CI),
  retries: 0,
  use: {baseURL: "http://127.0.0.1:4179", trace: "retain-on-failure",
    launchOptions: process.env.BROWSER_EXECUTABLE ? {executablePath: process.env.BROWSER_EXECUTABLE} : {}},
  projects: [
    {name: "desktop", use: {...devices["Desktop Chrome"]}},
    {name: "mobile", use: {...devices["Pixel 7"]}},
  ],
  webServer: {
    command: "node server.mjs",
    url: "http://127.0.0.1:4179",
    reuseExistingServer: false,
  },
});
