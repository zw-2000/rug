import { useState, type FormEvent } from "react";
import { ApiError, login, type Me } from "./api";

export default function Login({ onSignedIn, notice }: { onSignedIn: (me: Me) => void; notice: string }) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      onSignedIn(await login(username, password));
    } catch (err) {
      setError(
        err instanceof ApiError && err.status === 429
          ? "Too many failed attempts. Wait a few minutes and try again."
          : err instanceof ApiError
            ? err.message
            : "Could not reach the server.",
      );
      setPassword("");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="login">
      <form onSubmit={submit} aria-labelledby="login-title">
        <h1 id="login-title">Sign in</h1>
        <p className="muted">Use your company account.</p>
        {notice && <p className="banner">{notice}</p>}
        <label>
          Username
          <input
            value={username}
            onChange={(e) => setUsername(e.target.value)}
            autoComplete="username"
            autoFocus
            required
          />
        </label>
        <label>
          Password
          <input
            type="password"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            autoComplete="current-password"
            required
          />
        </label>
        {error && (
          <p className="error" role="alert">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy || !username || !password}>
          {busy ? "Signing in…" : "Sign in"}
        </button>
      </form>
    </main>
  );
}
