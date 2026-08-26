import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import tailwindcss from "@tailwindcss/vite";

// Dev server proxies /api to the FastAPI engine:
//   uvicorn cosmaneura_billing.api:app --port 8000
export default defineConfig(({ mode }) => ({
  // The one-command demo sanitizes its process environment and uses this mode
  // to prevent Vite from loading developer-owned .env files into the bundle.
  envDir: mode === "local-demo" ? false : undefined,
  plugins: [react(), tailwindcss()],
  server: {
    proxy: {
      "/api": { target: "http://127.0.0.1:8000", changeOrigin: true },
    },
  },
}));
