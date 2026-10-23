import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// In dev, /api proxies to the backend. In Docker, nginx does the same job.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { '/api': 'http://localhost:8000' } },
})
