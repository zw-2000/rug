"""Question answering over HTTP: a streamed answer, feedback, and the admin Q&A log.

`POST /api/chat` answers as Server-Sent Events over a normal fetch (EventSource is GET-only
and cannot carry the CSRF header). Events, in order:

  status  {"state": "queued" | "working"}      queued = waiting for the local model's turn
  token   {"text": ...}                        PROVISIONAL model output; may be replaced
  answer  {...}                                 the only authoritative result (validated text,
                                                sources, candidates); replaces any draft
  error   {"code", "message"}                  failure after the stream started

What may be searched is always `perms.effective_folders(principal)`, never anything the
client sends. Sources and candidates appear only in the final event, after citation checking.
"""

import asyncio
import json
import logging
import threading
import time
import uuid
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from rug import perms
from rug.auth import sessions
from rug.config import Settings
from rug.db.models import QaLog
from rug.llm import ChatError, ChatModel, EmbeddingError
from rug.rag import NOT_FOUND, Answer, DocumentNotAvailable, Rag

log = logging.getLogger(__name__)

KEEPALIVE_S = 15.0
UNAVAILABLE = "The answering service is unavailable. Try again in a moment."
INTERNAL = "Something went wrong answering that. Try again."


class _Cancelled(Exception):
    """The client went away; stop spending model time on its question."""


class Gate:
    """Lets `n` questions use the local model at once; the rest wait their turn."""

    def __init__(self, n: int):
        self._sem = threading.BoundedSemaphore(max(1, n))
        self._lock = threading.Lock()
        self.waiting = 0

    def acquire(self, cancelled: threading.Event, on_wait: Callable[[], None]) -> bool:
        if self._sem.acquire(blocking=False):
            return True
        with self._lock:
            self.waiting += 1
        on_wait()
        try:
            while not cancelled.is_set():
                if self._sem.acquire(timeout=0.5):
                    return True
            return False
        finally:
            with self._lock:
                self.waiting -= 1

    def release(self) -> None:
        self._sem.release()


class ChatBody(BaseModel):
    question: str = Field(max_length=10_000)  # the real limit (RUG_MAX_QUESTION_CHARS) is below
    pinned: str | None = Field(default=None, max_length=64)


class FeedbackBody(BaseModel):
    value: int | None  # 1, -1, or null to clear
    comment: str | None = Field(default=None, max_length=2000)


def sse(event: str, data: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


def answer_payload(a: Answer, qa_id: int) -> dict[str, Any]:
    """What the browser is told. Only documents already inside the caller's scope get here."""
    d = a.resolved
    return {
        "qa_id": qa_id,
        "status": a.status,
        "text": a.text,
        "mode": a.mode,
        "ungrounded": a.ungrounded,
        "note": a.note,
        "resolved": None
        if d is None
        else {
            "id": str(d.id),
            "filename": d.filename,
            "title": d.title,
            "folder": d.folder,
            "n_versions": d.n_versions,
        },
        "sources": [
            {
                "id": str(s.document_id),
                "filename": s.filename,
                "title": s.title,
                "folder": s.folder,
                "sections": s.sections,
                "refs": s.refs,
                "snippet": s.snippet,
                "n_versions": s.n_versions,
            }
            for s in a.sources
        ],
        "candidates": [
            {
                "id": str(c.doc.id),
                "filename": c.doc.filename,
                "title": c.doc.title,
                "folder": c.doc.folder,
            }
            for c in a.candidates
        ],
    }


def not_found_answer() -> Answer:
    return Answer("not_found", NOT_FOUND, mode="unavailable")


def purge_qa(db: Session, days: int) -> None:
    cutoff = datetime.now(UTC) - timedelta(days=days)
    db.execute(delete(QaLog).where(QaLog.created_at < cutoff))


def register_chat(
    app: FastAPI,
    *,
    s: Settings,
    factory: Callable[[], Session],
    get_db: Callable[..., Any],
    current: Callable[..., sessions.Active],
    admin: Callable[..., sessions.Active],
    embedder: Any,
    chat_model: ChatModel,
) -> None:
    gate = Gate(s.chat_concurrency)
    last_purge = 0.0

    def finish(qa_id: int, answer: Answer | None, status: str, started: float) -> None:
        with factory() as wdb:
            row = wdb.get(QaLog, qa_id)
            if row is None:
                return
            row.status = status
            row.latency_ms = int((time.monotonic() - started) * 1000)
            row.model = s.chat_model
            if answer is not None:
                row.status = answer.status
                row.mode = answer.mode
                row.resolved_doc = answer.resolved.id if answer.resolved else None
                row.chunk_ids = [e.chunk_id for e in answer.excerpts]
                row.answer = answer.text
                row.ungrounded = answer.ungrounded
            wdb.commit()

    def work(
        emit: Callable[[str, dict[str, Any]], None],
        cancelled: threading.Event,
        folders: frozenset[str],
        question: str,
        pinned: uuid.UUID | None,
        qa_id: int,
    ) -> None:
        started = time.monotonic()
        try:
            if not gate.acquire(cancelled, lambda: emit("status", {"state": "queued"})):
                finish(qa_id, None, "cancelled", started)
                return
            try:
                if cancelled.is_set():
                    finish(qa_id, None, "cancelled", started)
                    return
                emit("status", {"state": "working"})

                def on_token(t: str) -> None:
                    if cancelled.is_set():
                        raise _Cancelled
                    emit("token", {"text": t})

                with factory() as wdb:
                    try:
                        answer = Rag(wdb, embedder, chat_model, s).ask(
                            question, folders=folders, pinned=pinned, on_token=on_token
                        )
                    except DocumentNotAvailable:
                        # Same reply as any other not-found: never confirm a forbidden document.
                        answer = not_found_answer()
            finally:
                gate.release()
            finish(qa_id, answer, "answered", started)
            emit("answer", answer_payload(answer, qa_id))
        except _Cancelled:
            finish(qa_id, None, "cancelled", started)
        except (ChatError, EmbeddingError):
            log.exception("model unavailable while answering")
            finish(qa_id, None, "error", started)
            emit("error", {"code": "unavailable", "message": UNAVAILABLE})
        except Exception:
            log.exception("answering failed")
            finish(qa_id, None, "error", started)
            emit("error", {"code": "internal", "message": INTERNAL})

    @app.post("/api/chat")
    async def chat(
        body: ChatBody,
        active: sessions.Active = Depends(current),
        db: Session = Depends(get_db),
    ) -> StreamingResponse:
        nonlocal last_purge
        question = body.question.strip()
        if not question:
            raise HTTPException(422, "Type a question.")
        if len(question) > s.max_question_chars:
            raise HTTPException(
                422, f"That question is too long (limit {s.max_question_chars} characters)."
            )
        if gate.waiting >= s.chat_max_queue:
            raise HTTPException(503, "Too many questions are waiting. Try again shortly.")
        pinned: uuid.UUID | None = None
        unavailable = False
        if body.pinned:
            try:
                pinned = uuid.UUID(body.pinned)
            except ValueError:
                unavailable = True  # a malformed id is answered exactly like an unknown one
        p = active.principal
        folders = perms.effective_folders(db, p)
        row = QaLog(username=p.username, question=question, model=s.chat_model)
        db.add(row)
        if time.monotonic() - last_purge > 3600:
            last_purge = time.monotonic()
            purge_qa(db, s.qa_retention_days)
        db.commit()
        qa_id = row.id

        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[tuple[str, dict[str, Any]]] = asyncio.Queue()
        cancelled = threading.Event()

        def emit(event: str, data: dict[str, Any]) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, (event, data))

        if unavailable:
            answer = not_found_answer()
            finish(qa_id, answer, "answered", time.monotonic())
            emit("answer", answer_payload(answer, qa_id))
        else:
            threading.Thread(
                target=work,
                args=(emit, cancelled, folders, question, pinned, qa_id),
                daemon=True,
            ).start()

        async def stream() -> AsyncIterator[bytes]:
            try:
                while True:
                    try:
                        event, data = await asyncio.wait_for(queue.get(), KEEPALIVE_S)
                    except TimeoutError:
                        yield b": keepalive\n\n"
                        continue
                    yield sse(event, data)
                    if event in ("answer", "error"):
                        return
            finally:
                cancelled.set()  # client left (or we finished): stop any remaining work

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/qa/{qa_id}/feedback")
    def feedback(
        qa_id: int,
        body: FeedbackBody,
        active: sessions.Active = Depends(current),
        db: Session = Depends(get_db),
    ) -> dict[str, bool]:
        if body.value not in (1, -1, None):
            raise HTTPException(422, "value must be 1, -1 or null")
        row = db.get(QaLog, qa_id)
        if row is None or row.username != active.principal.username:
            raise HTTPException(404, "Answer not found.")  # never confirm someone else's row
        row.feedback, row.comment = body.value, (body.comment or None)
        db.commit()
        return {"ok": True}

    @app.get("/api/admin/qa")
    def qa_list(
        limit: int = 100,
        before_id: int | None = None,
        feedback: int | None = None,
        _: sessions.Active = Depends(admin),
        db: Session = Depends(get_db),
    ) -> list[dict[str, Any]]:
        q = select(QaLog).order_by(QaLog.id.desc()).limit(max(1, min(limit, 500)))
        if before_id is not None:
            q = q.where(QaLog.id < before_id)
        if feedback in (1, -1):
            q = q.where(QaLog.feedback == feedback)
        return [
            {
                "id": r.id,
                "at": r.created_at.isoformat(),
                "username": r.username,
                "question": r.question,
                "status": r.status,
                "mode": r.mode,
                "resolved_doc": str(r.resolved_doc) if r.resolved_doc else None,
                "chunk_ids": r.chunk_ids,
                "answer": r.answer,
                "ungrounded": r.ungrounded,
                "latency_ms": r.latency_ms,
                "feedback": r.feedback,
                "comment": r.comment,
            }
            for r in db.scalars(q)
        ]
