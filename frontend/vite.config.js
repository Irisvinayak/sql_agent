import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        // Must match the backend port in api/main.py. Both places have to move
        // together — changing only one just relocates the 404.
        //
        // 8010 because the two obvious ports are both taken on this machine by
        // OTHER projects, and each collision presented as a mystery 404 rather
        // than a connection error (the squatter answers /api/query with 404 and
        // Vite forwards it verbatim):
        //   8000 — Docker Desktop publishes ANCHOR-MT there
        //   8001 — Desktop\Chat-SystemWorking\dev_server.py ("Report Assistant")
        target: 'http://localhost:8010',
        changeOrigin: true,
      },
    },
  },
})
