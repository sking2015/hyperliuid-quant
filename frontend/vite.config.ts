import { defineConfig } from 'vitest/config'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  test: { include: ['src/**/*.test.ts'] },
  server: { proxy: { '/api': { target: 'http://127.0.0.1:8000', ws: true } } },
})

