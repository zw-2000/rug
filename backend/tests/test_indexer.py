import os
import shutil
from pathlib import Path

import pytest
from docx import Document as Docx
from sqlalchemy import func, select, text

from rug.db.models import Chunk, Document
from rug.indexer import _LOCK_KEY, Indexer, IndexerBusy


def make_docx(path: Path, body: str, mtime: float | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    d = Docx()
    d.add_heading("Scope", level=1)
    d.add_paragraph(body)
    d.save(str(path))
    if mtime is not None:
        os.utime(path, (mtime, mtime))
    return path


def docs_by_path(db) -> dict[str, Document]:
    db.expire_all()
    return {d.path: d for d in db.scalars(select(Document))}


def chunk_ids(db, doc: Document) -> list[int]:
    return list(db.scalars(select(Chunk.id).where(Chunk.document_id == doc.id).order_by(Chunk.ord)))


def run(db, embedder, docs_dir):
    return Indexer(db, embedder, docs_dir).run()


def test_full_lifecycle(db, embedder, docs_dir):
    make_docx(docs_dir / "sales" / "Alpha SOW.docx", "Alpha scope text.")
    make_docx(docs_dir / "sales" / "Beta SOW.docx", "Beta scope text.")
    (docs_dir / "sales" / "~$Alpha SOW.docx").write_bytes(b"lock file")
    (docs_dir / "sales" / "notes.txt").write_text("not a registered format")
    (docs_dir / ".hidden").mkdir()
    make_docx(docs_dir / ".hidden" / "x.docx", "ignored")

    r = run(db, embedder, docs_dir)
    assert r.counts == {"added": 2}
    assert r.total == 2 and r.processed == 2 and r.finished_at is not None
    docs = docs_by_path(db)
    assert set(docs) == {"sales/Alpha SOW.docx", "sales/Beta SOW.docx"}
    alpha = docs["sales/Alpha SOW.docx"]
    assert alpha.folder == "sales" and alpha.title == "Alpha SOW"
    alpha_chunks = chunk_ids(db, alpha)
    assert alpha_chunks

    # Re-scan: nothing changes, nothing is embedded.
    calls = embedder.calls
    r = run(db, embedder, docs_dir)
    assert r.counts == {"unchanged": 2}
    assert embedder.calls == calls

    # Touch without content change: mtime updated, still no embedding.
    p = docs_dir / "sales" / "Alpha SOW.docx"
    os.utime(p, (1_800_000_000, 1_800_000_000))
    r = run(db, embedder, docs_dir)
    assert r.counts == {"unchanged": 2}
    assert embedder.calls == calls
    assert docs_by_path(db)["sales/Alpha SOW.docx"].mtime == 1_800_000_000

    # Rename within folder: same document row, same chunks, title follows filename.
    p.rename(docs_dir / "sales" / "Alpha SOW v2.docx")
    r = run(db, embedder, docs_dir)
    assert r.counts == {"moved": 1, "unchanged": 1}
    docs = docs_by_path(db)
    moved = docs["sales/Alpha SOW v2.docx"]
    assert moved.id == alpha.id
    assert moved.title == "Alpha SOW v2" and moved.version_key == "alpha sow"
    assert chunk_ids(db, moved) == alpha_chunks
    assert embedder.calls == calls

    # Move to another permission folder: folder is re-tagged, chunks untouched.
    (docs_dir / "legal").mkdir()
    shutil.move(docs_dir / "sales" / "Alpha SOW v2.docx", docs_dir / "legal" / "Alpha SOW v2.docx")
    r = run(db, embedder, docs_dir)
    assert r.counts == {"moved": 1, "unchanged": 1}
    moved = docs_by_path(db)["legal/Alpha SOW v2.docx"]
    assert (moved.id, moved.folder) == (alpha.id, "legal")
    assert chunk_ids(db, moved) == alpha_chunks

    # Edit content: re-indexed in place (same id, new chunks, new text).
    make_docx(docs_dir / "legal" / "Alpha SOW v2.docx", "Completely new alpha scope.")
    r = run(db, embedder, docs_dir)
    assert r.counts == {"updated": 1, "unchanged": 1}
    assert embedder.calls == calls + 1
    edited = docs_by_path(db)["legal/Alpha SOW v2.docx"]
    assert edited.id == alpha.id
    texts = db.scalars(select(Chunk.text).where(Chunk.document_id == edited.id)).all()
    assert any("Completely new" in t for t in texts)
    calls = embedder.calls

    # Copy: new document, chunks cloned without embedding.
    shutil.copy2(docs_dir / "sales" / "Beta SOW.docx", docs_dir / "legal" / "Beta SOW copy.docx")
    r = run(db, embedder, docs_dir)
    assert r.counts == {"copied": 1, "unchanged": 2}
    assert embedder.calls == calls
    docs = docs_by_path(db)
    beta, copy = docs["sales/Beta SOW.docx"], docs["legal/Beta SOW copy.docx"]
    assert copy.id != beta.id and copy.sha256 == beta.sha256
    assert len(chunk_ids(db, copy)) == len(chunk_ids(db, beta))

    # Delete: document and its chunks are gone.
    (docs_dir / "sales" / "Beta SOW.docx").unlink()
    r = run(db, embedder, docs_dir)
    assert r.counts == {"deleted": 1, "unchanged": 2}
    assert "sales/Beta SOW.docx" not in docs_by_path(db)
    assert db.scalar(select(func.count()).where(Chunk.document_id == beta.id)) == 0


def test_broken_file_recorded_and_not_retried(db, embedder, docs_dir):
    (docs_dir / "sales").mkdir()
    (docs_dir / "sales" / "broken.docx").write_bytes(b"this is not a zip")
    make_docx(docs_dir / "sales" / "ok.docx", "fine")

    r = run(db, embedder, docs_dir)
    assert r.counts == {"added": 1, "failed": 1}
    broken = docs_by_path(db)["sales/broken.docx"]
    assert broken.status == "error" and "load failed" in (broken.error or "")

    r = run(db, embedder, docs_dir)
    assert r.counts == {"unchanged": 2}


def test_embedding_outage_does_not_poison_catalog(db, docs_dir):
    class DownEmbedder:
        def embed_documents(self, texts):
            raise ConnectionError("ollama down")

        def embed_query(self, text):
            raise ConnectionError("ollama down")

    make_docx(docs_dir / "sales" / "a.docx", "text")
    r = Indexer(db, DownEmbedder(), docs_dir).run()
    assert r.counts == {"failed": 1}
    assert r.errors[0]["path"] == "sales/a.docx"
    assert docs_by_path(db) == {}  # nothing half-written; retried on the next run


def test_root_level_files_have_empty_folder(db, embedder, docs_dir):
    make_docx(docs_dir / "loose.docx", "x")
    run(db, embedder, docs_dir)
    assert docs_by_path(db)["loose.docx"].folder == ""


def test_concurrent_run_refused(db, embedder, docs_dir, engine):
    with engine.connect() as other:
        other.execute(text("SELECT pg_advisory_lock(:k)"), {"k": _LOCK_KEY})
        try:
            with pytest.raises(IndexerBusy):
                run(db, embedder, docs_dir)
        finally:
            other.execute(text("SELECT pg_advisory_unlock(:k)"), {"k": _LOCK_KEY})
