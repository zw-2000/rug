import os
import shutil
from pathlib import Path

import pytesseract
import pytest
from conftest import FakeEmbedder
from docx import Document as Docx
from sqlalchemy import func, select, text

from rug.config import Settings
from rug.db.models import Chunk, Document
from rug.indexer import _LOCK_KEY, Indexer, IndexerBusy, MassDeletionRefused


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

    # Rename within folder: same document row and chunk ids; the filename-derived title
    # changed, so vectors (which embed the title) are refreshed in place.
    p.rename(docs_dir / "sales" / "Alpha SOW v2.docx")
    r = run(db, embedder, docs_dir)
    assert r.counts == {"moved": 1, "unchanged": 1}
    docs = docs_by_path(db)
    moved = docs["sales/Alpha SOW v2.docx"]
    assert moved.id == alpha.id
    assert moved.title == "Alpha SOW v2" and moved.version_key == "alpha sow"
    assert chunk_ids(db, moved) == alpha_chunks
    assert embedder.calls == calls + 1
    calls = embedder.calls

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

    # Copy under the same filename: chunks cloned, nothing embedded.
    shutil.copy2(docs_dir / "sales" / "Beta SOW.docx", docs_dir / "legal" / "Beta SOW.docx")
    r = run(db, embedder, docs_dir)
    assert r.counts == {"copied": 1, "unchanged": 2}
    assert embedder.calls == calls
    docs = docs_by_path(db)
    beta, copy = docs["sales/Beta SOW.docx"], docs["legal/Beta SOW.docx"]
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


def test_rename_keeps_vectors_when_title_is_not_from_filename(db, embedder, docs_dir):
    p = docs_dir / "sales" / "a.docx"
    p.parent.mkdir(parents=True)
    d = Docx()
    d.add_paragraph("Real Title", style="Title")
    d.add_paragraph("body")
    d.save(str(p))
    run(db, embedder, docs_dir)
    calls = embedder.calls
    p.rename(docs_dir / "sales" / "b.docx")
    assert run(db, embedder, docs_dir).counts == {"moved": 1}
    assert embedder.calls == calls
    assert docs_by_path(db)["sales/b.docx"].title == "Real Title"


def test_copy_with_new_filename_title_reembeds(db, embedder, docs_dir):
    make_docx(docs_dir / "sales" / "Beta.docx", "beta")
    run(db, embedder, docs_dir)
    calls = embedder.calls
    shutil.copy2(docs_dir / "sales" / "Beta.docx", docs_dir / "sales" / "Gamma.docx")
    assert run(db, embedder, docs_dir).counts == {"copied": 1, "unchanged": 1}
    assert embedder.calls == calls + 1
    assert docs_by_path(db)["sales/Gamma.docx"].title == "Gamma"


def test_unmounted_share_refuses_to_wipe_index(db, embedder, docs_dir):
    for i in range(3):
        make_docx(docs_dir / "sales" / f"d{i}.docx", f"doc {i}")
    run(db, embedder, docs_dir)
    shutil.rmtree(docs_dir / "sales")  # empty mount point
    with pytest.raises(MassDeletionRefused, match="Is the NAS share mounted"):
        run(db, embedder, docs_dir)
    assert len(docs_by_path(db)) == 3
    # Deleting most (but not all) documents is also refused unless explicitly allowed.
    for i in range(3):
        make_docx(docs_dir / "sales" / f"d{i}.docx", f"doc {i}")
    run(db, embedder, docs_dir)
    for i in range(2):
        (docs_dir / "sales" / f"d{i}.docx").unlink()
    with pytest.raises(MassDeletionRefused):
        run(db, embedder, docs_dir)
    r = Indexer(db, embedder, docs_dir, allow_mass_delete=True).run()
    assert r.counts == {"deleted": 2, "unchanged": 1}


def test_load_failures_are_listed_in_run_errors(db, embedder, docs_dir):
    (docs_dir / "sales").mkdir()
    (docs_dir / "sales" / "broken.docx").write_bytes(b"not a zip")
    r = run(db, embedder, docs_dir)
    assert r.errors == [
        {"path": "sales/broken.docx", "error": "load failed: not a valid .docx (zip) file"}
    ]


def test_db_error_on_chunk_insert_fails_only_that_file(db, docs_dir):
    class WrongDim(FakeEmbedder):
        def embed_documents(self, texts):
            if any("poison" in t for t in texts):
                return [[0.1, 0.2] for _ in texts]  # wrong dimension -> insert error
            return super().embed_documents(texts)

    make_docx(docs_dir / "sales" / "a poison.docx", "x")
    make_docx(docs_dir / "sales" / "b ok.docx", "y")
    r = Indexer(db, WrongDim(), docs_dir).run()
    assert r.counts == {"failed": 1, "added": 1}
    assert r.finished_at is not None
    assert [e["path"] for e in r.errors] == ["sales/a poison.docx"]
    assert set(docs_by_path(db)) == {"sales/b ok.docx"}


def test_embed_dim_mismatch_rejected_up_front(db, embedder, docs_dir):
    with pytest.raises(ValueError, match="RUG_EMBED_DIM=1024"):
        Indexer(db, embedder, docs_dir, settings=Settings(embed_dim=1024))


def test_tesseract_outage_is_retried_not_recorded(db, embedder, docs_dir, monkeypatch):
    from eval.synthetic_gen import _image

    p = docs_dir / "sales" / "img.docx"
    p.parent.mkdir(parents=True)
    d = Docx()
    d.add_picture(_image("Melbourne DC2", (900, 300)))
    d.save(str(p))

    def missing(*a, **k):
        raise pytesseract.TesseractNotFoundError()

    monkeypatch.setattr(pytesseract, "image_to_string", missing)
    r = run(db, embedder, docs_dir)
    assert r.counts == {"failed": 1} and "Tesseract is not installed" in r.errors[0]["error"]
    assert docs_by_path(db) == {}  # nothing recorded, so the next run retries
    monkeypatch.undo()
    assert run(db, embedder, docs_dir).counts == {"added": 1}


def test_symlinks_are_not_followed(db, embedder, docs_dir, tmp_path):
    outside = make_docx(tmp_path / "outside" / "secret.docx", "secret")
    legal = make_docx(docs_dir / "legal" / "contract.docx", "legal only")
    (docs_dir / "sales").mkdir()
    (docs_dir / "sales" / "secret.docx").symlink_to(outside)
    (docs_dir / "sales" / "contract.docx").symlink_to(legal)
    (docs_dir / "sales" / "linked_dir").symlink_to(tmp_path / "outside", target_is_directory=True)
    run(db, embedder, docs_dir)
    assert set(docs_by_path(db)) == {"legal/contract.docx"}


def test_file_vanishing_mid_scan_is_ignored(docs_dir, monkeypatch):
    from rug import indexer as indexer_mod

    make_docx(docs_dir / "sales" / "a.docx", "a")
    make_docx(docs_dir / "sales" / "b.docx", "b")
    real_lstat = Path.lstat

    def flaky(self):
        if self.name == "a.docx":
            raise FileNotFoundError(self)
        return real_lstat(self)

    monkeypatch.setattr(Path, "lstat", flaky)
    assert set(indexer_mod.scan_disk(docs_dir)) == {"sales/b.docx"}


def test_renaming_every_file_is_not_a_mass_delete(db, embedder, docs_dir):
    for i in range(3):
        make_docx(docs_dir / "sales" / f"d{i}.docx", f"doc {i}")
    run(db, embedder, docs_dir)
    (docs_dir / "sales").rename(docs_dir / "delivery")  # folder restructure
    assert run(db, embedder, docs_dir).counts == {"moved": 3}
    assert {d.folder for d in docs_by_path(db).values()} == {"delivery"}
