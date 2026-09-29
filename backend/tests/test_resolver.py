import os
from pathlib import Path

import pytest
from docx import Document as Docx

from rug.indexer import Indexer
from rug.resolver import Resolver

ALL = {"sales", "delivery", "legal"}


def resolve(corpus, query, folders=ALL):
    return Resolver(corpus.db).resolve(query, folders=folders)


def names(res):
    return [c.doc.filename for c in res.candidates]


@pytest.mark.parametrize(
    "query",
    [
        "Boost Connect SR-1098 SOW what does the scope of work mainly about?",
        "boost connect sr1098 sow",
        "the Boost Connect SR 1098 SOW",
        "SR-1098 SOW",
    ],
)
def test_named_document_resolves_to_newest_version(corpus, query):
    res = resolve(corpus, query)
    assert res.kind == "single", (query, res.candidates)
    assert names(res) == ["Boost Connect SR-1098 SOW v2 FINAL.docx"]  # v1 is superseded


def test_same_id_different_document_type(corpus):
    assert names(resolve(corpus, "SR-1098 CR-01")) == ["Boost Connect SR-1098 CR-01.docx"]
    assert names(resolve(corpus, "the change request for SR-1098")) == [
        "Boost Connect SR-1098 CR-01.docx"
    ]


def test_near_duplicate_names_do_not_cross(corpus):
    assert names(resolve(corpus, "Boost Connectivity SOW")) == [
        "Boost Connectivity SR-1089 SOW.docx"
    ]
    assert names(resolve(corpus, "Boost Connect SOW SR-1098")) == [
        "Boost Connect SR-1098 SOW v2 FINAL.docx"
    ]


def test_title_and_typos_resolve(corpus):
    assert names(resolve(corpus, "Northwind Foods cold-chain monitoring SOW")) == [
        "Northwind Foods SR-5120 SOW.docx"
    ]
    assert names(resolve(corpus, "Harbour Logistics SOW"))[:1] == [
        "Harbor Logistics SR-2210 SOW.docx"
    ]


def test_topical_question_that_shares_a_word_is_not_a_document_reference(corpus):
    for q in ["which SOW covers logistics?", "who is responsible for UAT sign-off?"]:
        assert resolve(corpus, q).kind == "none", q


def test_topic_drops_the_document_reference(corpus):
    res = resolve(corpus, "Boost Connect SR-1098 SOW — what does the scope of work mainly about?")
    assert res.topic == "what does the scope of work mainly about?"


def test_unknown_id_in_a_known_series_is_not_answered_from_another_document(corpus):
    res = resolve(corpus, "Acme SR-9999 SOW scope")
    assert res.kind == "unknown_id" and res.unknown_ids == ["SR9999"]
    # an ID series nobody uses is just a word, not a reference
    assert resolve(corpus, "what are the top 10 risks").kind == "none"


def test_forbidden_documents_are_never_candidates(corpus):
    query = "Boost Connect MSA 2025 liability"
    assert names(resolve(corpus, query)) == ["Boost Connect MSA 2025.docx"]
    res = resolve(corpus, query, folders={"sales", "delivery"})
    assert all(c.doc.folder != "legal" for c in res.candidates + res.scored)
    assert res.kind != "single" or res.doc.folder != "legal"
    assert resolve(corpus, "SR-1098", folders=set()).kind == "none"


def _tiny(path: Path, text: str, mtime: float = 1_780_000_000):
    path.parent.mkdir(parents=True, exist_ok=True)
    d = Docx()
    d.add_paragraph(text)
    d.save(str(path))
    os.utime(path, (mtime, mtime))


def test_an_id_that_only_exists_in_a_forbidden_folder_looks_like_any_unknown_id(
    db, embedder, docs_dir
):
    _tiny(docs_dir / "sales" / "Public SOW SR-1000.docx", "public")
    _tiny(docs_dir / "legal" / "Secret Deal LG-4471.docx", "secret")
    Indexer(db, embedder, docs_dir).run()
    r = Resolver(db)
    hidden = r.resolve("Secret Deal LG-4471", folders={"sales"})
    typo = r.resolve("Secret Deal LG-4472", folders={"sales"})
    assert hidden.kind == typo.kind == "none"  # the LG- series does not exist for this caller
    assert hidden.candidates == [] and hidden.unknown_ids == []
    assert r.resolve("Secret Deal LG-4471", folders={"sales", "legal"}).kind == "single"


def test_same_name_in_two_folders_is_ambiguous_until_scope_narrows(db, embedder, docs_dir):
    _tiny(docs_dir / "sales" / "Orion SOW SR-5000.docx", "sales copy")
    _tiny(docs_dir / "delivery" / "Orion SOW SR-5000.docx", "delivery copy")
    Indexer(db, embedder, docs_dir).run()
    r = Resolver(db)
    both = r.resolve("Orion SOW SR-5000", folders={"sales", "delivery"})
    assert both.kind == "ambiguous" and {c.doc.folder for c in both.candidates} == {
        "sales",
        "delivery",
    }
    assert r.resolve("Orion SOW SR-5000", folders={"sales"}).kind == "single"


def test_different_files_with_the_same_newest_mtime_need_a_choice(db, embedder, docs_dir):
    _tiny(docs_dir / "sales" / "Vega SOW SR-6000 v1.docx", "one")
    _tiny(docs_dir / "sales" / "Vega SOW SR-6000 v2.docx", "two")  # same mtime, different bytes
    Indexer(db, embedder, docs_dir).run()
    res = Resolver(db).resolve("Vega SOW SR-6000", folders={"sales"})
    assert res.kind == "ambiguous" and len(res.candidates) == 2


def test_identical_duplicates_of_the_newest_version_are_not_a_tie(db, embedder, docs_dir):
    _tiny(docs_dir / "sales" / "Vega SOW SR-6000.docx", "same")
    _tiny(docs_dir / "sales" / "Vega SOW SR-6000 (1).docx", "same")
    Indexer(db, embedder, docs_dir).run()
    res = Resolver(db).resolve("Vega SOW SR-6000", folders={"sales"})
    assert res.kind == "single" and res.doc.filename == "Vega SOW SR-6000.docx"


@pytest.mark.parametrize(
    ("query", "expected"),
    [
        # Amounts and terms look like "letters + number" but are not document IDs.
        ("Are the payment terms net 45 in the Atlas Retail SOW?", "Atlas Retail SR-4410 SOW.docx"),
        ("Does the Harbor Logistics SOW mention AUD 18,400?", "Harbor Logistics SR-2210 SOW.docx"),
        (
            "Is the fixed fee AUD 96,000 in the Boost Connectivity SOW?",
            "Boost Connectivity SR-1089 SOW.docx",
        ),
        ("Does the Pinecrest Health SOW follow ISO 27001?", "Pinecrest Health SR-3301 SOW.docx"),
    ],
)
def test_stray_letters_plus_number_in_a_question_are_not_document_ids(corpus, query, expected):
    res = resolve(corpus, query)
    assert res.kind == "single", (
        query,
        res.kind,
        [(c.doc.filename, round(c.score, 2)) for c in res.scored[:3]],
    )
    assert names(res) == [expected]
    # and they stay in the text used for retrieval
    assert res.ids == []


def test_ids_are_only_recognised_in_series_the_scope_uses(corpus):
    res = resolve(corpus, "SR-1098 net 45 GST 10")
    assert [m.norm for m in res.ids] == ["SR1098"]
    assert "net 45" in res.topic and "GST 10" in res.topic
