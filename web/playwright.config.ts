import { defineConfig } from "@playwright/test";

const PLAYWRIGHT_BASE_URL = process.env.PLAYWRIGHT_BASE_URL || "http://localhost:60889";
const SHOULD_START_WEBSERVER = !process.env.PLAYWRIGHT_BASE_URL;

export default defineConfig({
  testDir: "./tests",
  fullyParallel: true,
  forbidOnly: !!process.env.CI,
  retries: process.env.CI ? 2 : 0,
  workers: process.env.CI ? 1 : undefined,
  reporter: "html",
  use: {
    baseURL: PLAYWRIGHT_BASE_URL,
    trace: "on-first-retry",
  },
  ...(SHOULD_START_WEBSERVER
    ? {
        webServer: {
          command: "npm run dev",
          url: PLAYWRIGHT_BASE_URL,
          reuseExistingServer: !process.env.CI,
        },
      }
    : {}),
  projects: [
    {
      name: "chromium",
      use: { browserName: "chromium" },
    },
  ],
});
