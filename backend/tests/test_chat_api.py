import json
import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select
from test_api import SALES, Env

from eval.fakes import ExtractiveChat
from eval.runner import load_golden
from rug import perms
from rug.api.chat import Gate, purge_qa
from rug.db.models import Document, QaLog
from rug.llm import ChatError
from rug.rag import NOT_FOUND

SOW = "Boost Connect SR-1098 SOW - what is the scope of work mainly about?"
V2 = "sales/Boost Connect SR-1098 SOW v2 FINAL.docx"


def events(resp) -> list[tuple[str, dict]]:
    out = []
    for block in resp.text.split("\n\n"):
        if not block.strip() or block.startswith(":"):
            continue
        name, _, data = block.partition("\ndata: ")
        out.append((name.removeprefix("event: "), json.loads(data)))
    return out


def ask(c, csrf, question, pinned=None):
    return c.post("/api/chat", headers=csrf, json={"question": question, "pinned": pinned})


def final(resp) -> dict:
    ev = events(resp)
    assert ev[-1][0] == "answer", ev[-1]
    return ev[-1][1]


@pytest.fixture
def env(engine, corpus, embedder):
    return Env(engine, corpus, embedder, chat_model=ExtractiveChat())


def test_chat_needs_a_session_and_csrf(env):
    assert env.client().post("/api/chat", json={"question": "hi"}).status_code == 401
    ann, _ = env.login("ann")
    assert ann.post("/api/chat", json={"question": "hi"}).status_code == 403


def test_named_question_streams_a_cited_answer(env):
    ann, csrf = env.login("ann")
    r = ask(ann, csrf, SOW)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    names = [n for n, _ in events(r)]
    assert names[0] == "status" and names[-1] == "answer" and "token" in names
    a = final(r)
    assert a["status"] == "answered" and a["mode"] == "named" and not a["ungrounded"]
    assert a["resolved"]["filename"].endswith("v2 FINAL.docx")
    assert [s["filename"] for s in a["sources"]] == ["Boost Connect SR-1098 SOW v2 FINAL.docx"]
    assert a["sources"][0]["refs"] and "[1]" in a["text"]
    with env.factory() as db:
        row = db.get(QaLog, a["qa_id"])
        assert row.username == "ann" and row.status == "answered" and row.answer == a["text"]
        assert row.chunk_ids and row.latency_ms is not None and row.resolved_doc


def test_input_limits(env):
    ann, csrf = env.login("ann")
    assert ask(ann, csrf, "   ").status_code == 422
    assert ask(ann, csrf, "x" * (env.settings.max_question_chars + 1)).status_code == 422
    assert ask(ann, csrf, "x" * 20_000).status_code == 422


def test_no_leak_over_http(env):
    """Closes the M3 gap: session -> effective folders -> ask, through the real endpoint."""
    with env.factory() as db:
        secret_files = [d.filename for d in db.scalars(select(Document)) if d.folder != "sales"]
    assert secret_files
    ann, csrf = env.login("ann")
    leaks = []
    for q in load_golden()["questions"]:
        r = ask(ann, csrf, q["q"])
        a = final(r)
        seen = (
            [s["folder"] for s in a["sources"]]
            + [c["folder"] for c in a["candidates"]]
            + ([a["resolved"]["folder"]] if a["resolved"] else [])
        )
        if any(f != "sales" for f in seen):
            leaks.append((q["id"], seen))
        echoed = q["q"].lower()
        for name in secret_files:
            if name.lower() not in echoed and name in r.text:
                leaks.append((q["id"], name))
        for bad in q.get("forbidden", []):
            if bad.lower() not in echoed and bad in r.text:
                leaks.append((q["id"], bad))
    assert leaks == []


def test_pinned_forbidden_document_looks_like_an_unknown_one(env):
    with env.factory() as db:
        legal = db.scalars(select(Document).where(Document.folder == "legal")).first()
    ann, csrf = env.login("ann")
    replies = [
        final(ask(ann, csrf, "What is the liability cap?", pinned=str(pid)))
        for pid in (legal.id, uuid.uuid4(), "not-a-uuid")
    ]
    for a in replies:
        a.pop("qa_id")
    assert replies[0] == replies[1] == replies[2]
    assert replies[0]["status"] == "not_found" and replies[0]["text"] == NOT_FOUND
    assert replies[0]["resolved"] is None and replies[0]["sources"] == []


def test_ambiguous_question_returns_a_picker_within_scope_then_pinning_answers(env, embedder):
    from test_resolver import _tiny

    from rug.indexer import Indexer

    _tiny(env.docs_dir / "sales" / "Orion SOW SR-5000.docx", "sales scope: widgets")
    _tiny(env.docs_dir / "delivery" / "Orion SOW SR-5000.docx", "delivery scope: gadgets")
    _tiny(env.docs_dir / "legal" / "Orion SOW SR-5000.docx", "legal scope: secrets")
    with env.factory() as db:
        Indexer(db, embedder, env.docs_dir).run()
        perms.set_group_folders(db, "setup", SALES, ["sales", "delivery"])
        db.commit()
    ann, csrf = env.login("ann")
    a = final(ask(ann, csrf, "Orion SOW SR-5000 scope"))
    assert a["status"] == "needs_choice" and a["sources"] == []
    assert sorted(c["folder"] for c in a["candidates"]) == ["delivery", "sales"]  # never legal
    b = final(ask(ann, csrf, "what is the scope?", pinned=a["candidates"][0]["id"]))
    assert b["status"] == "answered" and b["mode"] == "pinned"
    dan, dcsrf = env.login("dan")  # delivery only: one visible copy, so no picker
    only = final(ask(dan, dcsrf, "Orion SOW SR-5000 scope"))
    assert only["status"] == "answered" and only["resolved"]["folder"] == "delivery"


def test_model_failure_is_an_error_event_without_details(engine, corpus, embedder):
    class Broken:
        def chat(self, messages, on_token=None):
            raise ChatError("cannot reach Ollama at http://10.9.9.9:11434: secret detail")

    env = Env(engine, corpus, embedder, chat_model=Broken())
    ann, csrf = env.login("ann")
    r = ask(ann, csrf, SOW)
    assert r.status_code == 200
    name, data = events(r)[-1]
    assert name == "error" and data["code"] == "unavailable"
    assert "10.9.9.9" not in r.text and "secret" not in r.text
    with env.factory() as db:
        assert db.scalars(select(QaLog)).one().status == "error"


def test_full_queue_is_a_503(engine, corpus, embedder):
    env = Env(engine, corpus, embedder, chat_model=ExtractiveChat(), chat_max_queue=0)
    ann, csrf = env.login("ann")
    assert ask(ann, csrf, SOW).status_code == 503


def test_gate_orders_turns_and_honours_cancellation():
    gate = Gate(1)
    never = threading.Event()
    assert gate.acquire(never, lambda: None)
    queued = []
    cancelled = threading.Event()
    result = []

    def waiter():
        result.append(gate.acquire(cancelled, lambda: queued.append(1)))

    t = threading.Thread(target=waiter)
    t.start()
    while not queued:
        pass
    assert gate.waiting == 1
    cancelled.set()
    t.join(3)
    assert result == [False] and gate.waiting == 0
    gate.release()
    assert gate.acquire(never, lambda: None)  # the turn is free again


# -- feedback and the admin Q&A log --------------------------------------------------------


def test_feedback_belongs_to_the_asker(env):
    ann, acsrf = env.login("ann")
    dan, dcsrf = env.login("dan")
    root, rcsrf = env.login("root")
    qa = final(ask(ann, acsrf, SOW))["qa_id"]
    assert dan.post(f"/api/qa/{qa}/feedback", headers=dcsrf, json={"value": -1}).status_code == 404
    assert ann.post("/api/qa/99999/feedback", headers=acsrf, json={"value": 1}).status_code == 404
    assert ann.post(f"/api/qa/{qa}/feedback", headers=acsrf, json={"value": 5}).status_code == 422
    assert ann.post(f"/api/qa/{qa}/feedback", json={"value": 1}).status_code == 403  # CSRF
    r = ann.post(f"/api/qa/{qa}/feedback", headers=acsrf, json={"value": -1, "comment": "wrong"})
    assert r.status_code == 200
    rows = root.get("/api/admin/qa", params={"feedback": -1}).json()
    assert [(x["id"], x["comment"], x["username"]) for x in rows] == [(qa, "wrong", "ann")]
    assert ann.get("/api/admin/qa").status_code == 403
    ann.post(f"/api/qa/{qa}/feedback", headers=acsrf, json={"value": None})
    assert root.get("/api/admin/qa", params={"feedback": -1}).json() == []


def test_qa_log_is_purged_after_retention(env):
    ann, csrf = env.login("ann")
    final(ask(ann, csrf, SOW))
    with env.factory() as db:
        db.query(QaLog).update({"created_at": datetime.now(UTC) - timedelta(days=91)})
        db.commit()
        purge_qa(db, 90)
        db.commit()
        assert db.query(QaLog).count() == 0


# -- versions, upload folders ---------------------------------------------------------------


def test_versions_endpoint(env):
    ann, _ = env.login("ann")
    with env.factory() as db:
        v2 = db.scalars(select(Document).where(Document.path == V2)).one()
        legal = db.scalars(select(Document).where(Document.folder == "legal")).first()
    r = ann.get(f"/api/documents/{v2.id}/versions")
    assert r.status_code == 200
    names = [v["filename"] for v in r.json()]
    assert "Boost Connect SR-1098 SOW v1.docx" in names and len(names) >= 2
    assert sum(v["latest"] for v in r.json()) == 1
    assert ann.get(f"/api/documents/{legal.id}/versions").status_code == 403
    assert ann.get(f"/api/documents/{uuid.uuid4()}/versions").status_code == 404


def test_upload_folders_lists_only_folders_that_exist(env):
    with env.factory() as db:
        perms.set_group_folders(db, "setup", SALES, ["sales", "ghost", ""])
        db.commit()
    ann, _ = env.login("ann")
    me = ann.get("/api/me").json()
    assert set(me["folders"]) == {"sales", "ghost", ""} and me["upload_folders"] == ["sales"]


# -- static frontend and headers -------------------------------------------------------------


def test_spa_serving_and_headers(engine, corpus, embedder, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>rug</title>")
    (dist / "assets" / "app-abc123.js").write_text("console.log(1)")
    env = Env(engine, corpus, embedder, chat_model=ExtractiveChat(), static_dir=str(dist))
    c = env.client()
    for path in ("/", "/chat", "/upload"):
        r = c.get(path)
        assert r.status_code == 200 and "<title>rug</title>" in r.text
    a = c.get("/assets/app-abc123.js")
    assert a.status_code == 200 and "immutable" in a.headers["cache-control"]
    nf = c.get("/api/nope")
    assert nf.status_code == 404 and nf.headers["content-type"].startswith("application/json")
    assert c.get("/assets/missing.js").status_code == 404
    h = c.get("/healthz").headers
    assert "default-src 'self'" in h["content-security-policy"]
    assert h["x-frame-options"] == "DENY" and h["x-content-type-options"] == "nosniff"
    assert (
        "script-src" not in h["content-security-policy"]
        or "unsafe" not in h["content-security-policy"]
    )


def test_missing_frontend_build_fails_at_startup(engine, corpus, embedder, tmp_path):
    with pytest.raises(FileNotFoundError):
        Env(engine, corpus, embedder, static_dir=str(tmp_path / "nope"))
