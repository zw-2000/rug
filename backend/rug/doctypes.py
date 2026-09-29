"""The document-type vocabulary the resolver understands (SOW, CR, MSA, ...), edited by admins."""

import re

from sqlalchemy.orm import Session

from rug import audit
from rug.db.models import DocType

_NAME = re.compile(r"^[A-Z][A-Z0-9]{1,11}$")
_PHRASE = re.compile(r"^[a-z0-9][a-z0-9 \-]{0,59}$")
MAX_PHRASES = 20


def clean(name: str, phrases: list[str]) -> tuple[str, list[str]]:
    key = name.strip().upper()
    if not _NAME.match(key):
        raise ValueError("a type name is 2-12 letters or digits, starting with a letter")
    out = list(dict.fromkeys(" ".join(p.lower().split()) for p in phrases if p.strip()))
    if not out or len(out) > MAX_PHRASES:
        raise ValueError(f"give between 1 and {MAX_PHRASES} words or phrases")
    for p in out:
        if not _PHRASE.match(p):
            raise ValueError(
                f"not a usable word or phrase: {p!r} (letters, digits, spaces, hyphens)"
            )
    return key, out


def listing(db: Session) -> dict[str, list[str]]:
    return {r.name: list(r.phrases) for r in db.query(DocType).order_by(DocType.name)}


def put(
    db: Session, actor: str, name: str, phrases: list[str], ip: str | None = None
) -> dict[str, list[str] | None]:
    key, cleaned = clean(name, phrases)
    row = db.get(DocType, key)
    before = list(row.phrases) if row else None
    if row:
        row.phrases = cleaned
    else:
        db.add(DocType(name=key, phrases=cleaned))
    db.flush()
    change: dict[str, list[str] | None] = {"before": before, "after": cleaned}
    audit.log(db, actor, "admin.doc_type", key, change, ip)
    return change


def delete(db: Session, actor: str, name: str, ip: str | None = None) -> bool:
    key = name.strip().upper()
    row = db.get(DocType, key)
    if row is None:
        return False
    before = list(row.phrases)
    db.delete(row)
    db.flush()
    audit.log(db, actor, "admin.doc_type", key, {"before": before, "after": None}, ip)
    return True
