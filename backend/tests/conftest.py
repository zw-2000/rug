import hashlib
import math
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from rug.cli import alembic_upgrade
from rug.db.models import EMBED_DIM

TEST_DB_URL = os.environ.get(
    "RUG_TEST_DATABASE_URL", "postgresql+psycopg://rug:rug@localhost:5432/rug_test"
)


class FakeEmbedder:
    """Deterministic bag-of-words hashing embedder; counts calls so tests can assert
    that unchanged/moved/copied files are not re-embedded."""

    def __init__(self) -> None:
        self.calls = 0
        self.texts = 0

    def _vec(self, s: str) -> list[float]:
        v = [0.0] * EMBED_DIM
        for tok in re.findall(r"\w+", s.lower()):
            h = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=4).digest(), "big")
            v[h % EMBED_DIM] += 1.0
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        self.calls += 1
        self.texts += len(texts)
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


@pytest.fixture(scope="session")
def engine():
    eng = create_engine(TEST_DB_URL)
    try:
        with eng.connect() as c:
            c.execute(text("SELECT 1"))
    except Exception as e:  # fail loudly: these tests are the M1 gate
        pytest.exit(f"Postgres test database unreachable at {TEST_DB_URL}: {e}", returncode=2)
    with eng.begin() as c:
        c.execute(text("DROP TABLE IF EXISTS chunks, documents, index_runs, alembic_version"))
    alembic_upgrade(TEST_DB_URL)
    yield eng
    eng.dispose()


@pytest.fixture
def db(engine) -> Iterator[Session]:
    with engine.begin() as c:
        c.execute(text("TRUNCATE chunks, documents, index_runs RESTART IDENTITY CASCADE"))
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
