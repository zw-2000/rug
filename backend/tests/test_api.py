import io
import uuid

import docx
import pytest
from fastapi.testclient import TestClient
from ldapmock import MockDirectory
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker

from rug import audit, perms
from rug.api.app import create_app
from rug.auth.ldap import LdapAuthenticator
from rug.config import Settings
from rug.db.models import Document, SessionRow

SALES = "CN=Sales,OU=Groups,DC=corp,DC=local"
DELIVERY = "CN=Delivery,OU=Groups,DC=corp,DC=local"
LEGAL = "CN=Legal,OU=Groups,DC=corp,DC=local"
ADMINS = "CN=RugAdmins,OU=Groups,DC=corp,DC=local"

USERS = {
    "ann": ("pw-ann", "Ann A", [SALES]),
    "dan": ("pw-dan", "Dan D", [DELIVERY]),
    "lee": ("pw-lee", "Lee L", [LEGAL]),
    "root": ("pw-root", "Root R", [ADMINS]),
    "nobody": ("pw-nobody", "No Body", []),
}


def small_docx(text="hello") -> bytes:
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


class Env:
    def __init__(self, engine, corpus, embedder, **overrides):
        self.engine, self.corpus, self.docs_dir = engine, corpus, corpus.docs_dir
        self.directory = MockDirectory(USERS)
        self.settings = Settings(
            _env_file=None,
            docs_dir=self.docs_dir,
            session_secret="s" * 40,
            ldap_url="ldaps://dc.corp.local",
            ldap_base_dn="dc=corp,dc=local",
            ldap_netbios_domain="CORP",
            ldap_admin_group_dn=ADMINS,
            **overrides,
        )
        self.factory = sessionmaker(engine, expire_on_commit=False)
        self.app = create_app(
            self.settings,
            session_factory=self.factory,
            authenticator=LdapAuthenticator(self.settings, self.directory),
            embedder=embedder,
        )
        with self.factory() as db:  # who sees what
            perms.set_group_folders(db, "setup", SALES, ["sales"])
            perms.set_group_folders(db, "setup", DELIVERY, ["delivery"])
            perms.set_group_folders(db, "setup", LEGAL, ["legal"])
            db.commit()

    def client(self) -> TestClient:
        return TestClient(self.app, base_url="https://testserver")

    def login(self, name: str) -> tuple[TestClient, dict[str, str]]:
        c = self.client()
        r = c.post("/api/auth/login", json={"username": name, "password": USERS[name][0]})
        assert r.status_code == 200, r.text
        return c, {"X-CSRF-Token": r.json()["csrf_token"]}

    def doc(self, path: str) -> Document:
        with self.factory() as db:
            return db.scalars(select(Document).where(Document.path == path)).one()

    def audit_actions(self) -> list[str]:
        with self.factory() as db:
            return [e.action for e in audit.recent(db, 500)]


@pytest.fixture
def env(engine, corpus, embedder):
    return Env(engine, corpus, embedder)


# -- sign-in ---------------------------------------------------------------------------


def test_login_sets_a_locked_down_cookie(env):
    c = env.client()
    r = c.post("/api/auth/login", json={"username": "CORP\\Ann", "password": "pw-ann"})
    assert r.status_code == 200
    body = r.json()
    assert body["username"] == "ann" and body["folders"] == ["sales"] and not body["is_admin"]
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie
    assert c.get("/api/me").json()["username"] == "ann"


def test_login_failures_are_uniform_and_logged(env):
    c = env.client()
    bad = c.post("/api/auth/login", json={"username": "ann", "password": "wrong"})
    ghost = c.post("/api/auth/login", json={"username": "ghost", "password": "wrong"})
    assert bad.status_code == ghost.status_code == 401 and bad.json() == ghost.json()
    assert env.audit_actions().count("login.fail") == 2


def test_empty_password_never_reaches_the_directory(env):
    r = env.client().post("/api/auth/login", json={"username": "ann", "password": ""})
    assert r.status_code == 401 and env.directory.calls == []


def test_login_throttled_per_user_then_blocks_even_the_right_password(env):
    c = env.client()
    for _ in range(env.settings.login_max_failures_user):
        assert (
            c.post("/api/auth/login", json={"username": "ann", "password": "x"}).status_code == 401
        )
    r = c.post("/api/auth/login", json={"username": "ann", "password": "pw-ann"})
    assert r.status_code == 429 and "retry-after" in r.headers
    assert (
        env.client()
        .post("/api/auth/login", json={"username": "dan", "password": "pw-dan"})
        .status_code
        == 200
    )


def test_directory_down_is_503_and_not_counted(env):
    from ldap3.core.exceptions import LDAPSocketOpenError

    def down(identity, password):
        raise LDAPSocketOpenError("no route")

    app = create_app(
        env.settings,
        session_factory=env.factory,
        authenticator=LdapAuthenticator(env.settings, down),
    )
    r = TestClient(app, base_url="https://testserver").post(
        "/api/auth/login", json={"username": "ann", "password": "pw-ann"}
    )
    assert r.status_code == 503 and "login.fail" not in env.audit_actions()


def test_no_session_no_access(env):
    c = env.client()
    assert c.get("/api/me").status_code == 401
    assert c.get(f"/api/documents/{uuid.uuid4()}/download").status_code == 401
    assert c.post("/api/upload").status_code == 401
    assert c.get("/api/admin/config").status_code == 401
    assert c.get("/healthz").status_code == 200


def test_app_refuses_to_start_without_a_secret(env):
    from rug.auth.sessions import ConfigError

    with pytest.raises(ConfigError):
        create_app(env.settings.model_copy(update={"session_secret": ""}))


def test_csrf_required_on_state_changes(env):
    c, csrf = env.login("root")
    for method, url, kw in [
        ("post", "/api/auth/logout", {}),
        ("put", "/api/admin/groups", {"json": {"group_dn": SALES, "folders": []}}),
        ("post", "/api/admin/users/ann/revoke", {}),
    ]:
        assert getattr(c, method)(url, **kw).status_code == 403
        assert getattr(c, method)(url, headers={"X-CSRF-Token": "nope"}, **kw).status_code == 403
    assert c.get("/api/me").status_code == 200  # still signed in
    assert c.post("/api/auth/logout", headers=csrf).status_code == 200
    assert c.get("/api/me").status_code == 401


def test_expired_and_revoked_sessions(env):
    c, _ = env.login("ann")
    with env.factory() as db:
        db.query(SessionRow).update({"expires_at": SessionRow.created_at})
        db.commit()
    assert c.get("/api/me").status_code == 401
    c, _ = env.login("ann")
    a, acsrf = env.login("root")
    assert a.post("/api/admin/users/ann/revoke", headers=acsrf).json() == {"revoked": 1}
    assert c.get("/api/me").status_code == 401


def test_disabled_user_is_cut_off_immediately_and_cannot_log_in(env):
    c, _ = env.login("ann")
    a, acsrf = env.login("root")
    assert a.post("/api/admin/users/ann/disable", headers=acsrf).status_code == 200
    assert c.get("/api/me").status_code == 401
    r = env.client().post("/api/auth/login", json={"username": "ann", "password": "pw-ann"})
    assert r.status_code == 401
    assert a.post("/api/admin/users/ann/enable", headers=acsrf).status_code == 200
    env.login("ann")


# -- download: 200 / 403 / 404 ------------------------------------------------------------------


def test_download_status_codes(env):
    ann, _ = env.login("ann")
    mine = env.doc("sales/Boost Connect SR-1098 SOW v2 FINAL.docx")
    r = ann.get(f"/api/documents/{mine.id}/download")
    assert r.status_code == 200
    assert r.content == (env.docs_dir / mine.path).read_bytes()
    assert "attachment" in r.headers["content-disposition"]

    secret = env.doc("legal/" + next(p.name for p in (env.docs_dir / "legal").iterdir()))
    assert ann.get(f"/api/documents/{secret.id}/download").status_code == 403
    assert ann.get(f"/api/documents/{uuid.uuid4()}/download").status_code == 404
    assert ann.get("/api/documents/not-a-uuid/download").status_code == 404
    acts = env.audit_actions()
    assert "download" in acts and "download.forbidden" in acts


def test_any_version_downloadable_when_folder_permitted(env):
    ann, _ = env.login("ann")
    old = env.doc("sales/Boost Connect SR-1098 SOW v1.docx")
    assert ann.get(f"/api/documents/{old.id}/download").status_code == 200


def test_download_of_a_vanished_file_is_404(env):
    ann, _ = env.login("ann")
    doc = env.doc("sales/Boost Connect SR-1098 SOW v1.docx")
    (env.docs_dir / doc.path).unlink()
    assert ann.get(f"/api/documents/{doc.id}/download").status_code == 404


def test_download_refuses_a_file_swapped_for_a_symlink(env, tmp_path):
    ann, _ = env.login("ann")
    doc = env.doc("sales/Boost Connect SR-1098 SOW v1.docx")
    outside = tmp_path / "outside.docx"
    outside.write_bytes(b"secret")
    (env.docs_dir / doc.path).unlink()
    (env.docs_dir / doc.path).symlink_to(outside)
    assert ann.get(f"/api/documents/{doc.id}/download").status_code == 404


# -- upload ------------------------------------------------------------------------------------


def up(c, csrf, folder, name, data, **kw):
    return c.post(
        "/api/upload", headers=csrf, data={"folder": folder}, files={"file": (name, data)}, **kw
    )


def test_upload_indexes_and_never_overwrites(env):
    ann, csrf = env.login("ann")
    r = up(ann, csrf, "sales", "New Deal SR-5555 SOW.docx", small_docx("The scope is mowing."))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["path"] == "sales/New Deal SR-5555 SOW.docx" and body["indexed"] is True
    assert ann.get(f"/api/documents/{body['id']}/download").status_code == 200
    r2 = up(ann, csrf, "sales", "New Deal SR-5555 SOW.docx", small_docx("again"))
    assert r2.json()["path"] == "sales/New Deal SR-5555 SOW (1).docx"
    from rug import loaders

    first = loaders.load(env.docs_dir / "sales/New Deal SR-5555 SOW.docx")
    assert "mowing" in first.sections[0].text  # the original was not overwritten
    assert "upload" in env.audit_actions()


def test_upload_to_forbidden_folder_is_403_and_writes_nothing(env):
    ann, csrf = env.login("ann")
    before = sorted(p.name for p in env.docs_dir.rglob("*"))
    for folder in ("legal", "ghost-folder", "..", ""):
        assert up(ann, csrf, folder, "x.docx", small_docx()).status_code == 403
    assert sorted(p.name for p in env.docs_dir.rglob("*")) == before
    assert "upload.forbidden" in env.audit_actions()


def test_upload_content_and_type_checks(env):
    ann, csrf = env.login("ann")
    before = sorted(p.name for p in env.docs_dir.rglob("*"))
    assert up(ann, csrf, "sales", "x.pdf", b"%PDF-1.4").status_code == 415
    assert up(ann, csrf, "sales", "x.docx", b"not a zip").status_code == 415
    assert up(ann, csrf, "sales", "x.docx", b"").status_code == 400
    assert up(ann, csrf, "sales", "x.docx.exe", small_docx()).status_code == 415
    assert ann.post("/api/upload", headers=csrf, data={"folder": "sales"}).status_code == 400
    assert sorted(p.name for p in env.docs_dir.rglob("*")) == before


def test_upload_filename_cannot_escape_the_folder(env, tmp_path):
    ann, csrf = env.login("ann")
    r = up(ann, csrf, "sales", "../../../evil.docx", small_docx())
    assert r.status_code == 201 and r.json()["path"] == "sales/evil.docx"
    assert not (env.docs_dir.parent / "evil.docx").exists()
    r = up(ann, csrf, "sales", "C:\\temp\\win.docx", small_docx())
    assert r.json()["path"] == "sales/win.docx"


def test_upload_size_cap(engine, corpus, embedder):
    env = Env(engine, corpus, embedder, max_upload_mb=1)
    ann, csrf = env.login("ann")
    before = sorted(p.name for p in env.docs_dir.rglob("*"))
    big = small_docx() + b"\0" * (2 * 1024 * 1024)
    assert up(ann, csrf, "sales", "big.docx", big).status_code == 413
    assert sorted(p.name for p in env.docs_dir.rglob("*")) == before
    assert up(ann, csrf, "sales", "ok.docx", small_docx()).status_code == 201


def test_upload_saved_even_when_embedding_is_down(engine, corpus):
    class Down:
        def embed_documents(self, *_a, **_k):
            raise ConnectionError("ollama is down")

        embed_queries = embed_documents

    env = Env(engine, corpus, Down())
    ann, csrf = env.login("ann")
    r = up(ann, csrf, "sales", "late.docx", small_docx())
    assert r.status_code == 201 and r.json()["indexed"] is False
    assert (env.docs_dir / "sales/late.docx").exists()


def test_deny_override_blocks_upload_and_download_at_once(env):
    ann, csrf = env.login("ann")
    root, rcsrf = env.login("root")
    doc = env.doc("sales/Boost Connect SR-1098 SOW v1.docx")
    assert ann.get(f"/api/documents/{doc.id}/download").status_code == 200
    r = root.put(
        "/api/admin/overrides",
        headers=rcsrf,
        json={"username": "ann", "folder": "sales", "effect": "deny"},
    )
    assert r.status_code == 200
    assert ann.get(f"/api/documents/{doc.id}/download").status_code == 403
    assert up(ann, csrf, "sales", "x.docx", small_docx()).status_code == 403
    assert ann.get("/api/me").json()["folders"] == []


# -- administration -----------------------------------------------------------------------


def test_admin_endpoints_need_the_admin_group(env):
    ann, csrf = env.login("ann")
    assert ann.get("/api/admin/config").status_code == 403
    assert ann.get("/api/admin/audit").status_code == 403
    assert (
        ann.put(
            "/api/admin/groups", headers=csrf, json={"group_dn": SALES, "folders": ["legal"]}
        ).status_code
        == 403
    )
    with env.factory() as db:
        p = perms.Principal("ann", "", frozenset({perms.normalize_dn(SALES)}), False)
        assert perms.effective_folders(db, p) == {"sales"}  # the refused change did not apply


def test_admin_grants_no_documents_by_itself(env):
    root, _ = env.login("root")
    me = root.get("/api/me").json()
    assert me["is_admin"] and me["folders"] == []
    doc = env.doc("sales/Boost Connect SR-1098 SOW v1.docx")
    assert root.get(f"/api/documents/{doc.id}/download").status_code == 403


def test_group_map_change_applies_without_relogin_and_is_audited(env):
    ann, _ = env.login("ann")
    root, rcsrf = env.login("root")
    cfg = root.get("/api/admin/config").json()
    assert {"sales", "delivery", "legal"} <= set(cfg["folders"]) and cfg["admin_group"] == ADMINS
    r = root.put(
        "/api/admin/groups",
        headers=rcsrf,
        json={"group_dn": SALES, "folders": ["sales", "delivery"]},
    )
    assert r.json() == {"before": ["sales"], "after": ["delivery", "sales"]}
    assert ann.get("/api/me").json()["folders"] == ["delivery", "sales"]
    entry = root.get("/api/admin/audit", params={"action": "admin.group_folders"}).json()[0]
    assert entry["actor"] == "root" and entry["detail"]["after"] == ["delivery", "sales"]


def test_admin_input_validation(env):
    root, rcsrf = env.login("root")
    assert (
        root.put(
            "/api/admin/groups", headers=rcsrf, json={"group_dn": SALES, "folders": ["../legal"]}
        ).status_code
        == 422
    )
    assert (
        root.put(
            "/api/admin/groups", headers=rcsrf, json={"group_dn": "junk", "folders": []}
        ).status_code
        == 422
    )
    assert (
        root.put(
            "/api/admin/overrides",
            headers=rcsrf,
            json={"username": "a b", "folder": "x", "effect": "allow"},
        ).status_code
        == 422
    )
    assert (
        root.put(
            "/api/admin/overrides",
            headers=rcsrf,
            json={"username": "ann", "folder": "x", "effect": "maybe"},
        ).status_code
        == 422
    )
    assert root.post("/api/admin/users/ann/explode", headers=rcsrf).status_code == 404


def test_ad_group_change_applies_only_at_next_login(env):
    ann, _ = env.login("ann")
    env.directory.set_groups("ann", [LEGAL])
    assert ann.get("/api/me").json()["folders"] == ["sales"]  # snapshot from login
    ann2, _ = env.login("ann")
    assert ann2.get("/api/me").json()["folders"] == ["legal"]


def test_audit_log_never_holds_passwords(env):
    env.client().post("/api/auth/login", json={"username": "ann", "password": "TopSecret-XYZ"})
    with env.factory() as db:
        dump = " ".join(
            f"{e.actor} {e.action} {e.target} {e.detail}" for e in audit.recent(db, 500)
        )
    assert "TopSecret-XYZ" not in dump


# -- permission-leak suite -------------------------------------------------------------------


def principals(env) -> dict[str, frozenset[str]]:
    """Effective folders for a matrix of users, computed the way the API does it."""
    with env.factory() as db:
        perms.set_group_folders(db, "setup", DELIVERY, ["delivery", "sales"])  # ann-like + delivery
        perms.set_override(db, "setup", "dan", "sales", "deny")
        perms.set_override(db, "setup", "nobody", "legal", "allow")
        db.commit()

        def eff(name, groups, admin=False):
            p = perms.Principal(name, name, frozenset(perms.normalize_dn(g) for g in groups), admin)
            return perms.effective_folders(db, p)

        return {
            "sales_only": eff("ann", [SALES]),
            "deny_beats_group": eff("dan", [DELIVERY]),
            "allow_override": eff("nobody", []),
            "admin_no_mapping": eff("root", [ADMINS], admin=True),
            "no_groups": eff("ghost", []),
        }


def test_permission_leak_matrix(env, embedder):
    from eval.fakes import ExtractiveChat
    from eval.runner import load_golden
    from rug.rag import Rag

    matrix = principals(env)
    assert matrix["sales_only"] == {"sales"}
    assert matrix["deny_beats_group"] == {"delivery"}
    assert matrix["allow_override"] == {"legal"}
    assert matrix["admin_no_mapping"] == frozenset() == matrix["no_groups"]

    questions = load_golden()["questions"]
    leaks = []
    with env.factory() as db:
        rag = Rag(db, embedder, ExtractiveChat())
        for who, scope in matrix.items():
            for q in questions:
                a = rag.ask(q["q"], folders=scope)
                seen = (
                    [e.folder for e in a.excerpts]
                    + [s.folder for s in a.sources]
                    + [c.doc.folder for c in a.candidates]
                    + ([a.resolved.folder] if a.resolved else [])
                )
                bad = sorted({f for f in seen if f not in scope})
                if bad:
                    leaks.append((who, q["id"], bad))
                if not scope:
                    assert a.status == "not_found" and not a.sources, (who, q["id"])
    assert leaks == []


def test_size_cap_holds_without_a_content_length(engine, corpus, embedder):
    env = Env(engine, corpus, embedder, max_upload_mb=1)
    ann, csrf = env.login("ann")
    before = sorted(p.name for p in env.docs_dir.rglob("*"))
    boundary = "xBOUNDARYx"

    def body():  # chunked: the client never states a length
        yield (
            f'--{boundary}\r\nContent-Disposition: form-data; name="folder"\r\n\r\nsales\r\n'
        ).encode()
        yield (
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="a.docx"\r\n'
            "Content-Type: application/octet-stream\r\n\r\n"
        ).encode()
        for _ in range(3 * 1024):
            yield b"\0" * 1024
        yield f"\r\n--{boundary}--\r\n".encode()

    r = ann.post(
        "/api/upload",
        headers={**csrf, "Content-Type": f"multipart/form-data; boundary={boundary}"},
        content=body(),
    )
    assert r.status_code == 413
    assert sorted(p.name for p in env.docs_dir.rglob("*")) == before
