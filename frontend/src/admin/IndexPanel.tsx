import { useEffect, useState } from "react";
import { admin } from "../api";
import { message, useLoad } from "./useLoad";

const when = (s: string | null) => (s ? new Date(s).toLocaleString() : "—");

export default function IndexPanel() {
  const { data, error, reload } = useLoad(admin.index);
  const [problem, setProblem] = useState("");
  const scanning = data?.scanning ?? false;

  useEffect(() => {
    const t = window.setInterval(reload, scanning ? 3000 : 15000);
    return () => window.clearInterval(t);
  }, [reload, scanning]);

  async function scan() {
    setProblem("");
    try {
      await admin.scan();
      window.setTimeout(reload, 500);
    } catch (e) {
      setProblem(message(e));
    }
  }

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  return (
    <div>
      <p>
        <strong>{data.documents.ok}</strong> documents searchable
        {data.documents.error > 0 && <>, <strong className="error">{data.documents.error}</strong> could not be read</>};{" "}
        <strong>{data.summaries_pending}</strong> waiting for an overview. The index is refreshed automatically every{" "}
        {Math.round(data.scan_interval_s / 60)} minutes.
      </p>
      <p>
        <button onClick={scan} disabled={scanning}>{scanning ? "Scanning…" : "Scan now"}</button>
      </p>
      {problem && <p className="error" role="alert">{problem}</p>}

      <h2>Recent scans</h2>
      <table>
        <thead><tr><th>Started</th><th>Finished</th><th>Files</th><th>Result</th><th>Problems</th></tr></thead>
        <tbody>
          {data.runs.map((r) => (
            <tr key={r.id}>
              <td>{when(r.started_at)}</td>
              <td>{r.finished_at ? when(r.finished_at) : `running (${r.processed}/${r.total})`}</td>
              <td>{r.total}</td>
              <td>{Object.entries(r.counts).map(([k, v]) => `${k} ${v}`).join(", ") || "—"}</td>
              <td>{r.n_errors}</td>
            </tr>
          ))}
          {data.runs.length === 0 && <tr><td colSpan={5} className="muted">No scan has run yet.</td></tr>}
        </tbody>
      </table>

      {data.broken.length > 0 && (
        <>
          <h2>Files that could not be read</h2>
          <ul>
            {data.broken.map((b) => (
              <li key={b.path}><code>{b.path}</code>: {b.error}</li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}
