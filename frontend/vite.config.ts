import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

// The dashboard only reads JSON from the backend. It never holds an exchange or LLM key.
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      "/api": "http://127.0.0.1:8000",
      "/health": "http://127.0.0.1:8000",
    },
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
  },
});
