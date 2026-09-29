"""M2 gate (CI half): the deterministic metrics of the golden set must pass offline, and the
scorer must be able to fail. Model-dependent metrics (answer, citation, not-found on content)
are only meaningful with a real model: `rug eval --live` on the GPU server."""

import uuid

from typer.testing import CliRunner

from eval.fakes import ExtractiveChat
from eval.runner import DETERMINISTIC, load_golden, norm, run_eval, score
from rug.catalog import DocRow
from rug.cli import app
from rug.rag import NOT_FOUND, Answer, Excerpt, Source

ALL = ["sales", "delivery", "legal"]


def test_offline_gate_passes_on_the_golden_set(corpus):
    report = run_eval(corpus.db, corpus.embedder, ExtractiveChat(), live=False)
    golden = load_golden()
    assert len(report.results) == len(golden["questions"]) >= 50
    assert report.gate() == [], "\n" + report.format()
    # every deterministic metric was actually exercised
    for name in DETERMINISTIC:
        assert report.metric(name)[1] > 0, name
    assert "not judged offline" in report.format()


def test_golden_questions_are_unique_and_well_formed():
    g = load_golden()
    ids = [q["id"] for q in g["questions"]]
    assert len(ids) == len(set(ids))
    for q in g["questions"]:
        assert q["kind"] in {"named", "find", "unanswerable"}, q["id"]
        assert q.get("scope", "all") in g["scopes"], q["id"]
        if q["kind"] in {"named", "find"}:
            assert q["doc"] and q["facts"], q["id"]


def _doc(folder: str) -> DocRow:
    return DocRow(
        uuid.uuid4(), f"{folder}/x.docx", folder, "x.docx", "X", "filename", "x", 1.0, "h", 1
    )


def _answer(folder: str, text: str, status: str = "answered") -> Answer:
    doc = _doc(folder)
    ex = Excerpt(1, 1, doc.id, folder, "X", "S", text)
    src = Source(doc.id, "x.docx", "X", folder, doc.path, ["S"], [1], text, 1)
    return Answer(status, text, sources=[src], excerpts=[ex], resolved=doc)  # type: ignore[arg-type]


def test_scorer_detects_a_document_from_outside_the_scope():
    q = {"id": "t", "kind": "unanswerable", "q": "anything", "_all": ALL}
    leaked = score(q, frozenset({"sales"}), _answer("legal", "secret"), {})
    assert leaked.checks["leaks"] is False and "LEAK" in " ".join(leaked.notes)
    clean = score(q, frozenset({"sales"}), _answer("sales", "fine"), {})
    assert clean.checks["leaks"] is True


def test_scorer_detects_forbidden_text_but_ignores_an_echo_of_the_question():
    q = {
        "id": "t",
        "kind": "unanswerable",
        "q": "Is the cap AUD 2,000,000?",
        "forbidden": ["aggregate liability", "2,000,000"],
        "_all": ALL,
    }
    assert (
        score(
            q, frozenset({"sales"}), _answer("sales", "Aggregate liability is capped"), {}
        ).checks["leaks"]
        is False
    )
    # "2,000,000" is in the question itself, so repeating it is not a leak
    assert (
        score(q, frozenset({"sales"}), _answer("sales", "No, not 2,000,000."), {}).checks["leaks"]
        is True
    )


def test_scorer_fails_a_wrong_document_and_missing_facts():
    ans = _answer("sales", "The fee is AUD 1,000.")
    wrong = {
        "id": "n",
        "kind": "named",
        "doc": "other.docx",
        "q": "q",
        "facts": [["1,000"]],
        "_all": ALL,
    }
    r = score(wrong, frozenset(ALL), ans, {ans.excerpts[0].document_id: "x.docx"})
    assert r.checks["document"] is False and r.checks["retrieval"] is True
    missing = {**wrong, "doc": "x.docx", "facts": [["9,999"]]}
    r = score(missing, frozenset(ALL), ans, {})
    assert r.checks["document"] is True and r.checks["retrieval"] is False
    absent = {**wrong, "doc": "x.docx", "absent": ["1,000"]}
    assert score(absent, frozenset(ALL), ans, {}).checks["retrieval"] is False


def test_not_found_scoring_and_thousands_separators():
    q = {"id": "u", "kind": "unanswerable", "det": True, "q": "q", "_all": ALL}
    nf = score(q, frozenset(ALL), Answer("not_found", NOT_FOUND), {})
    assert nf.checks["not_found"] and nf.checks["not_found_det"]
    assert not score(q, frozenset(ALL), _answer("sales", "an answer"), {}).checks["not_found_det"]
    assert norm("AUD 184,500") == norm("aud 184500")


def test_eval_command_refuses_to_wipe_a_production_looking_database():
    result = CliRunner().invoke(
        app, ["eval", "--database-url", "postgresql+psycopg://rug:rug@localhost:5432/rug"]
    )
    assert result.exit_code == 2 and "must end in _eval or _test" in result.output


def test_new_commands_are_registered():
    out = CliRunner().invoke(app, ["--help"]).output
    for cmd in ("ask", "summarize", "eval", "ingest", "migrate"):
        assert cmd in out
