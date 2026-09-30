"""Which embedding model made the stored vectors.

Vectors from different models (or different task prefixes) are not comparable, yet a query
vector from the wrong model still "works": it just returns poor results, silently. So the model
in use is recorded with the index, and everything that embeds text refuses to run when the
configured model differs, until `rug reembed` has rebuilt every vector.
"""

import logging

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from rug.config import Settings
from rug.db.models import Chunk, DocumentSummary, IndexMeta

log = logging.getLogger(__name__)
KEY = "embed_model"
UNFINISHED = " [re-embedding unfinished]"


class EmbeddingModelMismatch(RuntimeError):
    pass


def fingerprint(s: Settings) -> str:
    """Model plus the task prefixes: changing any of them changes the vectors."""
    return f"{s.embed_model}|{s.embed_doc_prefix}|{s.embed_query_prefix}"


def _name(fp: str) -> str:
    return fp.split("|", 1)[0] + (UNFINISHED if fp.endswith(UNFINISHED) else "")


def has_vectors(db: Session) -> bool:
    return bool(
        db.scalar(select(func.count()).select_from(Chunk).limit(1))
        or db.scalar(select(func.count()).select_from(DocumentSummary).limit(1))
    )


def check(db: Session, s: Settings) -> None:
    """Raise EmbeddingModelMismatch unless the configured model made the stored vectors.
    An index with vectors but no record (built before this check existed) is assumed to
    have been made by the configured model, with a warning, and the record is written."""
    want = fingerprint(s)
    row = db.get(IndexMeta, KEY)
    if row is None:
        if has_vectors(db):
            log.warning(
                "no record of which embedding model built this index; assuming %s. If that is "
                "wrong, run `rug reembed`.",
                s.embed_model,
            )
        db.add(IndexMeta(key=KEY, value=want))
        db.commit()
        return
    if row.value != want:
        raise EmbeddingModelMismatch(
            f"the index was built with embedding model {_name(row.value)!r} but RUG_EMBED_MODEL "
            f"(and its prefixes) now give {s.embed_model!r}. Either restore the old setting, or "
            "run `rug reembed` to rebuild every vector with the new one (stop the app and "
            "worker first; search is unreliable until it finishes)."
        )


def record(db: Session, value: str) -> None:
    row = db.get(IndexMeta, KEY)
    if row is None:
        db.add(IndexMeta(key=KEY, value=value))
    else:
        row.value = value
    db.commit()
