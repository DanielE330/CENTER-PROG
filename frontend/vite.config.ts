import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'

export default defineConfig({
  plugins: [react(), tailwindcss()],
  server: {
    port: 5173,
    // The SPA talks to the backend on the same origin: /api goes to the api service.
    proxy: { '/api': 'http://localhost:3000' },
  },
  build: { chunkSizeWarningLimit: 1500 },
})
