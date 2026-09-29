"""Admin endpoints beyond folder access: users, document types, index status, Q&A export."""

import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from rug import audit, doctypes, qa_export
from rug.auth import sessions
from rug.config import Settings
from rug.db.models import Document, IndexRun, SessionRow, User
from rug.indexer import Indexer, IndexerBusy, MassDeletionRefused

log = logging.getLogger(__name__)


class DocTypeBody(BaseModel):
    name: str = Field(max_length=40)
    phrases: list[str] = Field(max_length=50)


def register_admin(
    app: FastAPI,
    *,
    s: Settings,
    factory: Callable[[], Session],
    get_db: Callable[..., Any],
    admin: Callable[..., sessions.Active],
    ip_of: Callable[[Request], str | None],
    embedder: Any,
) -> None:
    @app.get("/api/admin/users")
    def users(_: sessions.Active = Depends(admin), db: Session = Depends(get_db)) -> list[dict]:
        live = dict(
            db.execute(
                select(SessionRow.username, func.count())
                .where(SessionRow.expires_at > datetime.now(UTC))
                .group_by(SessionRow.username)
            ).all()
        )
        return [
            {
                "username": u.username,
                "display_name": u.display_name,
                "first_login": u.first_login.isoformat() if u.first_login else None,
                "last_login": u.last_login.isoformat() if u.last_login else None,
                "disabled": u.disabled,
                "sessions": int(live.get(u.username, 0)),
            }
            for u in db.scalars(select(User).order_by(User.username))
        ]

    @app.get("/api/admin/doc-types")
    def doc_types(
        _: sessions.Active = Depends(admin), db: Session = Depends(get_db)
    ) -> dict[str, list[str]]:
        return doctypes.listing(db)

    @app.put("/api/admin/doc-types")
    def put_doc_type(
        body: DocTypeBody,
        request: Request,
        a: sessions.Active = Depends(admin),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        try:
            change = doctypes.put(db, a.principal.username, body.name, body.phrases, ip_of(request))
        except ValueError as e:
            db.rollback()
            raise HTTPException(422, str(e)) from None
        db.commit()
        return change

    @app.delete("/api/admin/doc-types/{name}")
    def delete_doc_type(
        name: str,
        request: Request,
        a: sessions.Active = Depends(admin),
        db: Session = Depends(get_db),
    ) -> dict[str, bool]:
        if not doctypes.delete(db, a.principal.username, name, ip_of(request)):
            raise HTTPException(404, "No such document type.")
        db.commit()
        return {"ok": True}

    @app.get("/api/admin/index")
    def index_status(
        _: sessions.Active = Depends(admin), db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        runs = db.scalars(select(IndexRun).order_by(IndexRun.id.desc()).limit(10)).all()
        by_status = dict(
            db.execute(select(Document.status, func.count()).group_by(Document.status)).all()
        )
        pending = db.execute(
            text(
                "SELECT count(DISTINCT d.sha256) FROM documents d "
                "LEFT JOIN document_summaries m ON m.sha256 = d.sha256 "
                "WHERE d.status = 'ok' AND m.sha256 IS NULL "
                "AND EXISTS (SELECT 1 FROM chunks c WHERE c.document_id = d.id)"
            )
        ).scalar()
        broken = db.execute(
            select(Document.path, Document.error)
            .where(Document.status == "error")
            .order_by(Document.path)
            .limit(50)
        ).all()
        return {
            "scan_interval_s": s.scan_interval_s,
            "scanning": Indexer(db, embedder, Path(s.docs_dir), s).is_busy(),
            "documents": {"ok": by_status.get("ok", 0), "error": by_status.get("error", 0)},
            "summaries_pending": int(pending or 0),
            "runs": [
                {
                    "id": r.id,
                    "started_at": r.started_at.isoformat(),
                    "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                    "total": r.total,
                    "processed": r.processed,
                    "counts": r.counts,
                    "errors": r.errors[:5],
                    "n_errors": len(r.errors),
                }
                for r in runs
            ],
            "broken": [{"path": b.path, "error": b.error} for b in broken],
        }

    @app.post("/api/admin/index/scan", status_code=202)
    def scan_now(
        request: Request,
        a: sessions.Active = Depends(admin),
        db: Session = Depends(get_db),
    ) -> dict[str, bool]:
        """Start a scan in the background; the status endpoint shows progress."""
        if Indexer(db, embedder, Path(s.docs_dir), s).is_busy():
            raise HTTPException(409, "A scan is already running.")
        audit.log(db, a.principal.username, "admin.scan", ip=ip_of(request))
        db.commit()

        def run() -> None:
            try:
                with factory() as wdb:
                    Indexer(wdb, embedder, Path(s.docs_dir), s).run()
            except (IndexerBusy, MassDeletionRefused) as e:
                log.warning("manual scan not run: %s", e)
            except Exception:
                log.exception("manual scan failed")

        threading.Thread(target=run, daemon=True).start()
        return {"started": True}

    @app.get("/api/admin/qa/export")
    def export(
        feedback: int | None = -1,
        _: sessions.Active = Depends(admin),
        db: Session = Depends(get_db),
    ) -> PlainTextResponse:
        body = qa_export.to_yaml(qa_export.skeletons(db, feedback))
        return PlainTextResponse(
            body,
            media_type="text/yaml; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="golden-candidates.yaml"'},
        )
