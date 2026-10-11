import { defineConfig } from '@playwright/test';
export default defineConfig({
  testDir: './tests/browser',
  workers: 1,
  timeout: 60000,
  use: { baseURL: process.env.BASE_URL || 'http://localhost:4103', headless: true,
    launchOptions: process.env.CHROMIUM_PATH ? { executablePath: process.env.CHROMIUM_PATH } : {} },
  webServer: process.env.BASE_URL ? undefined : {
    command: '.venv/bin/python scripts/test_server.py',
    url: 'http://localhost:4103/healthz', reuseExistingServer: false, timeout: 30000,
  },
});
