import time

import pytest
import yaml
from sqlalchemy import func, select, text
from test_api import Env
from test_indexer import make_docx

from eval.fakes import ExtractiveChat
from rug.db.models import Document, IndexRun
from rug.indexer import _LOCK_KEY
from rug.resolver import DEFAULT_DOC_TYPES, Resolver


@pytest.fixture
def env(engine, corpus, embedder):
    return Env(engine, corpus, embedder, chat_model=ExtractiveChat())


ADMIN_ROUTES = [
    ("get", "/api/admin/users", None),
    ("get", "/api/admin/doc-types", None),
    ("put", "/api/admin/doc-types", {"name": "LEASE", "phrases": ["lease"]}),
    ("delete", "/api/admin/doc-types/SOW", None),
    ("get", "/api/admin/index", None),
    ("post", "/api/admin/index/scan", None),
    ("get", "/api/admin/qa/export", None),
    ("get", "/api/admin/qa", None),
]


def call(c, method, url, csrf, body):
    kw = {"headers": csrf}
    if body is not None:
        kw["json"] = body
    return getattr(c, method)(url, **kw)


def test_admin_routes_are_closed_to_everyone_else(env):
    anon = env.client()
    ann, acsrf = env.login("ann")
    for method, url, body in ADMIN_ROUTES:
        assert call(anon, method, url, {}, body).status_code == 401, url
        assert call(ann, method, url, acsrf, body).status_code == 403, url
    root, _ = env.login("root")
    for method, url, body in ADMIN_ROUTES:
        if method != "get":  # state changes need the CSRF token
            assert call(root, method, url, {}, body).status_code == 403, url


def test_users_list(env):
    env.login("ann")
    root, csrf = env.login("root")
    rows = {u["username"]: u for u in root.get("/api/admin/users").json()}
    assert (
        rows["ann"]["sessions"] == 1 and rows["ann"]["last_login"] and not rows["ann"]["disabled"]
    )
    root.post("/api/admin/users/ann/disable", headers=csrf)
    rows = {u["username"]: u for u in root.get("/api/admin/users").json()}
    assert rows["ann"]["disabled"] and rows["ann"]["sessions"] == 0


def test_document_types_crud_validation_and_audit(env):
    root, csrf = env.login("root")
    assert root.get("/api/admin/doc-types").json() == {}  # empty means "use the defaults"
    r = root.put(
        "/api/admin/doc-types",
        headers=csrf,
        json={"name": " lease ", "phrases": ["Lease", " tenancy  agreement ", "lease"]},
    )
    assert r.status_code == 200 and r.json() == {
        "before": None,
        "after": ["lease", "tenancy agreement"],
    }
    assert root.get("/api/admin/doc-types").json() == {"LEASE": ["lease", "tenancy agreement"]}
    bad = [
        {"name": "1x", "phrases": ["a"]},
        {"name": "x", "phrases": ["a"]},
        {"name": "OK", "phrases": []},
        {"name": "OK", "phrases": ["  "]},
        {"name": "OK", "phrases": ["semi;colon"]},
        {"name": "OK", "phrases": [f"w{i}" for i in range(21)]},
        {"name": "OK", "phrases": ["x" * 61]},
    ]
    for body in bad:
        assert root.put("/api/admin/doc-types", headers=csrf, json=body).status_code == 422, body
    assert root.delete("/api/admin/doc-types/NOPE", headers=csrf).status_code == 404
    assert root.delete("/api/admin/doc-types/lease", headers=csrf).status_code == 200
    acts = [e["action"] for e in root.get("/api/admin/audit").json()]
    assert acts.count("admin.doc_type") == 2


def test_the_resolver_uses_the_edited_vocabulary(env):
    db = env.corpus.db
    assert Resolver(db).vocab == DEFAULT_DOC_TYPES  # empty table: defaults
    root, csrf = env.login("root")
    root.put(
        "/api/admin/doc-types", headers=csrf, json={"name": "PO", "phrases": ["purchase order"]}
    )
    db.rollback()  # read the committed change
    assert Resolver(db).vocab == {"PO": ("purchase order",)}


def test_index_status_reports_runs_broken_files_and_pending_summaries(env, embedder):
    from rug.indexer import Indexer

    (env.docs_dir / "sales" / "Broken.docx").write_bytes(b"not a zip")
    with env.factory() as db:
        Indexer(db, embedder, env.docs_dir).run()
    root, _ = env.login("root")
    st = root.get("/api/admin/index").json()
    assert st["documents"]["ok"] >= 10 and st["documents"]["error"] == 1
    assert [b["path"] for b in st["broken"]] == ["sales/Broken.docx"]
    assert (
        st["runs"][0]["counts"] and st["runs"][0]["finished_at"] and st["runs"][0]["n_errors"] == 1
    )
    assert st["summaries_pending"] >= 10 and st["scanning"] is False
    assert st["scan_interval_s"] == env.settings.scan_interval_s


def test_scan_now_runs_in_the_background_and_refuses_overlap(env):
    make_docx(env.docs_dir / "sales" / "Fresh SR-4242 SOW.docx", "New scope: install pumps.")
    root, csrf = env.login("root")
    with env.factory() as db:
        before = db.scalar(select(func.count()).select_from(IndexRun))
    assert root.post("/api/admin/index/scan", headers=csrf).status_code == 202
    for _ in range(50):
        with env.factory() as db:
            done = db.scalars(
                select(Document).where(Document.filename == "Fresh SR-4242 SOW.docx")
            ).first()
            runs = db.scalar(select(func.count()).select_from(IndexRun))
        if done and runs == before + 1:
            break
        time.sleep(0.2)
    assert done is not None and runs == before + 1

    with env.engine.connect() as holder:  # another process is mid-scan
        holder.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LOCK_KEY})
        assert root.get("/api/admin/index").json()["scanning"] is True
        assert root.post("/api/admin/index/scan", headers=csrf).status_code == 409
        holder.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})


def test_thumbs_down_export_is_a_golden_skeleton(env):
    from test_chat_api import SOW, ask, final

    ann, acsrf = env.login("ann")
    root, _ = env.login("root")
    a = final(ask(ann, acsrf, SOW))
    ann.post(
        f"/api/qa/{a['qa_id']}/feedback", headers=acsrf, json={"value": -1, "comment": "too vague"}
    )
    other = final(ask(ann, acsrf, SOW))
    ann.post(f"/api/qa/{other['qa_id']}/feedback", headers=acsrf, json={"value": 1})
    r = root.get("/api/admin/qa/export")
    assert r.status_code == 200 and "attachment" in r.headers["content-disposition"]
    items = yaml.safe_load(r.text)
    assert [i["id"] for i in items] == [f"fb-{a['qa_id']}"]
    item = items[0]
    assert item["q"] == SOW and item["kind"] == "named" and item["facts"] == []
    assert item["doc"].endswith("v2 FINAL.docx") and item["note"] == "too vague"
    assert len(yaml.safe_load(root.get("/api/admin/qa/export", params={"feedback": 1}).text)) == 1


def test_map_edit_changes_another_users_view_without_relogin(env):
    ann, _ = env.login("ann")
    root, csrf = env.login("root")
    root.put(
        "/api/admin/groups",
        headers=csrf,
        json={"group_dn": "CN=Sales,OU=Groups,DC=corp,DC=local", "folders": []},
    )
    assert ann.get("/api/me").json()["folders"] == []
