import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        // Must match the backend port in api/main.py (default 8001 — Docker
        // publishes an unrelated service on 8000 on this machine).
        target: 'http://localhost:8001',
        changeOrigin: true,
      },
    },
  },
})
