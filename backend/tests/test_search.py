import pytest
from sqlalchemy import text

from rug.config import Settings
from rug.search import PgSearch, keyword_tsquery, rrf

ALL = {"sales", "delivery", "legal"}
SOW_V1 = "sales/Boost Connect SR-1098 SOW v1.docx"
SOW_V2 = "sales/Boost Connect SR-1098 SOW v2 FINAL.docx"
MSA = "legal/Boost Connect MSA 2025.docx"


def hybrid(corpus, query, **kw):
    search = kw.pop("search", None) or PgSearch(corpus.db)
    return search.hybrid(
        query_text=query,
        query_vec=corpus.embedder.embed_query(query),
        **{"folders": ALL, **kw},
    )


def test_tsquery_or_terms_and_id_variants(db):
    q = keyword_tsquery(db, "what does the scope of work mainly cover", ids=["SR1098"])
    assert q is not None
    assert " | " in q and "'scope'" in q
    assert "'sr' <-> '-1098'" in q and "'sr' <-> '1098'" in q and "'sr1098'" in q
    # every term is OR-ed, never AND-ed, so a long question can still match
    assert " & " not in q


@pytest.mark.parametrize(
    "hostile",
    ["-1098", '"unbalanced', "it's a trap'", "a:b & c | d ! (e)", "\\", "'; DROP TABLE chunks; --"],
)
def test_hostile_text_never_breaks_the_tsquery(db, hostile):
    q = keyword_tsquery(db, hostile)
    if q is not None:
        db.execute(text("SELECT CAST(:q AS tsquery)"), {"q": q})  # must parse


def test_stopword_only_question_has_no_keyword_query(db):
    assert keyword_tsquery(db, "what is the of and to") is None


def test_rrf_prefers_items_ranked_by_both_lists():
    fused = rrf([[1, 2, 3], [3, 4, 1]], k=60)
    assert [i for i, _ in fused][:2] == [1, 3]


def test_single_document_search_stays_in_that_document(corpus):
    hits = hybrid(corpus, "scope of work", doc_ids=[corpus.id(SOW_V2)])
    assert hits and {h.document_id for h in hits} == {corpus.id(SOW_V2)}


def test_corpus_search_finds_id_and_only_newest_version(corpus):
    hits = hybrid(corpus, "network assessment of three sites", ids=["SR1098"])
    docs = {h.document_id for h in hits}
    assert corpus.id(SOW_V1) not in docs  # superseded by v2 FINAL
    assert corpus.id(SOW_V2) in docs


def test_scope_is_enforced_before_the_limit(corpus):
    query = "liability is capped at AUD 2,000,000"
    assert corpus.id(MSA) in {h.document_id for h in hybrid(corpus, query)}
    hits = hybrid(corpus, query, folders={"sales", "delivery"}, limit=50)
    assert hits and corpus.id(MSA) not in {h.document_id for h in hits}
    assert hybrid(corpus, query, folders=set()) == []


def test_explicit_doc_ids_cannot_reach_outside_scope(corpus):
    hits = hybrid(corpus, "liability", doc_ids=[corpus.id(MSA)], folders={"sales"})
    assert hits == []


def test_a_bare_string_is_not_a_folder_list(corpus):
    with pytest.raises(TypeError):
        hybrid(corpus, "scope", folders="sales")


def test_vector_modes_use_index_or_exact_scan_as_intended(corpus):
    search = PgSearch(corpus.db)
    # Tiny tables never pick HNSW by cost; make an index-ordered plan the only cheap option.
    corpus.db.execute(text("SET LOCAL enable_seqscan = off"))
    corpus.db.execute(text("SET LOCAL enable_sort = off"))
    scope = "SELECT d.id FROM documents d WHERE d.folder = ANY(CAST(:folders AS text[]))"
    params = {"folders": ["sales", "delivery"], "qv": "[" + ",".join(["0.1"] * 768) + "]", "n": 5}

    def plan(exact):
        rows = corpus.db.execute(
            text("EXPLAIN " + search.vector_sql(scope, exact=exact)), params
        ).scalars()
        return "\n".join(rows)

    assert "ix_chunks_embedding" in plan(exact=False)
    assert "ix_chunks_embedding" not in plan(exact=True)


def test_index_scope_under_delivery_falls_back_to_exact(corpus):
    """A near-useless index (ef_search=1) under a folder filter must not shrink results."""
    exact = PgSearch(corpus.db, Settings(exact_scan_max_chunks=10_000))
    tiny_index = PgSearch(corpus.db, Settings(exact_scan_max_chunks=0, hnsw_ef_search=1))
    want = hybrid(corpus, "Wi-Fi rollout sites", folders={"sales"}, search=exact, limit=8)
    assert exact.last_vector_mode == "exact"

    # Make an index-ordered plan the only cheap option (tiny tables never pick HNSW by cost),
    # then check the index really was used, came up short, and the fallback repaired it.
    corpus.db.execute(text("SET LOCAL enable_seqscan = off"))
    corpus.db.execute(text("SET LOCAL enable_sort = off"))
    got = hybrid(corpus, "Wi-Fi rollout sites", folders={"sales"}, search=tiny_index, limit=8)
    assert tiny_index.last_vector_mode == "index+exact-fallback"
    assert [h.chunk_id for h in got] == [h.chunk_id for h in want]


def test_overview_chunks_come_from_key_sections_in_order(corpus):
    hits = PgSearch(corpus.db).overview_chunks(folders=ALL, doc_ids=[corpus.id(SOW_V2)])
    assert hits
    assert all(
        "scope" in h.heading_path.lower() or "overview" in h.heading_path.lower() for h in hits
    )
    assert [h.ord for h in hits] == sorted(h.ord for h in hits)
