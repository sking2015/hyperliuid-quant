import { defineConfig } from '@playwright/test'

export default defineConfig({
  testDir: './e2e',
  timeout: 45_000,
  use: {
    actionTimeout: 7_000,
    baseURL: process.env.M1_BASE_URL ?? 'http://127.0.0.1:5173',
    browserName: 'chromium',
    channel: process.env.M1_BROWSER_CHANNEL,
    screenshot: 'only-on-failure',
  },
})

