import { useState } from "react";
import Access from "./Access";
import DocTypes from "./DocTypes";
import IndexPanel from "./IndexPanel";
import { AuditLog, QaLog } from "./Logs";
import Users from "./Users";

const SECTIONS = [
  ["access", "Access"],
  ["users", "Users"],
  ["types", "Document types"],
  ["index", "Index"],
  ["questions", "Questions"],
  ["audit", "Audit log"],
] as const;
type Section = (typeof SECTIONS)[number][0];

export default function Admin() {
  const [section, setSection] = useState<Section>("access");
  return (
    <section aria-label="Administration">
      <h1>Administration</h1>
      <nav className="subtabs" aria-label="Administration sections">
        {SECTIONS.map(([id, label]) => (
          <button
            key={id}
            className={section === id ? "tab on" : "tab"}
            aria-current={section === id ? "page" : undefined}
            onClick={() => setSection(id)}
          >
            {label}
          </button>
        ))}
      </nav>
      {section === "access" && <Access />}
      {section === "users" && <Users />}
      {section === "types" && <DocTypes />}
      {section === "index" && <IndexPanel />}
      {section === "questions" && <QaLog />}
      {section === "audit" && <AuditLog />}
    </section>
  );
}
