import pytest

from rug.chunking import chunk_sections
from rug.loaders.base import Section
from rug.versions import version_key


@pytest.mark.parametrize(
    "names",
    [
        [
            "Boost Connect SR-1098 SOW v1.docx",
            "Boost Connect SR-1098 SOW v2 FINAL.docx",
            "Boost_Connect_SR-1098_SOW_v3.1.docx",
            "Boost Connect SR-1098 SOW - FINAL (2).docx",
            "Boost Connect SR-1098 SOW 2026-03-01 signed.docx",
            "Copy of Boost Connect SR-1098 SOW.docx",
        ],
        ["MSA draft.docx", "MSA version 2.docx", "MSA rev3 clean.docx"],
    ],
)
def test_versions_share_key(names):
    assert len({version_key(n) for n in names}) == 1


def test_different_documents_keep_distinct_keys():
    keys = {
        version_key(n)
        for n in [
            "Boost Connect SR-1098 SOW.docx",
            "Boost Connect SR-1098 CR-01.docx",
            "Boost Connectivity SR-1089 SOW.docx",
        ]
    }
    assert len(keys) == 3
    assert version_key("Boost Connect SR-1098 SOW v2.docx") == "boost connect sr-1098 sow"


def test_prose_chunks_respect_limit_and_overlap():
    words = [f"w{i}" for i in range(2000)]
    paras = [" ".join(words[i : i + 50]) for i in range(0, 2000, 50)]
    sections = [Section(p, ("Scope",)) for p in paras]
    chunks = chunk_sections(sections, max_chars=1000, overlap=200)
    assert len(chunks) > 5
    assert all(len(c.text) <= 1000 for c in chunks)
    assert all(c.heading_path == "Scope" for c in chunks)
    # consecutive chunks overlap
    assert chunks[1].text.split("\n")[0] in chunks[0].text
    # no word is lost
    joined = " ".join(c.text.replace("\n", " ") for c in chunks).split()
    assert set(words) <= set(joined)


def test_single_huge_paragraph_is_split():
    chunks = chunk_sections([Section("x " * 3000)], max_chars=500, overlap=0)
    assert all(len(c.text) <= 500 for c in chunks)


def test_headings_and_kinds_do_not_mix():
    sections = [
        Section("a", ("H1",)),
        Section("b", ("H2",)),
        Section("hdr\nrow1", ("H2",), "table"),
        Section("[image text] c", ("H2",), "image_text"),
    ]
    chunks = chunk_sections(sections, 1000, 100)
    assert [(c.heading_path, c.kind) for c in chunks] == [
        ("H1", "text"),
        ("H2", "text"),
        ("H2", "table"),
        ("H2", "image_text"),
    ]


def test_table_split_repeats_header():
    rows = "\n".join(f"Col: value {i}" for i in range(200))
    chunks = chunk_sections([Section(f"Col\n{rows}", (), "table")], 400, 0)
    assert len(chunks) > 1
    assert all(c.text.startswith("Col\n") for c in chunks)
    assert all(len(c.text) <= 400 for c in chunks)


def test_numeric_ids_are_not_dates():
    assert version_key("PO 45001234.docx") != version_key("PO 45009999.docx")
    assert version_key("MSA 20240501.docx") == version_key("MSA 2024-05-01 signed.docx") == "msa"
    assert version_key("SOW 1.5.24.docx") == "sow"


def test_oversized_table_row_keeps_header_on_every_piece():
    long_row = "Col: " + " ".join(f"word{i}" for i in range(400))
    chunks = chunk_sections([Section(f"Col\nCol: short\n{long_row}", (), "table")], 300, 0)
    assert len(chunks) > 2
    assert all(c.text.startswith("Col\n") for c in chunks)
    assert all(len(c.text) <= 300 for c in chunks)
    body = " ".join(c.text.split("\n", 1)[1] for c in chunks)
    assert "Col: short" in body and "word399" in body
