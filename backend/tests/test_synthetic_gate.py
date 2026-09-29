"""M1 gate: the synthetic corpus indexes fully, facts are searchable (including table-only
and image-only facts), tracked deletions are absent, and a re-scan changes nothing."""

import pytest
from sqlalchemy import select, text

from eval.synthetic_gen import ABSENT, FACTS, build
from rug.db.models import Chunk, Document
from rug.indexer import Indexer


@pytest.fixture
def indexed(db, embedder, docs_dir):
    build(docs_dir)
    first = Indexer(db, embedder, docs_dir).run()
    return first


def _doc_text(db, path: str) -> str:
    doc = db.scalars(select(Document).where(Document.path == path)).one()
    rows = db.scalars(select(Chunk).where(Chunk.document_id == doc.id)).all()
    return "\n".join(f"{c.heading_path} {c.text}" for c in rows)


def test_all_documents_indexed_ok(db, indexed):
    assert indexed.counts == {"added": len(FACTS)}
    assert indexed.errors == []
    docs = db.scalars(select(Document)).all()
    assert {d.path for d in docs} == set(FACTS)
    assert all(d.status == "ok" and d.error is None for d in docs)
    assert {d.folder for d in docs} == {"sales", "delivery", "legal"}


@pytest.mark.parametrize("path", sorted(FACTS))
def test_facts_present(db, indexed, path):
    body = _doc_text(db, path)
    for fact in FACTS[path]:
        assert fact in body, f"{fact!r} missing from {path}"
    for gone in ABSENT.get(path, []):
        assert gone not in body, f"deleted text {gone!r} leaked into {path}"


def test_kinds_extracted(db, indexed):
    kinds = dict(
        db.execute(select(Document.path, Chunk.kind).join(Chunk).where(Chunk.kind != "text")).all()
    )
    assert kinds["delivery/Harbor Logistics SR-2210 SOW.docx"] == "table"
    assert kinds["delivery/Pinecrest Health SR-3301 SOW.docx"] == "image_text"


def test_versions_grouped_per_folder(db, indexed):
    rows = db.execute(
        select(Document.filename, Document.version_key, Document.mtime).where(
            Document.filename.like("Boost Connect SR-1098 SOW%")
        )
    ).all()
    assert len(rows) == 2 and len({r.version_key for r in rows}) == 1
    latest = max(rows, key=lambda r: r.mtime)
    assert latest.filename == "Boost Connect SR-1098 SOW v2 FINAL.docx"


def test_rescan_is_noop(db, embedder, docs_dir, indexed):
    calls = embedder.calls
    again = Indexer(db, embedder, docs_dir).run()
    assert again.counts == {"unchanged": len(FACTS)}
    assert embedder.calls == calls


def test_keyword_index_matches_ids_and_stems(db, indexed):
    def hits(q: str) -> set[str]:
        return set(
            db.scalars(
                text(
                    "SELECT DISTINCT d.filename FROM chunks c "
                    "JOIN documents d ON d.id = c.document_id "
                    "WHERE c.tsv @@ websearch_to_tsquery('simple', :q) "
                    "OR c.tsv @@ websearch_to_tsquery('english', :q)"
                ),
                {"q": q},
            )
        )

    sr1098 = hits("SR-1098")
    assert "Boost Connect SR-1098 CR-01.docx" in sr1098
    assert "Boost Connectivity SR-1089 SOW.docx" not in sr1098
    assert "Harbor Logistics SR-2210 SOW.docx" in hits("UAT sign-off")
    assert "Pinecrest Health SR-3301 SOW.docx" in hits("Melbourne datacentre")
    # English stemming: "migrating" finds "migration".
    assert "Boost Connectivity SR-1089 SOW.docx" in hits("migrating")


def test_vector_index_returns_nearest(db, embedder, indexed):
    q = embedder.embed_query("liability capped contract year")
    top = db.execute(
        select(Document.filename).join(Chunk).order_by(Chunk.embedding.cosine_distance(q)).limit(1)
    ).scalar_one()
    assert top == "Boost Connect MSA 2025.docx"
