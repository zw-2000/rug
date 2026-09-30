import { useState } from "react";
import { admin } from "../api";
import { message, useLoad } from "./useLoad";

const when = (s: string | null) => (s ? new Date(s).toLocaleString() : "never");

export default function Users() {
  const { data, error, reload } = useLoad(admin.users);
  const [problem, setProblem] = useState("");

  async function act(name: string, action: "disable" | "enable" | "revoke") {
    setProblem("");
    try {
      await admin.userAction(name, action);
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
        People appear here after their first sign-in. Disabling blocks them in this application (not in Active
        Directory) and ends their sessions at once.
      </p>
      {problem && <p className="error" role="alert">{problem}</p>}
      <table>
        <thead>
          <tr><th>User</th><th>Name</th><th>Last sign-in</th><th>Sessions</th><th>Status</th><th /></tr>
        </thead>
        <tbody>
          {data.map((u) => (
            <tr key={u.username}>
              <td>{u.username}</td>
              <td>{u.display_name}</td>
              <td>{when(u.last_login)}</td>
              <td>{u.sessions}</td>
              <td>{u.disabled ? "disabled" : "active"}</td>
              <td className="row-actions">
                {u.sessions > 0 && <button className="link" onClick={() => act(u.username, "revoke")}>Sign out everywhere</button>}
                <button className="link" onClick={() => act(u.username, u.disabled ? "enable" : "disable")}>
                  {u.disabled ? "Enable" : "Disable"}
                </button>
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
