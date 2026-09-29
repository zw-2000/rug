import { defineConfig } from "@playwright/test";

const PORT = 8765;
const python = process.env.RUG_PYTHON ?? "../backend/.venv/bin/python";
const database =
  process.env.RUG_E2E_DATABASE_URL ?? "postgresql+psycopg://rug:rug@localhost:5432/rug_e2e";

// The server is the real API plus the built frontend, with a mock directory and FAKE models
// (see backend/eval/e2e_server.py). These tests prove the plumbing, not answer quality.
export default defineConfig({
  testDir: "e2e",
  workers: 1,
  fullyParallel: false,
  timeout: 30_000,
  retries: 0,
  reporter: [["list"]],
  use: { baseURL: `http://127.0.0.1:${PORT}`, trace: "retain-on-failure" },
  webServer: {
    command: `${python} -m eval.e2e_server --port ${PORT} --static ../frontend/dist --docs .e2e-docs`,
    cwd: "../backend",
    url: `http://127.0.0.1:${PORT}/healthz`,
    timeout: 120_000,
    reuseExistingServer: false,
    env: { RUG_E2E_DATABASE_URL: database },
  },
});
