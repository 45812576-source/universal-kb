import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./e2e",
  use: {
    baseURL: "http://localhost:5174",
    headless: true,
    screenshot: "only-on-failure",
  },
  webServer: {
    command: "PORT=5174 npm run build && PORT=5174 npm run start",
    url: "http://localhost:5174",
    reuseExistingServer: true,
    timeout: 120_000,
  },
});
