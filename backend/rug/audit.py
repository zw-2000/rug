"""Audit trail. Entries are written in the caller's transaction, so a change and its record
commit or roll back together. There is deliberately no update or delete function, and a
database trigger rejects both. Never put secrets in `detail` (passwords, tokens are scrubbed)."""

from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rug.db.models import AuditEntry

_SECRET_KEYS = ("password", "passwd", "secret", "token", "cookie", "authorization")


def _scrub(detail: dict[str, Any]) -> dict[str, Any]:
    return {
        k: ("[redacted]" if any(s in k.lower() for s in _SECRET_KEYS) else v)
        for k, v in detail.items()
    }


def log(
    db: Session,
    actor: str,
    action: str,
    target: str = "",
    detail: dict[str, Any] | None = None,
    ip: str | None = None,
) -> None:
    db.add(
        AuditEntry(actor=actor, action=action, target=target, detail=_scrub(detail or {}), ip=ip)
    )
    db.flush()


def recent(
    db: Session,
    limit: int = 100,
    before_id: int | None = None,
    action: str | None = None,
    actor: str | None = None,
) -> list[AuditEntry]:
    q = select(AuditEntry).order_by(AuditEntry.id.desc()).limit(max(1, min(limit, 500)))
    if before_id is not None:
        q = q.where(AuditEntry.id < before_id)
    if action:
        q = q.where(AuditEntry.action == action)
    if actor:
        q = q.where(AuditEntry.actor == actor)
    return list(db.scalars(q))


def count_since(
    db: Session,
    action: str,
    window_s: int,
    actor: str | None = None,
    ip: str | None = None,
) -> int:
    since = datetime.now(UTC) - timedelta(seconds=window_s)
    q = (
        select(func.count())
        .select_from(AuditEntry)
        .where(AuditEntry.action == action, AuditEntry.at >= since)
    )
    if actor is not None:
        q = q.where(AuditEntry.actor == actor)
    if ip is not None:
        q = q.where(AuditEntry.ip == ip)
    return int(db.scalar(q) or 0)
