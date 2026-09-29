"""Read-side view of the catalog: which documents a caller can see, latest version only.

A version group is (folder, version_key). Within a group the newest healthy version (by
mtime) is "current". Identical-content duplicates of the newest version collapse to one;
different files sharing the newest mtime stay separate and are flagged `tie`, so callers
can ask the user instead of guessing.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from rug.scope import Folders, folder_list


@dataclass(frozen=True)
class DocRow:
    id: uuid.UUID
    path: str
    folder: str
    filename: str
    title: str
    title_source: str
    version_key: str
    mtime: float
    sha256: str
    n_versions: int
    tie: bool = False


# Reused by search.py so both sides agree on what "current" means.
CURRENT_DOCS_SQL = """
    SELECT d.id FROM (
        SELECT d.*, max(d.mtime) OVER w AS newest
        FROM documents d
        WHERE d.status = 'ok' AND d.folder = ANY(CAST(:folders AS text[]))
        WINDOW w AS (PARTITION BY d.folder, d.version_key)
    ) d
    WHERE d.mtime = d.newest
"""

_ROWS_SQL = text(
    """
    SELECT d.id, d.path, d.folder, d.filename, d.title, d.title_source, d.version_key,
           d.mtime, d.sha256, d.n_versions
    FROM (
        SELECT d.*, max(d.mtime) OVER w AS newest, count(*) OVER w AS n_versions
        FROM documents d
        WHERE d.status = 'ok' AND d.folder = ANY(CAST(:folders AS text[]))
        WINDOW w AS (PARTITION BY d.folder, d.version_key)
    ) d
    WHERE d.mtime = d.newest
    ORDER BY d.folder, d.version_key, d.filename
    """
)


def _row(r, tie: bool = False) -> DocRow:  # type: ignore[no-untyped-def]
    return DocRow(
        id=r.id,
        path=r.path,
        folder=r.folder,
        filename=r.filename,
        title=r.title,
        title_source=r.title_source,
        version_key=r.version_key,
        mtime=r.mtime,
        sha256=r.sha256,
        n_versions=r.n_versions,
        tie=tie,
    )


def current_documents(db: Session, *, folders: Folders) -> list[DocRow]:
    rows = db.execute(_ROWS_SQL, {"folders": folder_list(folders)}).all()
    groups: dict[tuple[str, str], list] = {}  # type: ignore[type-arg]
    for r in rows:
        groups.setdefault((r.folder, r.version_key), []).append(r)
    out: list[DocRow] = []
    for members in groups.values():
        by_hash: dict[str, list] = {}  # type: ignore[type-arg]
        for r in members:
            by_hash.setdefault(r.sha256, []).append(r)
        # Same bytes under several names: keep the shortest name, then alphabetical.
        distinct = [min(g, key=lambda r: (len(r.filename), r.filename)) for g in by_hash.values()]
        out.extend(_row(r, tie=len(distinct) > 1) for r in distinct)
    return out


def fetch_document(db: Session, doc_id: uuid.UUID, *, folders: Folders) -> DocRow | None:
    """A specific document (any version) if it is healthy and inside `folders`."""
    r = db.execute(
        text(
            """
            SELECT d.id, d.path, d.folder, d.filename, d.title, d.title_source, d.version_key,
                   d.mtime, d.sha256,
                   (SELECT count(*) FROM documents o
                     WHERE o.folder = d.folder AND o.version_key = d.version_key
                       AND o.status = 'ok') AS n_versions
            FROM documents d
            WHERE d.id = :id AND d.status = 'ok' AND d.folder = ANY(CAST(:folders AS text[]))
            """
        ),
        {"id": doc_id, "folders": folder_list(folders)},
    ).first()
    return _row(r) if r else None


def documents_by_id(
    db: Session, doc_ids: Sequence[uuid.UUID], *, folders: Folders
) -> dict[uuid.UUID, DocRow]:
    """Rows for specific documents (any version), restricted to healthy ones inside `folders`."""
    if not doc_ids:
        return {}
    rows = db.execute(
        text(
            """
            SELECT d.id, d.path, d.folder, d.filename, d.title, d.title_source, d.version_key,
                   d.mtime, d.sha256,
                   (SELECT count(*) FROM documents o
                     WHERE o.folder = d.folder AND o.version_key = d.version_key
                       AND o.status = 'ok') AS n_versions
            FROM documents d
            WHERE d.id = ANY(CAST(:ids AS uuid[])) AND d.status = 'ok'
              AND d.folder = ANY(CAST(:folders AS text[]))
            """
        ),
        {"ids": [str(i) for i in doc_ids], "folders": folder_list(folders)},
    )
    return {r.id: _row(r) for r in rows}
