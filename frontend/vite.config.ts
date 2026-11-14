import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// Dev server proxies to the backend on 127.0.0.1:8384 so cookies/auth
// work same-origin. The backend itself binds localhost-only by default.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": { target: "http://127.0.0.1:8384", changeOrigin: false },
      "/ws": { target: "ws://127.0.0.1:8384", ws: true },
    },
  },
  build: { outDir: "../backend/app/static", emptyOutDir: true, sourcemap: false },
});
