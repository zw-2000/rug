import threading

import pytest
from conftest import FakeChat
from test_indexer import make_docx

from rug.config import Settings
from rug.db.models import Document, DocumentSummary
from rug.llm import ChatError, EmbeddingError
from rug.worker import run_cycle, serve_forever


@pytest.fixture
def share(docs_dir):
    (docs_dir / "sales").mkdir()
    make_docx(docs_dir / "sales" / "Acme SOW SR-1.docx", "Scope: paint the fence.")
    return docs_dir


def settings_for(share):
    return Settings(_env_file=None, docs_dir=share)


def test_a_cycle_indexes_then_summarises(engine, db, embedder, share):
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(engine, expire_on_commit=False)
    out = run_cycle(factory, embedder, FakeChat("An overview."), settings_for(share), poll_s=0)
    assert out["scan"] == {"added": 1}
    assert out["summaries"]["summarised"] == 1
    assert db.query(Document).count() == 1 and db.query(DocumentSummary).count() == 1
    again = run_cycle(factory, embedder, FakeChat("An overview."), settings_for(share), poll_s=0)
    assert again["scan"] == {"unchanged": 1} and again["summaries"].get("summarised", 0) == 0


def test_outages_skip_the_cycle_instead_of_raising(engine, db, embedder, share):
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(engine, expire_on_commit=False)

    def embeddings_down():
        raise EmbeddingError("ollama unreachable")

    def chat_down():
        raise ChatError("ollama unreachable")

    out = run_cycle(
        factory, embedder, FakeChat(), settings_for(share),
        check_embedder=embeddings_down, check_chat=chat_down,
    )  # fmt: skip
    assert out == {"scan": "embeddings-unavailable", "summaries": "model-unavailable"}
    assert db.query(Document).count() == 0  # nothing half-indexed


def test_an_unmounted_share_is_reported_not_fatal(engine, embedder, tmp_path):
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(engine, expire_on_commit=False)
    out = run_cycle(factory, embedder, FakeChat(), settings_for(tmp_path / "missing"), poll_s=0)
    assert out["scan"] == "share-missing"


def test_an_emptied_share_does_not_wipe_the_catalog(engine, db, embedder, share):
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(engine, expire_on_commit=False)
    s = settings_for(share)
    run_cycle(factory, embedder, FakeChat("x"), s, poll_s=0)
    for f in share.rglob("*.docx"):
        f.unlink()  # e.g. the NAS mount vanished and left an empty directory
    out = run_cycle(factory, embedder, FakeChat("x"), s, poll_s=0)
    assert out["scan"] == "refused"
    assert db.query(Document).count() == 1


def test_serve_forever_loops_until_stopped(engine, embedder, share):
    from sqlalchemy.orm import sessionmaker

    factory = sessionmaker(engine, expire_on_commit=False)
    stop = threading.Event()
    s = Settings(_env_file=None, docs_dir=share, scan_interval_s=0)
    cycles = []
    real_wait = stop.wait

    def wait(timeout=None):
        cycles.append(1)
        if len(cycles) >= 2:
            stop.set()
        return real_wait(0)

    stop.wait = wait  # type: ignore[method-assign]
    serve_forever(stop, factory, embedder, FakeChat("x"), s)
    assert len(cycles) == 2
