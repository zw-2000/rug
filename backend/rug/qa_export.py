"""Turn thumbs-down (or any) logged answers into a skeleton for the golden question set.

The skeleton is deliberately incomplete: `facts` is empty and `doc` is only what the system
picked, so a person must decide what a correct answer contains before it is added to
`eval/golden.yaml`. Questions come from real users: review them for anything sensitive first.
"""

import uuid
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from rug.db.models import Document, QaLog


def skeletons(db: Session, feedback: int | None = -1, limit: int = 200) -> list[dict[str, Any]]:
    q = select(QaLog).order_by(QaLog.id.desc()).limit(max(1, min(limit, 1000)))
    if feedback in (1, -1):
        q = q.where(QaLog.feedback == feedback)
    rows = list(db.scalars(q))
    ids = [r.resolved_doc for r in rows if r.resolved_doc]
    names: dict[uuid.UUID, str] = {}
    if ids:
        names = {d.id: d.filename for d in db.scalars(select(Document).where(Document.id.in_(ids)))}
    out: list[dict[str, Any]] = []
    for r in rows:
        out.append(
            {
                "id": f"fb-{r.id}",
                "kind": "named" if r.mode in ("named", "pinned") else "find",
                "q": r.question,
                "doc": names.get(r.resolved_doc, "") if r.resolved_doc else "",
                "facts": [],  # TODO(reviewer): groups of alternatives a correct answer must contain
                "note": r.comment or "",
                "given_answer": r.answer,
            }
        )
    return out


def to_yaml(items: list[dict[str, Any]]) -> str:
    return yaml.safe_dump(items, sort_keys=False, allow_unicode=True, width=100)
