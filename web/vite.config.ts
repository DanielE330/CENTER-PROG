import react from "@vitejs/plugin-react";
import { defineConfig } from "vite";

// In dev, /api and /auth go to the running stack (make up); the browser keeps one origin.
export default defineConfig({
  plugins: [react()],
  build: { sourcemap: false, chunkSizeWarningLimit: 900 },
  server: {
    proxy: {
      "/api": { target: "https://localhost:8443", secure: false, changeOrigin: false },
      "/auth": { target: "https://localhost:8443", secure: false, changeOrigin: false },
    },
  },
});
