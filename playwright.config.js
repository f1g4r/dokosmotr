const { defineConfig } = require('@playwright/test');
module.exports = defineConfig({
  testDir: './tests/browser',
  timeout: 30000,
  fullyParallel: false,
  use: {
    baseURL: process.env.TEST_URL || 'http://127.0.0.1:5080',
    browserName: 'chromium',
    channel: process.env.TEST_BROWSER_CHANNEL || undefined,
    headless: true,
    screenshot: 'only-on-failure',
    trace: 'retain-on-failure',
  },
  reporter: 'list',
  workers: 1,
});
