"""Access scope: the set of top-level folders a caller may see.

Every retrieval entry point takes `folders` as a required argument with no default, so a
forgotten argument fails loudly instead of meaning "everything". An empty collection means
"nothing". The filter is applied inside each SQL query (before any LIMIT), never on results.
Only trusted callers (the CLI, admin tooling, eval) build "all folders" via `all_folders`.
"""

from collections.abc import Collection

from sqlalchemy import select
from sqlalchemy.orm import Session

from rug.db.models import Document

Folders = Collection[str]


def all_folders(db: Session) -> frozenset[str]:
    return frozenset(db.scalars(select(Document.folder).distinct()))


def folder_list(folders: Folders) -> list[str]:
    """Deterministic list for SQL array parameters."""
    if isinstance(folders, str):  # a bare string would be iterated per character
        raise TypeError("folders must be a collection of folder names, not a string")
    return sorted(set(folders))
