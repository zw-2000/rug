"""HTTP API: sign-in, session, download, upload, administration.

Every route except sign-in and /healthz needs a valid session; state-changing routes also
need the session's CSRF token in `X-CSRF-Token`. What a caller may see is computed per
request by `perms.effective_folders`. Status codes: unknown document 404, existing but not
permitted 403, upload to a folder the caller may not use 403.
"""

import logging
import mimetypes
import uuid
from collections.abc import Callable, Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from rug import audit, perms
from rug.api.admin import register_admin
from rug.api.chat import register_chat
from rug.auth import sessions
from rug.auth.ldap import AuthError, DirectoryUnavailable, LdapAuthenticator
from rug.config import Settings, get_settings
from rug.db.models import Document, User
from rug.db.session import make_session
from rug.indexer import Indexer, IndexerBusy
from rug.loaders import supported_extensions
from rug.paths import UnsafePath, safe_doc_path
from rug.uploads import UploadRejected, store

log = logging.getLogger(__name__)

DEFAULT_BODY_LIMIT = 64 * 1024  # every route except upload
MULTIPART_OVERHEAD = 1024 * 1024  # form fields and boundaries on top of the file itself
BAD_LOGIN = "Invalid username or password."
# The built frontend has no inline scripts or styles; answers are rendered as text only.
CSP = (
    "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
    "form-action 'self'; frame-ancestors 'none'"
)


class _BodyTooLarge(Exception):
    pass


class BodyLimit:
    """Reject request bodies over a limit as they stream in: `upload_limit` under
    `upload_prefix`, `default_limit` everywhere else (JSON bodies are read whole into memory,
    and sign-in needs no session). Starlette's multipart parser has no file-size limit of its
    own, and Content-Length can be absent or a lie."""

    def __init__(self, app: ASGIApp, default_limit: int, upload_limit: int, upload_prefix: str):
        self.app, self.default, self.upload = app, default_limit, upload_limit
        self.prefix = upload_prefix

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        limit = self.upload if scope["path"].startswith(self.prefix) else self.default
        declared = dict(scope["headers"]).get(b"content-length")
        started = False
        seen = 0

        async def counting() -> Message:
            nonlocal seen
            msg = await receive()
            if msg["type"] == "http.request":
                seen += len(msg.get("body", b""))
                if seen > limit:
                    raise _BodyTooLarge
            return msg

        async def tracking(msg: Message) -> None:
            nonlocal started
            started = started or msg["type"] == "http.response.start"
            await send(msg)

        too_big = JSONResponse({"detail": "The request is too large."}, status_code=413)
        if declared is not None and declared.isdigit() and int(declared) > limit:
            await too_big(scope, receive, send)
            return
        try:
            await self.app(scope, counting, tracking)
        except _BodyTooLarge:
            if not started:
                await too_big(scope, receive, send)


class LoginBody(BaseModel):
    username: str = Field(max_length=200)
    password: str = Field(max_length=1024)


class GroupBody(BaseModel):
    group_dn: str = Field(max_length=1000)
    folders: list[str] = Field(max_length=500)


class OverrideBody(BaseModel):
    username: str
    folder: str
    effect: str | None


def create_app(
    settings: Settings | None = None,
    session_factory: Callable[[], Session] | None = None,
    authenticator: LdapAuthenticator | None = None,
    embedder: Any = None,
    chat_model: Any = None,
) -> FastAPI:
    s = settings or get_settings()
    sessions.check_secret(s)  # refuse to start without a session secret
    auth = authenticator or LdapAuthenticator(s)
    factory = session_factory or make_session
    root = Path(s.docs_dir)
    max_bytes = s.max_upload_mb * 1024 * 1024

    app = FastAPI(title="rug", docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(
        BodyLimit,
        default_limit=DEFAULT_BODY_LIMIT,
        upload_limit=max_bytes + MULTIPART_OVERHEAD,
        upload_prefix="/api/upload",
    )

    @app.middleware("http")
    async def headers(request: Request, call_next: Callable[..., Any]) -> Response:
        resp: Response = await call_next(request)
        resp.headers.setdefault("Cache-Control", "no-store")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Referrer-Policy", "same-origin")
        resp.headers.setdefault("Content-Security-Policy", CSP)
        return resp

    def get_db() -> Iterator[Session]:
        db = factory()
        try:
            yield db
        finally:
            db.close()

    def ip_of(request: Request) -> str | None:
        peer = request.client.host if request.client else None
        return sessions.client_ip(peer, request.headers, s)

    def current(request: Request, db: Session = Depends(get_db)) -> sessions.Active:
        active = sessions.lookup(db, s, request.cookies.get(sessions.COOKIE_NAME))
        db.commit()  # keeps last_seen / expired-row cleanup
        if active is None:
            raise HTTPException(401, "Sign in required.")
        if request.method not in ("GET", "HEAD", "OPTIONS") and not sessions.csrf_ok(
            active, request.headers.get("x-csrf-token")
        ):
            raise HTTPException(403, "Missing or invalid CSRF token.")
        return active

    def admin(active: sessions.Active = Depends(current)) -> sessions.Active:
        if not active.principal.is_admin:
            raise HTTPException(403, "Administrator access required.")
        return active

    def me_payload(active: sessions.Active, db: Session) -> dict[str, Any]:
        p = active.principal
        folders = sorted(perms.effective_folders(db, p))
        return {
            "username": p.username,
            "display_name": p.display_name,
            "is_admin": p.is_admin,
            "folders": folders,
            "upload_folders": [
                f for f in folders if f and (root / f).is_dir() and not (root / f).is_symlink()
            ],
            "upload_types": sorted(supported_extensions()),
            "max_upload_mb": s.max_upload_mb,
            "csrf_token": active.csrf_token,
        }

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # -- authentication -----------------------------------------------------------------

    @app.post("/api/auth/login")
    def login(
        body: LoginBody, request: Request, response: Response, db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        ip = ip_of(request)
        try:  # ann, CORP\ann and ann@corp.local are one account, so one throttle bucket
            who = auth.parse_username(body.username)
        except AuthError:
            who = sessions.throttle_key(body.username)
        if sessions.login_blocked(db, s, who, ip):
            audit.log(db, who, "login.throttled", ip=ip)
            db.commit()
            raise HTTPException(
                429, "Too many failed attempts. Try again later.",
                headers={"Retry-After": str(s.login_window_s)},
            )  # fmt: skip
        try:
            ident = auth.authenticate(body.username, body.password)
        except AuthError:
            audit.log(db, who, "login.fail", ip=ip)
            db.commit()
            raise HTTPException(401, BAD_LOGIN) from None
        except DirectoryUnavailable:
            log.exception("directory unavailable during login")
            raise HTTPException(503, "The sign-in service is unavailable.") from None
        row = db.get(User, ident.username)
        if row is not None and row.disabled:
            audit.log(db, ident.username, "login.disabled", ip=ip)
            db.commit()
            raise HTTPException(401, BAD_LOGIN)
        cookie, _ = sessions.create(db, s, ident, ip)
        audit.log(db, ident.username, "login.ok", ip=ip)
        db.commit()
        response.set_cookie(
            sessions.COOKIE_NAME, cookie, max_age=s.session_ttl_s, httponly=True,
            secure=s.cookie_secure, samesite="lax", path="/",
        )  # fmt: skip
        active = sessions.lookup(db, s, cookie)
        assert active is not None
        return me_payload(active, db)

    @app.post("/api/auth/logout")
    def logout(
        response: Response,
        request: Request,
        active: sessions.Active = Depends(current),
        db: Session = Depends(get_db),
    ) -> dict[str, bool]:
        sessions.destroy(db, active.session_id)
        audit.log(db, active.principal.username, "logout", ip=ip_of(request))
        db.commit()
        response.delete_cookie(sessions.COOKIE_NAME, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(
        active: sessions.Active = Depends(current), db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        return me_payload(active, db)

    # -- documents ----------------------------------------------------------------------

    @app.get("/api/documents/{doc_id}/download")
    def download(
        doc_id: str,
        request: Request,
        active: sessions.Active = Depends(current),
        db: Session = Depends(get_db),
    ) -> Response:
        p = active.principal
        try:
            key = uuid.UUID(doc_id)
        except ValueError:
            raise HTTPException(404, "Document not found.") from None
        doc = db.get(Document, key)
        if doc is None:
            raise HTTPException(404, "Document not found.")
        if doc.folder not in perms.effective_folders(db, p):
            audit.log(db, p.username, "download.forbidden", str(key), ip=ip_of(request))
            db.commit()
            raise HTTPException(403, "You do not have access to this document.")
        try:
            path = safe_doc_path(root, doc.path)
        except UnsafePath:
            raise HTTPException(404, "The file is no longer available.") from None
        if not path.is_file():
            raise HTTPException(404, "The file is no longer available.")
        audit.log(db, p.username, "download", str(key), {"path": doc.path}, ip_of(request))
        db.commit()
        media = mimetypes.guess_type(doc.filename)[0] or "application/octet-stream"
        return FileResponse(path, media_type=media, filename=doc.filename)

    @app.get("/api/documents/{doc_id}/versions")
    def versions(
        doc_id: str,
        request: Request,
        active: sessions.Active = Depends(current),
        db: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        """Every version of a document (same folder and version key), newest first."""
        try:
            key = uuid.UUID(doc_id)
        except ValueError:
            raise HTTPException(404, "Document not found.") from None
        doc = db.get(Document, key)
        if doc is None:
            raise HTTPException(404, "Document not found.")
        if doc.folder not in perms.effective_folders(db, active.principal):
            raise HTTPException(403, "You do not have access to this document.")
        rows = db.scalars(
            select(Document)
            .where(
                Document.folder == doc.folder,
                Document.version_key == doc.version_key,
                Document.status == "ok",
            )
            .order_by(Document.mtime.desc(), Document.filename)
        ).all()
        return [
            {
                "id": str(r.id),
                "filename": r.filename,
                "modified": datetime.fromtimestamp(r.mtime, UTC).isoformat(),
                "latest": i == 0,
            }
            for i, r in enumerate(rows)
        ]

    @app.post("/api/upload", status_code=201)
    async def upload(
        request: Request,
        active: sessions.Active = Depends(current),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        p, ip = active.principal, ip_of(request)
        try:
            form = await request.form(max_files=1, max_fields=5)
        except _BodyTooLarge:
            raise  # answered as 413 by the BodyLimit middleware
        except Exception:
            raise HTTPException(400, "Malformed upload.") from None
        folder, file = form.get("folder"), form.get("file")
        if not isinstance(folder, str) or file is None or isinstance(file, str):
            raise HTTPException(400, "Send a 'folder' field and a 'file' part.")
        if folder not in perms.effective_folders(db, p):  # before anything is written
            audit.log(db, p.username, "upload.forbidden", folder[:255], ip=ip)
            db.commit()
            await form.close()
            raise HTTPException(403, "You do not have access to that folder.")

        def chunks() -> Iterator[bytes]:
            while block := file.file.read(1024 * 1024):
                yield block

        try:
            rel = await run_in_threadpool(
                store, root, folder, file.filename or "", chunks(), max_bytes
            )
        except UploadRejected as e:
            audit.log(db, p.username, "upload.rejected", folder, {"reason": e.reason}, ip)
            db.commit()
            raise HTTPException(e.status, e.reason) from None
        except OSError:
            # e.g. the share is mounted read-only or not writable by the server's user
            log.exception("could not write an upload into %s", root)
            audit.log(db, p.username, "upload.failed", folder, {"reason": "share not writable"}, ip)
            db.commit()
            raise HTTPException(
                500,
                "The server could not save the file to the document share. Tell an administrator.",
            ) from None
        finally:
            await form.close()
        audit.log(db, p.username, "upload", rel, ip=ip)
        db.commit()
        indexed, doc_id = False, None
        try:
            emb = embedder
            if emb is None:
                from rug.llm import OllamaEmbedder

                emb = OllamaEmbedder()
            await run_in_threadpool(Indexer(db, emb, root, s).sync_one, rel)
            found = db.query(Document).filter(Document.path == rel).first()
            indexed = found is not None and found.status == "ok"
            doc_id = str(found.id) if indexed and found else None
        except IndexerBusy:
            log.info("upload %s saved; indexer busy, next scan will index it", rel)
        except Exception:
            db.rollback()
            log.exception("upload %s saved but not indexed yet", rel)
        return {"path": rel, "filename": Path(rel).name, "folder": folder,
                "indexed": indexed, "id": doc_id}  # fmt: skip

    # -- chat -----------------------------------------------------------------------------

    if embedder is None or chat_model is None:
        from rug.llm import OllamaChat, OllamaEmbedder

        embedder = embedder or OllamaEmbedder(s)
        chat_model = chat_model or OllamaChat(s)
    register_chat(
        app,
        s=s,
        factory=factory,
        get_db=get_db,
        current=current,
        admin=admin,
        embedder=embedder,
        chat_model=chat_model,
    )

    register_admin(
        app, s=s, factory=factory, get_db=get_db, admin=admin, ip_of=ip_of, embedder=embedder
    )

    # -- administration -----------------------------------------------------------------

    @app.get("/api/admin/config")
    def admin_config(
        _: sessions.Active = Depends(admin), db: Session = Depends(get_db)
    ) -> dict[str, Any]:
        return {
            "admin_group": s.ldap_admin_group_dn,
            "folders": perms.known_folders(db, root),
            "groups": perms.group_folder_map(db),
            "overrides": perms.overrides(db),
        }

    def _bad(e: ValueError) -> HTTPException:
        return HTTPException(422, str(e))

    @app.put("/api/admin/groups")
    def admin_groups(
        body: GroupBody,
        request: Request,
        a: sessions.Active = Depends(admin),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        try:
            change = perms.set_group_folders(
                db, a.principal.username, body.group_dn, body.folders, ip_of(request)
            )
        except ValueError as e:
            db.rollback()
            raise _bad(e) from None
        db.commit()
        return change

    @app.put("/api/admin/overrides")
    def admin_override(
        body: OverrideBody,
        request: Request,
        a: sessions.Active = Depends(admin),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        try:
            change = perms.set_override(
                db, a.principal.username, body.username, body.folder, body.effect, ip_of(request)
            )
        except ValueError as e:
            db.rollback()
            raise _bad(e) from None
        db.commit()
        return change

    @app.post("/api/admin/users/{username}/{action}")
    def admin_user(
        username: str,
        action: str,
        request: Request,
        a: sessions.Active = Depends(admin),
        db: Session = Depends(get_db),
    ) -> dict[str, Any]:
        actor, ip = a.principal.username, ip_of(request)
        try:
            if action == "revoke":
                result: dict[str, Any] = {"revoked": perms.revoke_sessions(db, actor, username, ip)}
            elif action in ("disable", "enable"):
                perms.set_user_disabled(db, actor, username, action == "disable", ip)
                result = {"disabled": action == "disable"}
            else:
                raise HTTPException(404, "Unknown action.")
        except ValueError as e:
            db.rollback()
            raise _bad(e) from None
        db.commit()
        return result

    @app.get("/api/admin/audit")
    def admin_audit(
        limit: int = 100,
        before_id: int | None = None,
        action: str | None = None,
        actor: str | None = None,
        _: sessions.Active = Depends(admin),
        db: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        return [
            {"id": e.id, "at": e.at.isoformat(), "actor": e.actor, "action": e.action,
             "target": e.target, "detail": e.detail, "ip": e.ip}
            for e in audit.recent(db, limit, before_id, action, actor)
        ]  # fmt: skip

    if s.static_dir:
        _serve_frontend(app, Path(s.static_dir))
    return app


class _Assets(StaticFiles):
    """Vite fingerprints asset names, so they may be cached for good."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        resp = await super().get_response(path, scope)
        if resp.status_code == 200:
            resp.headers["Cache-Control"] = "public, max-age=31536000, immutable"
        return resp


def _serve_frontend(app: FastAPI, dist: Path) -> None:
    index = dist / "index.html"
    if not index.is_file():
        raise FileNotFoundError(f"RUG_STATIC_DIR has no index.html: {dist}")
    app.mount("/assets", _Assets(directory=dist / "assets"), name="assets")

    @app.get("/{path:path}", include_in_schema=False)
    def spa(path: str) -> Response:
        # Client-side routes get the app; an unknown API path stays a JSON 404.
        if path == "api" or path.startswith("api/"):
            raise HTTPException(404, "Not found.")
        return FileResponse(index, media_type="text/html")
