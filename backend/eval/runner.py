"""Run the golden questions against a Rag and score the results.

Deterministic metrics need no model and gate CI:
  document   named questions resolved to the right document
  retrieval  the key facts are in the excerpts the model is shown (and superseded or
             deleted text is not)
  not_found_det  questions decided without the model (unknown ID, empty scope) get "not found"
  leaks      nothing outside the scope reaches the model, the answer or the sources
Model-dependent metrics are meaningful only with a real model (`rug eval --live`):
  citation   the cited excerpts contain the key facts
  answer     the answer contains the key facts (and finds the right document)
  not_found  every unanswerable question gets "not found"
"""

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from rug.config import Settings
from rug.db.models import Document
from rug.llm import ChatModel, Embedder
from rug.rag import Answer, Rag

GOLDEN = Path(__file__).with_name("golden.yaml")
DETERMINISTIC = ("document", "retrieval", "not_found_det", "leaks")
LIVE_ONLY = ("citation", "answer", "not_found")


def load_golden(path: Path | None = None) -> dict[str, Any]:
    return yaml.safe_load((path or GOLDEN).read_text())  # type: ignore[no-any-return]


def norm(s: str) -> str:
    """Lower-case, collapse whitespace, drop thousands separators (184,500 == 184500)."""
    return re.sub(r"(?<=\d),(?=\d)", "", re.sub(r"\s+", " ", s.lower())).strip()


def has_all(text: str, groups: list[list[str]]) -> bool:
    t = norm(text)
    return all(any(norm(alt) in t for alt in group) for group in groups)


def has_any(text: str, needles: list[str]) -> bool:
    t = norm(text)
    return any(norm(n) in t for n in needles)


@dataclass
class Result:
    id: str
    kind: str
    status: str
    checks: dict[str, bool] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)


@dataclass
class Report:
    results: list[Result]
    thresholds: dict[str, float]
    live: bool

    def metric(self, name: str) -> tuple[int, int]:
        rows = [r.checks[name] for r in self.results if name in r.checks]
        return sum(rows), len(rows)

    def rate(self, name: str) -> float | None:
        passed, total = self.metric(name)
        return passed / total if total else None

    def gate(self) -> list[str]:
        """Names of metrics that miss their threshold. Live-only metrics are not judged offline."""
        names = DETERMINISTIC + (LIVE_ONLY if self.live else ())
        failed = []
        for name in names:
            passed, total = self.metric(name)
            if not total:
                continue
            if name == "leaks":
                if total - passed > self.thresholds.get("leaks", 0):
                    failed.append(name)
            elif passed / total < self.thresholds.get(name.removesuffix("_det"), 0):
                failed.append(name)
        return failed

    def format(self) -> str:
        lines = ["metric          passed / total   rate    threshold"]
        for name in DETERMINISTIC + LIVE_ONLY:
            passed, total = self.metric(name)
            judged = name in DETERMINISTIC or self.live
            need = self.thresholds.get(name.removesuffix("_det"), 0)
            rate = f"{passed / total:6.1%}" if total else "   n/a"
            if name == "leaks":
                shown = f"{total - passed} leaks"
                mark = "" if not judged else ("PASS" if total - passed <= need else "FAIL")
                lines.append(f"{name:15} {shown:>14}   {rate}  <= {need:g}   {mark}")
                continue
            mark = "" if not (judged and total) else ("PASS" if passed / total >= need else "FAIL")
            note = "" if judged else "(model-dependent: not judged offline)"
            lines.append(f"{name:15} {passed:>6} / {total:<6} {rate}  >= {need:.0%}  {mark} {note}")
        bad = [r for r in self.results if any(not v for v in r.checks.values())]
        if bad:
            lines.append("")
            lines.append("questions with a failed check:")
            for r in bad:
                failed = [k for k, v in r.checks.items() if not v]
                lines.append(
                    f"  {r.id:20} {r.status:12} failed: {', '.join(failed)} {' '.join(r.notes)}"
                )
        return "\n".join(lines)


def score(
    q: dict[str, Any], scope: frozenset[str], answer: Answer, files: dict[Any, str]
) -> Result:
    """Score one answer. Pure: no database, no model."""
    r = Result(q["id"], q["kind"], answer.status)
    groups = q.get("facts", [])
    excerpts = "\n".join(e.text for e in answer.excerpts)
    cited = "\n".join(e.text for e in answer.excerpts if any(e.n in s.refs for s in answer.sources))
    seen_docs = {files.get(e.document_id, "?") for e in answer.excerpts}

    if q["kind"] == "named":
        got = answer.resolved.filename if answer.resolved else None
        r.checks["document"] = got == q["doc"]
        if got != q["doc"]:
            r.notes.append(f"resolved {got!r}")
    if q["kind"] in ("named", "find"):
        ok = has_all(excerpts, groups) and not has_any(excerpts, q.get("absent", []))
        if q["kind"] == "find":
            ok = ok and q["doc"] in seen_docs
        r.checks["retrieval"] = ok
        answered = answer.status == "answered"
        r.checks["citation"] = answered and not answer.ungrounded and has_all(cited, groups)
        found_doc = q["kind"] != "find" or q["doc"] in {s.filename for s in answer.sources}
        r.checks["answer"] = (
            answered
            and has_all(answer.text, groups)
            and not has_any(answer.text, q.get("absent", []))
            and found_doc
        )
    if q["kind"] == "unanswerable":
        r.checks["not_found"] = answer.status == "not_found"
        if q.get("det"):
            r.checks["not_found_det"] = answer.status == "not_found"

    if scope != frozenset(q_scopes_all(q)):  # restricted scope: look for leaks
        folders = (
            [e.folder for e in answer.excerpts]
            + [s.folder for s in answer.sources]
            + [c.doc.folder for c in answer.candidates]
        )
        outside = [f for f in folders if f not in scope]
        echoed = norm(q["q"])
        leaked = [
            s
            for s in q.get("forbidden", [])
            if norm(s) not in echoed and (has_any(excerpts, [s]) or has_any(answer.text, [s]))
        ]
        r.checks["leaks"] = not outside and not leaked
        if outside or leaked:
            r.notes.append(f"LEAK folders={sorted(set(outside))} strings={leaked}")
    return r


def q_scopes_all(q: dict[str, Any]) -> list[str]:
    return q["_all"]


def run_eval(
    db: Session,
    embedder: Embedder,
    chat: ChatModel,
    *,
    live: bool,
    golden: dict[str, Any] | None = None,
    settings: Settings | None = None,
) -> Report:
    g = golden or load_golden()
    files = {d.id: d.filename for d in db.scalars(select(Document))}
    rag = Rag(db, embedder, chat, settings)
    results = []
    for q in g["questions"]:
        scope = frozenset(g["scopes"][q.get("scope", "all")])
        q = {**q, "_all": g["scopes"]["all"]}
        results.append(score(q, scope, rag.ask(q["q"], folders=scope), files))
    return Report(results, g["thresholds"], live)
