import { useState, type FormEvent } from "react";
import { admin } from "../api";
import { message, useLoad } from "./useLoad";

export default function DocTypes() {
  const { data, error, reload } = useLoad(admin.docTypes);
  const [name, setName] = useState("");
  const [phrases, setPhrases] = useState("");
  const [problem, setProblem] = useState("");

  async function save(n: string, p: string) {
    setProblem("");
    try {
      await admin.putDocType(n, p.split(",").map((x) => x.trim()));
      setName("");
      setPhrases("");
      reload();
    } catch (e) {
      setProblem(message(e));
    }
  }

  async function remove(n: string) {
    setProblem("");
    try {
      await admin.deleteDocType(n);
      reload();
    } catch (e) {
      setProblem(message(e));
    }
  }

  if (error) return <p className="error" role="alert">{error}</p>;
  if (!data) return <p className="muted">Loading…</p>;
  return (
    <div>
      <p className="muted">
        Words that mean a kind of document (SOW, change request, MSA, …). When a question or a file name
        contains one, documents of that type rank first. Separate the words or phrases with commas. If this
        list is ever empty the built-in defaults are used.
      </p>
      {problem && <p className="error" role="alert">{problem}</p>}
      <table>
        <thead><tr><th>Type</th><th>Words and phrases</th><th /></tr></thead>
        <tbody>
          {Object.entries(data).map(([n, p]) => (
            <tr key={n}>
              <td>{n}</td>
              <td>{p.join(", ")}</td>
              <td className="row-actions">
                <button className="link" onClick={() => { setName(n); setPhrases(p.join(", ")); }}>Edit</button>
                <button className="link" onClick={() => remove(n)}>Delete</button>
              </td>
            </tr>
          ))}
          {Object.keys(data).length === 0 && <tr><td colSpan={3} className="muted">Using the built-in defaults.</td></tr>}
        </tbody>
      </table>
      <form className="inline" onSubmit={(e: FormEvent) => { e.preventDefault(); void save(name, phrases); }}>
        <label>Type name<input value={name} onChange={(e) => setName(e.target.value)} placeholder="LEASE" required /></label>
        <label>Words and phrases<input value={phrases} onChange={(e) => setPhrases(e.target.value)} placeholder="lease, tenancy agreement" required /></label>
        <button type="submit">Save type</button>
      </form>
    </div>
  );
}
