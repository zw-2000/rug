import react from "@vitejs/plugin-react";
import { defineConfig } from "vitest/config";

// In development the API runs separately (`rug serve`); production serves dist/ from the API.
export default defineConfig({
  plugins: [react()],
  server: { proxy: { "/api": "http://127.0.0.1:8000" } },
  build: { sourcemap: false },
  test: { include: ["src/**/*.test.ts"] },
});
