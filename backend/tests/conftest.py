import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from rug.cli import alembic_upgrade

TEST_DB_URL = os.environ.get(
    "RUG_TEST_DATABASE_URL", "postgresql+psycopg://rug:rug@localhost:5432/rug_test"
)


from eval.fakes import HashEmbedder as FakeEmbedder  # noqa: E402


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB_URL)
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:  # fail loudly: these tests are the M1 gate
        pytest.exit(f"Postgres test database unreachable at {TEST_DB_URL}: {e}", returncode=2)
    with eng.begin() as c:
        c.execute(
            text(
                "DROP TABLE IF EXISTS chunks, documents, document_summaries, index_runs, "
                "users, sessions, group_folders, user_overrides, audit_log, alembic_version"
            )
        )
    alembic_upgrade(TEST_DB_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine) -> Iterator[Session]:
    with engine.begin() as c:
        c.execute(
            text(
                "TRUNCATE chunks, documents, document_summaries, index_runs, users, sessions, "
                "group_folders, user_overrides, audit_log RESTART IDENTITY CASCADE"
            )
        )
    session = sessionmaker(engine, expire_on_commit=False)()
    yield session
    session.close()


@pytest.fixture
def embedder() -> FakeEmbedder:
    return FakeEmbedder()


@pytest.fixture
def docs_dir(tmp_path: Path) -> Path:
    d = tmp_path / "nas"
    d.mkdir()
    return d


class Corpus:
    """The synthetic corpus, indexed with the fake embedder."""

    def __init__(self, db, embedder, docs_dir: Path):
        from sqlalchemy import select

        from rug.db.models import Document

        self.db, self.embedder, self.docs_dir = db, embedder, docs_dir
        self.ids = {d.path: d.id for d in db.scalars(select(Document))}
        self.folders = frozenset(d.folder for d in db.scalars(select(Document)))

    def id(self, path: str):
        return self.ids[path]


@pytest.fixture
def corpus(db, embedder, docs_dir) -> Corpus:
    from eval.synthetic_gen import build
    from rug.indexer import Indexer

    build(docs_dir)
    Indexer(db, embedder, docs_dir).run()
    return Corpus(db, embedder, docs_dir)


class FakeChat:
    """Scripted chat model: `reply` is a string or a function of the messages."""

    def __init__(self, reply="ok"):
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages, on_token=None):
        self.calls.append(messages)
        out = self.reply(messages) if callable(self.reply) else self.reply
        if on_token:
            on_token(out)
        return out


@pytest.fixture
def chat() -> FakeChat:
    return FakeChat()
