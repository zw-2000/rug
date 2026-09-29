import { useCallback, useEffect, useState } from "react";
import { getMe, logout, setUnauthorizedHandler, type Me } from "./api";
import Admin from "./admin/Admin";
import Chat from "./Chat";
import Login from "./Login";
import Upload from "./Upload";

type Page = "chat" | "upload" | "admin";
const pageOf = (path: string): Page =>
  path.startsWith("/upload") ? "upload" : path.startsWith("/admin") ? "admin" : "chat";
const pathOf = (p: Page): string => (p === "chat" ? "/" : `/${p}`);

export default function App() {
  const [me, setMe] = useState<Me | null | undefined>(undefined); // undefined = still checking
  const [page, setPage] = useState<Page>(pageOf(window.location.pathname));
  const [expired, setExpired] = useState(false);

  useEffect(() => {
    setUnauthorizedHandler(() => {
      setMe(null);
      setExpired(true);
    });
    getMe()
      .then(setMe)
      .catch(() => setMe(null));
    const onPop = () => setPage(pageOf(window.location.pathname));
    window.addEventListener("popstate", onPop);
    return () => window.removeEventListener("popstate", onPop);
  }, []);

  const go = useCallback((p: Page) => {
    window.history.pushState(null, "", pathOf(p));
    setPage(p);
  }, []);

  if (me === undefined) return <p className="center muted">Loading…</p>;
  if (me === null) {
    return (
      <Login
        notice={expired ? "Your session ended. Please sign in again." : ""}
        onSignedIn={(m) => {
          setExpired(false);
          setMe(m);
        }}
      />
    );
  }

  return (
    <div className="shell">
      <header className="bar">
        <strong className="brand">rug</strong>
        <nav aria-label="Main">
          <button className={page === "chat" ? "tab on" : "tab"} onClick={() => go("chat")}>
            Ask
          </button>
          <button className={page === "upload" ? "tab on" : "tab"} onClick={() => go("upload")}>
            Upload
          </button>
          {me.is_admin && (
            <button className={page === "admin" ? "tab on" : "tab"} onClick={() => go("admin")}>
              Admin
            </button>
          )}
        </nav>
        <span className="who">
          {me.display_name || me.username}
          <button
            className="link"
            onClick={() => {
              logout()
                .catch(() => {})
                .finally(() => setMe(null));
            }}
          >
            Sign out
          </button>
        </span>
      </header>
      <main>
        {me.folders.length === 0 && page !== "admin" && (
          <p className="banner" role="status">
            You don&apos;t have access to any document folders yet. Ask an administrator.
          </p>
        )}
        {page === "chat" && <Chat />}
        {page === "upload" && <Upload me={me} />}
        {page === "admin" && (me.is_admin ? <Admin /> : <p className="error">Administrators only.</p>)}
      </main>
    </div>
  );
}
