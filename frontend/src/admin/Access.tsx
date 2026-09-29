import { useState, type FormEvent } from "react";
import { admin, type AdminConfig } from "../api";
import { message, useLoad } from "./useLoad";

const label = (f: string) => (f === "" ? "(root)" : f);

export default function Access() {
  const { data, error, reload } = useLoad(admin.config);
  const [notice, setNotice] = useState("");
  const [problem, setProblem] = useState("");

  async function run(fn: () => Promise<unknown>, ok: string) {
    setProblem("");
    setNotice("");
    try {
      await fn();
      setNotice(ok);
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
        A user sees the folders of their Active Directory groups, plus any &quot;allow&quot; exceptions, minus any
        &quot;deny&quot; exceptions (deny always wins). Changes here apply to the user&apos;s next request; changes to a
        person&apos;s AD groups apply when they next sign in. Being an administrator grants no documents by itself.
      </p>
      {notice && <p className="ok" role="status">{notice}</p>}
      {problem && <p className="error" role="alert">{problem}</p>}

      <h2>Groups and folders</h2>
      {Object.keys(data.groups).length === 0 && <p className="muted">No group has any folder yet.</p>}
      {Object.entries(data.groups).map(([dn, folders]) => (
        <GroupEditor
          key={dn + folders.join("|")}
          dn={dn}
          selected={folders}
          all={data.folders}
          onSave={(next) => run(() => admin.setGroup(dn, next), `Saved folders for ${dn}.`)}
        />
      ))}
      <NewGroup config={data} onSave={(dn, f) => run(() => admin.setGroup(dn, f), `Saved folders for ${dn}.`)} />

      <h2>Exceptions for individual people</h2>
      <table>
        <thead>
          <tr><th>User</th><th>Folder</th><th>Effect</th><th>Set by</th><th /></tr>
        </thead>
        <tbody>
          {data.overrides.map((o) => (
            <tr key={o.username + o.folder}>
              <td>{o.username}</td>
              <td>{label(o.folder)}</td>
              <td>{o.effect}</td>
              <td>{o.created_by}</td>
              <td>
                <button className="link" onClick={() => run(() => admin.setOverride(o.username, o.folder, null), "Exception removed.")}>
                  Remove
                </button>
              </td>
            </tr>
          ))}
          {data.overrides.length === 0 && <tr><td colSpan={5} className="muted">None.</td></tr>}
        </tbody>
      </table>
      <NewOverride folders={data.folders} onSave={(u, f, e) => run(() => admin.setOverride(u, f, e), "Exception saved.")} />
    </div>
  );
}

function GroupEditor(props: { dn: string; selected: string[]; all: string[]; onSave: (f: string[]) => void }) {
  const [chosen, setChosen] = useState(new Set(props.selected));
  const folders = Array.from(new Set([...props.all, ...props.selected])).sort();
  return (
    <fieldset className="card">
      <legend>{props.dn === "*" ? "Everyone who signs in (*)" : props.dn}</legend>
      <div className="checks">
        {folders.map((f) => (
          <label key={f} className="check">
            <input
              type="checkbox"
              checked={chosen.has(f)}
              onChange={(e) => {
                const next = new Set(chosen);
                if (e.target.checked) next.add(f);
                else next.delete(f);
                setChosen(next);
              }}
            />
            {label(f)}
          </label>
        ))}
      </div>
      <button onClick={() => props.onSave(Array.from(chosen))}>Save</button>
    </fieldset>
  );
}

function NewGroup(props: { config: AdminConfig; onSave: (dn: string, folders: string[]) => void }) {
  const [dn, setDn] = useState("");
  const [folder, setFolder] = useState(props.config.folders[0] ?? "");
  function submit(e: FormEvent) {
    e.preventDefault();
    props.onSave(dn.trim(), [folder]);
    setDn("");
  }
  return (
    <form onSubmit={submit} className="inline">
      <label>
        Add a group (its distinguished name, or * for everyone)
        <input value={dn} onChange={(e) => setDn(e.target.value)} placeholder="CN=Sales,OU=Groups,DC=corp,DC=local" required />
      </label>
      <label>
        First folder
        <select value={folder} onChange={(e) => setFolder(e.target.value)}>
          {props.config.folders.map((f) => <option key={f} value={f}>{label(f)}</option>)}
        </select>
      </label>
      <button type="submit" disabled={!dn.trim()}>Add group</button>
    </form>
  );
}

function NewOverride(props: { folders: string[]; onSave: (u: string, f: string, e: "allow" | "deny") => void }) {
  const [user, setUser] = useState("");
  const [folder, setFolder] = useState(props.folders[0] ?? "");
  const [effect, setEffect] = useState<"allow" | "deny">("allow");
  function submit(e: FormEvent) {
    e.preventDefault();
    props.onSave(user.trim(), folder, effect);
    setUser("");
  }
  return (
    <form onSubmit={submit} className="inline">
      <label>
        Username
        <input value={user} onChange={(e) => setUser(e.target.value)} required />
      </label>
      <label>
        Folder
        <select value={folder} onChange={(e) => setFolder(e.target.value)}>
          {props.folders.map((f) => <option key={f} value={f}>{label(f)}</option>)}
        </select>
      </label>
      <label>
        Effect
        <select value={effect} onChange={(e) => setEffect(e.target.value as "allow" | "deny")}>
          <option value="allow">allow</option>
          <option value="deny">deny</option>
        </select>
      </label>
      <button type="submit" disabled={!user.trim()}>Save exception</button>
    </form>
  );
}
