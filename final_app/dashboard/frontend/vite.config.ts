/// <reference types="vitest" />
import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: { port: 7001, proxy: { '/api': 'http://10.7.11.24:7000' } },
  test: { environment: 'jsdom', globals: true, setupFiles: './src/setupTests.ts' },
})
