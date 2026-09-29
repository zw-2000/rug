"""Hybrid retrieval over the chunk index, entirely in Postgres.

Two candidate lists (keyword and vector) are computed inside the caller's scope and fused
with reciprocal-rank fusion. Scope is enforced in SQL on every query, before any LIMIT.

Vector scoring is explicit about exactness. pgvector's HNSW index is approximate and applies
`WHERE` filters *after* the index scan, so a selective filter can return far fewer rows than
asked for (measured on pgvector 0.6: a filter matching 5% of chunks returned 3 of 8 rows
with the default ef_search, and 3 of 50 with LIMIT 50). So:
  - small scopes (<= exact_scan_max_chunks chunks) are scored exactly: the ORDER BY carries
    a `+ 0` that makes it unusable by the index;
  - large scopes use the index with a high hnsw.ef_search and over-fetching, and if that
    still under-delivers the query is repeated exactly.
"""

import re
import uuid
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.orm import Session

from rug.catalog import CURRENT_DOCS_SQL
from rug.config import Settings, get_settings
from rug.scope import Folders, folder_list

_DOC_SET_SQL = """
    SELECT d.id FROM documents d
    WHERE d.id = ANY(CAST(:doc_ids AS uuid[])) AND d.status = 'ok'
      AND d.folder = ANY(CAST(:folders AS text[]))
"""
_LEXEMES_SQL = text("SELECT tsvector_to_array(to_tsvector('english', :t))")
_KEY_SECTION_RX = "(scope|overview|summary|objective|introduction|background|purpose)"


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    document_id: uuid.UUID
    ord: int
    heading_path: str
    kind: str
    text: str
    score: float


def _quote(lexeme: str) -> str:
    return "'" + lexeme.replace("\\", "\\\\").replace("'", "''") + "'"


def vector_literal(vec: Sequence[float]) -> str:
    return "[" + ",".join(f"{x:.7g}" for x in vec) + "]"


def keyword_tsquery(
    db: Session, question: str, ids: Sequence[str] = (), max_terms: int = 32
) -> str | None:
    """OR-combined tsquery built from the question's English lexemes plus ID phrases.

    Raw user text never reaches a tsquery parser: websearch syntax would treat a leading
    '-' as NOT and AND every term, which is far too strict for a full question. Returns
    None when nothing searchable remains (e.g. only stop-words).
    """
    lexemes = db.scalar(_LEXEMES_SQL, {"t": question}) or []
    # Longer lexemes are more specific; keep those if a very long question needs trimming.
    keep = sorted(lexemes, key=lambda lex: (-len(lex), lex))[:max_terms]
    terms = [_quote(lex) for lex in keep]
    for norm in ids:
        m = re.fullmatch(r"([A-Z]+)(\d+)", norm)
        if not m:
            continue
        letters, digits = m.group(1).lower(), m.group(2)
        # The default parser reads "SR-1098" as 'sr' then '-1098'; also cover "SR 1098", "SR1098".
        terms.append(f"({_quote(letters)} <-> {_quote('-' + digits)})")
        terms.append(f"({_quote(letters)} <-> {_quote(digits)})")
        terms.append(_quote(letters + digits))
    return " | ".join(terms) or None


def rrf(rankings: Sequence[Sequence[int]], k: int) -> list[tuple[int, float]]:
    scores: dict[int, float] = defaultdict(float)
    for ranking in rankings:
        for position, item in enumerate(ranking, 1):
            scores[item] += 1.0 / (k + position)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


class PgSearch:
    def __init__(self, db: Session, settings: Settings | None = None):
        self.db = db
        self.s = settings or get_settings()
        self.last_vector_mode = ""  # "exact" | "index" | "index+exact-fallback" (for tests/logs)

    # -- scopes ---------------------------------------------------------------------------

    def _scope(
        self, folders: Folders, doc_ids: Sequence[uuid.UUID] | None
    ) -> tuple[str, dict[str, object]]:
        params: dict[str, object] = {"folders": folder_list(folders)}
        if doc_ids is None:
            return CURRENT_DOCS_SQL, params
        params["doc_ids"] = [str(d) for d in doc_ids]
        return _DOC_SET_SQL, params

    # -- candidate lists ------------------------------------------------------------------

    def _keyword_ids(self, scope: str, params: dict[str, object], tsq: str, n: int) -> list[int]:
        rows = self.db.execute(
            text(
                f"""
                WITH scope AS ({scope})
                SELECT c.id FROM chunks c JOIN scope s ON s.id = c.document_id
                WHERE c.tsv @@ CAST(:tsq AS tsquery)
                ORDER BY ts_rank_cd(c.tsv, CAST(:tsq AS tsquery), 1) DESC, c.id
                LIMIT :n
                """
            ),
            {**params, "tsq": tsq, "n": n},
        )
        return [r[0] for r in rows]

    def vector_sql(self, scope: str, *, exact: bool) -> str:
        order = "c.embedding <=> CAST(:qv AS vector)"
        if exact:
            order = f"({order}) + 0"  # not the bare index operator, so HNSW cannot be used
        return f"""
            WITH scope AS ({scope})
            SELECT c.id FROM chunks c JOIN scope s ON s.id = c.document_id
            ORDER BY {order}
            LIMIT :n
        """

    def _vector_ids(
        self, scope: str, params: dict[str, object], qvec: Sequence[float], n: int
    ) -> list[int]:
        bind = {**params, "qv": vector_literal(qvec), "n": n}
        in_scope = self.db.scalar(
            text(
                f"WITH scope AS ({scope}) "
                "SELECT count(*) FROM chunks c JOIN scope s ON s.id = c.document_id"
            ),
            params,
        )
        in_scope = int(in_scope or 0)
        if in_scope <= self.s.exact_scan_max_chunks:
            self.last_vector_mode = "exact"
            return [r[0] for r in self.db.execute(text(self.vector_sql(scope, exact=True)), bind)]
        self.db.execute(
            text("SELECT set_config('hnsw.ef_search', :ef, true)"),
            {"ef": str(self.s.hnsw_ef_search)},
        )
        ids = [r[0] for r in self.db.execute(text(self.vector_sql(scope, exact=False)), bind)]
        if len(ids) >= min(n, in_scope):
            self.last_vector_mode = "index"
            return ids
        self.last_vector_mode = "index+exact-fallback"
        return [r[0] for r in self.db.execute(text(self.vector_sql(scope, exact=True)), bind)]

    # -- public ---------------------------------------------------------------------------

    def hybrid(
        self,
        *,
        folders: Folders,
        query_text: str,
        query_vec: Sequence[float],
        ids: Sequence[str] = (),
        doc_ids: Sequence[uuid.UUID] | None = None,
        limit: int | None = None,
    ) -> list[Hit]:
        """Top chunks by keyword+vector fusion. `doc_ids=None` searches the newest version of
        every document in scope; otherwise only the given documents (still inside scope)."""
        if doc_ids is not None and not doc_ids:
            return []
        limit = limit or self.s.retrieval_top_k
        n = self.s.retrieval_candidates
        scope, params = self._scope(folders, doc_ids)

        rankings: list[list[int]] = [self._vector_ids(scope, params, query_vec, n)]
        tsq = keyword_tsquery(self.db, query_text, ids)
        if tsq:
            rankings.append(self._keyword_ids(scope, params, tsq, n))
        fused = rrf(rankings, self.s.rrf_k)[:limit]
        return self._fetch(fused, folders)

    def overview_chunks(
        self, *, folders: Folders, doc_ids: Sequence[uuid.UUID], limit: int = 4
    ) -> list[Hit]:
        """Chunks under scope/overview/summary-style headings, in document order."""
        if not doc_ids:
            return []
        scope, params = self._scope(folders, doc_ids)
        rows = self.db.execute(
            text(
                f"""
                WITH scope AS ({scope})
                SELECT c.id FROM chunks c JOIN scope s ON s.id = c.document_id
                WHERE c.heading_path ~* :rx AND c.kind = 'text'
                ORDER BY c.document_id, c.ord
                LIMIT :n
                """
            ),
            {**params, "rx": _KEY_SECTION_RX, "n": limit},
        )
        return self._fetch([(r[0], 0.0) for r in rows], folders)

    def summary_ranking(
        self, *, folders: Folders, query_vec: Sequence[float], limit: int = 50
    ) -> list[uuid.UUID]:
        """Documents (newest version, in scope) ranked by summary-vector similarity. Exact
        scan: there is one summary per distinct document, so no index is needed."""
        rows = self.db.execute(
            text(
                f"""
                WITH scope AS ({CURRENT_DOCS_SQL})
                SELECT d.id FROM documents d
                JOIN scope s ON s.id = d.id
                JOIN document_summaries m ON m.sha256 = d.sha256
                ORDER BY m.embedding <=> CAST(:qv AS vector), d.id
                LIMIT :n
                """
            ),
            {"folders": folder_list(folders), "qv": vector_literal(query_vec), "n": limit},
        )
        return [r[0] for r in rows]

    def summaries(self, *, folders: Folders, doc_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, str]:
        if not doc_ids:
            return {}
        rows = self.db.execute(
            text(
                """
                SELECT d.id, m.summary FROM documents d
                JOIN document_summaries m ON m.sha256 = d.sha256
                WHERE d.id = ANY(CAST(:doc_ids AS uuid[])) AND d.status = 'ok'
                  AND d.folder = ANY(CAST(:folders AS text[]))
                """
            ),
            {"doc_ids": [str(d) for d in doc_ids], "folders": folder_list(folders)},
        )
        return {r[0]: r[1] for r in rows}

    # -- helpers --------------------------------------------------------------------------

    def _fetch(self, fused: Sequence[tuple[int, float]], folders: Folders) -> list[Hit]:
        """Load chunk rows in fused order. Re-applies the folder filter as defence in depth."""
        if not fused:
            return []
        rows = self.db.execute(
            text(
                """
                SELECT c.id, c.document_id, c.ord, c.heading_path, c.kind, c.text
                FROM chunks c JOIN documents d ON d.id = c.document_id
                WHERE c.id = ANY(:ids) AND d.folder = ANY(CAST(:folders AS text[]))
                """
            ),
            {"ids": [cid for cid, _ in fused], "folders": folder_list(folders)},
        )
        by_id = {r.id: r for r in rows}
        return [
            Hit(cid, r.document_id, r.ord, r.heading_path, r.kind, r.text, score)
            for cid, score in fused
            if (r := by_id.get(cid)) is not None
        ]
