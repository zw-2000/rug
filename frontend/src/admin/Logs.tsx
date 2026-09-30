import { useEffect, useState } from "react";
import { admin, type AuditRow, type QaRow } from "../api";
import { message } from "./useLoad";

const when = (s: string) => new Date(s).toLocaleString();

export function QaLog() {
  const [filter, setFilter] = useState<number | null>(null);
  const [rows, setRows] = useState<QaRow[]>([]);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");

  function load(before: number | null, replace: boolean) {
    admin
      .qa(filter, before)
      .then((r) => {
        setRows((prev) => (replace ? r : [...prev, ...r]));
        setMore(r.length === 25);
        setError("");
      })
      .catch((e) => setError(message(e)));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => load(null, true), [filter]);

  return (
    <div>
      <p className="muted">
        Every question people ask is kept here for {"the retention period (90 days by default)"}. Only administrators
        can see it. Treat it as confidential.
      </p>
      <div className="inline">
        <label>
          Show
          <select value={filter ?? ""} onChange={(e) => setFilter(e.target.value ? Number(e.target.value) : null)}>
            <option value="">all questions</option>
            <option value="-1">thumbs down</option>
            <option value="1">thumbs up</option>
          </select>
        </label>
        <a className="button" href="/api/admin/qa/export?feedback=-1">Export thumbs-down for the test set</a>
      </div>
      {error && <p className="error" role="alert">{error}</p>}
      <table>
        <thead><tr><th>When</th><th>Who</th><th>Question</th><th>Outcome</th><th>Rating</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{when(r.at)}</td>
              <td>{r.username}</td>
              <td>
                <details>
                  <summary>{r.question}</summary>
                  <p className="muted small">Answer ({r.mode || "n/a"}, {r.latency_ms ?? "?"} ms):</p>
                  <p className="pre">{r.answer || "—"}</p>
                  {r.comment && <p className="small">Comment: {r.comment}</p>}
                </details>
              </td>
              <td>{r.status}{r.ungrounded ? " · no citation" : ""}</td>
              <td>{r.feedback === 1 ? "👍" : r.feedback === -1 ? "👎" : ""}</td>
            </tr>
          ))}
          {rows.length === 0 && <tr><td colSpan={5} className="muted">Nothing yet.</td></tr>}
        </tbody>
      </table>
      {more && <button onClick={() => load(rows[rows.length - 1].id, false)}>Older</button>}
    </div>
  );
}

export function AuditLog() {
  const [action, setAction] = useState("");
  const [rows, setRows] = useState<AuditRow[]>([]);
  const [more, setMore] = useState(false);
  const [error, setError] = useState("");

  function load(before: number | null, replace: boolean) {
    admin
      .audit(action, before)
      .then((r) => {
        setRows((prev) => (replace ? r : [...prev, ...r]));
        setMore(r.length === 25);
        setError("");
      })
      .catch((e) => setError(message(e)));
  }
  // eslint-disable-next-line react-hooks/exhaustive-deps
  useEffect(() => load(null, true), [action]);

  return (
    <div>
      <p className="muted">Sign-ins, downloads, uploads and every administration change. It cannot be edited or deleted.</p>
      <label>
        Action
        <select value={action} onChange={(e) => setAction(e.target.value)}>
          <option value="">all</option>
          {["login.ok", "login.fail", "login.throttled", "logout", "download", "download.forbidden", "upload",
            "upload.forbidden", "upload.rejected", "admin.group_folders", "admin.override", "admin.doc_type",
            "admin.user_disabled", "admin.user_enabled", "admin.revoke_sessions", "admin.scan"].map((a) => (
            <option key={a} value={a}>{a}</option>
          ))}
        </select>
      </label>
      {error && <p className="error" role="alert">{error}</p>}
      <table>
        <thead><tr><th>When</th><th>Who</th><th>Action</th><th>Target</th><th>Detail</th><th>Address</th></tr></thead>
        <tbody>
          {rows.map((r) => (
            <tr key={r.id}>
              <td>{when(r.at)}</td>
              <td>{r.actor}</td>
              <td>{r.action}</td>
              <td>{r.target}</td>
              <td className="small">{Object.keys(r.detail).length ? JSON.stringify(r.detail) : ""}</td>
              <td>{r.ip ?? ""}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {more && <button onClick={() => load(rows[rows.length - 1].id, false)}>Older</button>}
    </div>
  );
}
