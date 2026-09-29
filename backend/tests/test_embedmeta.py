import logging

import pytest
from conftest import FakeChat
from sqlalchemy import select
from test_indexer import make_docx

from eval.fakes import HashEmbedder
from rug import embedmeta
from rug.config import Settings
from rug.db.models import Chunk, DocumentSummary, IndexMeta
from rug.embedmeta import EmbeddingModelMismatch
from rug.indexer import Indexer
from rug.summaries import summarize_pending
from rug.worker import run_cycle


class OtherModel(HashEmbedder):
    """A different embedding model: same size, different vectors."""

    def _vec(self, s: str) -> list[float]:
        return super()._vec(s + " zz-other-model")


@pytest.fixture
def share(docs_dir):
    (docs_dir / "sales").mkdir()
    make_docx(docs_dir / "sales" / "Acme SOW SR-1.docx", "Scope: paint the fence.")
    return docs_dir


def settings(share, model="nomic-embed-text", **kw):
    return Settings(_env_file=None, docs_dir=share, embed_model=model, **kw)


def test_first_run_records_the_model_and_a_repeat_is_fine(db, embedder, share):
    Indexer(db, embedder, share, settings(share)).run()
    assert db.get(IndexMeta, embedmeta.KEY).value.startswith("nomic-embed-text|")
    Indexer(db, embedder, share, settings(share)).run()  # same model: no complaint


def test_changing_the_model_is_refused_everywhere_with_the_fix_named(db, embedder, share):
    Indexer(db, embedder, share, settings(share)).run()
    new = settings(share, model="mxbai-embed-large")
    with pytest.raises(EmbeddingModelMismatch, match="rug reembed") as e:
        Indexer(db, embedder, share, new).run()
    assert "nomic-embed-text" in str(e.value) and "mxbai-embed-large" in str(e.value)
    with pytest.raises(EmbeddingModelMismatch):
        Indexer(db, embedder, share, new).sync_one("sales/Acme SOW SR-1.docx")
    with pytest.raises(EmbeddingModelMismatch):
        summarize_pending(db, FakeChat("x"), embedder, new)
    out = run_cycle(lambda: db, embedder, FakeChat("x"), new, poll_s=0)  # the worker reports it
    assert out == {"scan": "model-mismatch", "summaries": "model-mismatch"}


def test_changed_task_prefixes_count_as_a_different_model(db, embedder, share):
    Indexer(db, embedder, share, settings(share)).run()
    with pytest.raises(EmbeddingModelMismatch):
        Indexer(db, embedder, share, settings(share, embed_query_prefix="query: ")).run()


def test_an_index_without_a_record_is_adopted_with_a_warning(db, embedder, share, caplog):
    Indexer(db, embedder, share, settings(share)).run()
    db.query(IndexMeta).delete()
    db.commit()
    with caplog.at_level(logging.WARNING):
        embedmeta.check(db, settings(share, model="something-else"))
    assert "no record of which embedding model" in caplog.text
    assert db.get(IndexMeta, embedmeta.KEY).value.startswith("something-else|")


def test_reembed_rebuilds_every_vector_and_then_the_new_model_works(db, embedder, share):
    old = settings(share)
    Indexer(db, embedder, share, old).run()
    summarize_pending(db, FakeChat("An overview of painting."), embedder, old)
    before = {c.id: list(c.embedding) for c in db.scalars(select(Chunk))}
    before_summary = [list(s.embedding) for s in db.scalars(select(DocumentSummary))]
    assert before and before_summary

    new = settings(share, model="other-model")
    other = OtherModel()
    progress = []
    done = Indexer(db, other, share, new).reembed_all(lambda n, t: progress.append((n, t)))
    assert done == {"documents": 1, "summaries": 1} and progress == [(1, 1)]

    db.expire_all()
    for c in db.scalars(select(Chunk)):
        assert list(c.embedding) != before[c.id]  # every chunk was re-embedded, ids preserved
    assert [list(s.embedding) for s in db.scalars(select(DocumentSummary))] != before_summary
    assert db.get(IndexMeta, embedmeta.KEY).value.startswith("other-model|")
    Indexer(db, other, share, new).run()  # allowed again
    with pytest.raises(EmbeddingModelMismatch):  # and the old setting is now the wrong one
        Indexer(db, embedder, share, old).run()


def test_an_interrupted_reembed_leaves_the_index_marked_unusable(db, embedder, share):
    make_docx(share / "sales" / "Second SOW SR-2.docx", "Scope: mow the lawn.")
    old = settings(share)
    Indexer(db, embedder, share, old).run()
    new = settings(share, model="other-model")

    class Dies(OtherModel):
        def embed_documents(self, texts):
            if self.calls >= 1:
                raise ConnectionError("ollama went away")
            return super().embed_documents(texts)

    with pytest.raises(ConnectionError):
        Indexer(db, Dies(), share, new).reembed_all()
    assert "unfinished" in db.get(IndexMeta, embedmeta.KEY).value
    for s in (old, new):  # neither setting may use a half-converted index
        with pytest.raises(EmbeddingModelMismatch):
            Indexer(db, embedder, share, s).run()
    Indexer(db, OtherModel(), share, new).reembed_all()  # running it again finishes the job
    Indexer(db, OtherModel(), share, new).run()
