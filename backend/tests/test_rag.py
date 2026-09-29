import uuid

import pytest
from conftest import FakeChat

from rug.config import Settings
from rug.rag import NOT_FOUND, DocumentNotAvailable, Rag, clean_citations, is_overview
from rug.summaries import summarize_pending

ALL = frozenset({"sales", "delivery", "legal"})
SOW_V2 = "sales/Boost Connect SR-1098 SOW v2 FINAL.docx"
MSA = "legal/Boost Connect MSA 2025.docx"
Q = "Boost Connect SR-1098 SOW what does the scope of work mainly about?"


def echo_first_excerpt(messages):
    """Fake model: quote the start of excerpt [1] and cite it."""
    body = messages[1]["content"]
    first = body.split("[1] ", 1)[1].split("\n", 1)[1].split("\n\n", 1)[0]
    return f"{first[:120].strip()} [1]"


def rag(corpus, chat=None, **settings):
    return Rag(
        corpus.db, corpus.embedder, chat or FakeChat(echo_first_excerpt), Settings(**settings)
    )


def test_named_question_is_answered_from_the_resolved_document(corpus):
    chat = FakeChat(echo_first_excerpt)
    answer = rag(corpus, chat).ask(Q, folders=ALL)
    assert answer.status == "answered" and answer.mode == "named"
    assert answer.resolved.filename == "Boost Connect SR-1098 SOW v2 FINAL.docx"
    assert {e.document_id for e in answer.excerpts} == {corpus.id(SOW_V2)}
    assert [s.document_id for s in answer.sources] == [corpus.id(SOW_V2)]
    assert (
        answer.sources[0].filename.endswith("v2 FINAL.docx") and answer.sources[0].n_versions == 2
    )
    assert not answer.ungrounded and "[1]" in answer.text
    system, user = chat.calls[0]
    assert "untrusted" in system["content"] and "Excerpts:" in user["content"]
    assert "Question: " + Q in user["content"]


def test_ambiguous_question_asks_instead_of_answering(corpus):
    from test_resolver import _tiny

    _tiny(corpus.docs_dir / "sales" / "Orion SOW SR-5000.docx", "sales copy")
    _tiny(corpus.docs_dir / "delivery" / "Orion SOW SR-5000.docx", "delivery copy")
    from rug.indexer import Indexer

    Indexer(corpus.db, corpus.embedder, corpus.docs_dir).run()
    chat = FakeChat("should not be called")
    answer = rag(corpus, chat).ask("Orion SOW SR-5000 scope", folders=ALL)
    assert answer.status == "needs_choice" and len(answer.candidates) == 2
    assert chat.calls == [] and answer.sources == []


def test_not_found_paths_never_call_the_model_and_look_identical(corpus):
    chat = FakeChat("should not be called")
    r = rag(corpus, chat)
    unknown = r.ask("Acme SR-9999 SOW scope", folders=ALL)
    nothing_visible = r.ask(Q, folders=set())
    assert unknown.status == nothing_visible.status == "not_found"
    assert unknown.text == nothing_visible.text == NOT_FOUND
    assert chat.calls == []


def test_invalid_citations_are_dropped_and_ungrounded_answers_are_flagged(corpus):
    answer = rag(corpus, FakeChat("The scope is Wi-Fi 7 [1] and more [9]. Also [3, 42].")).ask(
        Q, folders=ALL
    )
    assert "[9]" not in answer.text and "[42]" not in answer.text
    assert answer.dropped_citations == 2 and 1 in answer.sources[0].refs
    n = len(answer.excerpts)
    text, cited, dropped = clean_citations("a [1] b [2, 99] c [50]", 2)
    assert (text, cited, dropped) == ("a [1] b [2] c", [1, 2], 2) and n >= 1

    bare = rag(corpus, FakeChat("Wi-Fi 7 rollout across five sites.")).ask(Q, folders=ALL)
    assert bare.status == "answered" and bare.ungrounded and bare.note
    assert bare.sources  # the user still gets the documents the model was shown


def test_model_saying_not_found_yields_a_clean_not_found(corpus):
    answer = rag(corpus, FakeChat("I couldn’t find that in the documents you have access to.")).ask(
        Q, folders=ALL
    )
    assert answer.status == "not_found" and answer.text == NOT_FOUND and answer.sources == []


def test_the_model_never_sees_a_forbidden_document(corpus):
    chat = FakeChat(echo_first_excerpt)
    allowed = {"sales", "delivery"}
    for q in [
        "Boost Connect MSA 2025 what is the liability cap?",
        "which agreement caps liability at AUD 2,000,000?",
        "Contoso Mutual NDA confidentiality term",
    ]:
        answer = rag(corpus, chat).ask(q, folders=allowed)
        assert all(e.folder in allowed for e in answer.excerpts), q
        assert all(s.folder in allowed for s in answer.sources), q
        assert all(c.doc.folder in allowed for c in answer.candidates), q
    # the question may repeat the figure; what matters is the text the model was shown
    seen = "\n".join(m[1]["content"].split("Excerpts:", 1)[1] for m in chat.calls)
    assert "2,000,000" not in seen and "three (3) years" not in seen
    # the same question with access finds it
    full = rag(corpus, FakeChat(echo_first_excerpt)).ask(
        "which agreement caps liability at AUD 2,000,000?", folders=ALL
    )
    assert corpus.id(MSA) in {e.document_id for e in full.excerpts}


def test_pinning_a_document_outside_scope_is_indistinguishable_from_a_missing_one(corpus):
    r = rag(corpus)
    with pytest.raises(DocumentNotAvailable):
        r.ask("liability", folders={"sales"}, pinned=corpus.id(MSA))
    with pytest.raises(DocumentNotAvailable):
        r.ask("liability", folders=ALL, pinned=uuid.uuid4())
    ok = r.ask("what does the scope cover?", folders={"sales"}, pinned=corpus.id(SOW_V2))
    assert ok.mode == "pinned" and ok.status == "answered"


def test_folders_is_required_and_must_not_be_a_string(corpus):
    r = rag(corpus)
    with pytest.raises(TypeError):
        r.ask(Q)  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        r.ask(Q, folders="sales")


def test_overview_questions_get_the_summary_and_key_sections(corpus):
    summarize_pending(corpus.db, FakeChat("OVERVIEW-MARKER: a Wi-Fi 7 rollout."), corpus.embedder)
    chat = FakeChat(echo_first_excerpt)
    overview = rag(corpus, chat).ask(Q, folders=ALL)
    prompt = chat.calls[0][1]["content"]
    assert "OVERVIEW-MARKER" in prompt and "cite excerpts, not this overview" in prompt
    assert any(
        "scope" in e.heading_path.lower() or "overview" in e.heading_path.lower()
        for e in overview.excerpts
    )

    chat2 = FakeChat(echo_first_excerpt)
    rag(corpus, chat2).ask("Boost Connect SR-1098 SOW what is the total fee?", folders=ALL)
    assert "OVERVIEW-MARKER" not in chat2.calls[0][1]["content"]


def test_the_whole_prompt_respects_the_context_budget(corpus):
    chat = FakeChat(echo_first_excerpt)
    answer = rag(corpus, chat, context_char_budget=2000).ask(Q, folders=ALL)
    assert answer.excerpts and answer.status == "answered"
    assert sum(len(m["content"]) for m in chat.calls[0]) <= 2000  # system + question + excerpts


def test_find_the_document_ranks_the_right_one_first(corpus):
    chat = FakeChat(lambda m: "The SD-WAN migration SOW is the one you want [1].")
    answer = rag(corpus, chat).ask("Which SOW covers the SD-WAN migration?", folders=ALL)
    assert answer.mode == "find" and answer.status == "answered"
    assert "does not name one document" in chat.calls[0][1]["content"]
    assert [s.filename for s in answer.sources] == ["Boost Connectivity SR-1089 SOW.docx"]
    assert answer.excerpts[0].title.startswith("Boost Connectivity")


def test_tokens_stream_through(corpus):
    tokens: list[str] = []
    rag(corpus, FakeChat("Streamed [1]")).ask(Q, folders=ALL, on_token=tokens.append)
    assert tokens == ["Streamed [1]"]


def test_overview_detection():
    assert is_overview("what does the scope of work mainly about?")
    assert is_overview("Boost SOW", topic="")  # only a name given: describe the document
    assert is_overview("summarise the SOW")
    assert not is_overview("what is the total fee?")


def test_amounts_and_terms_stay_in_the_retrieval_text(corpus):
    """ "net 45" looks like an ID but is a search term: it must reach the search, in find mode
    and when a document is pinned."""
    chat = FakeChat(lambda m: "Payment is net 45 [1].")
    found = rag(corpus, chat).ask("Which SOW has payment terms of net 45?", folders=ALL)
    assert found.mode == "find" and found.sources[0].filename == "Atlas Retail SR-4410 SOW.docx"

    atlas = corpus.id("delivery/Atlas Retail SR-4410 SOW.docx")
    pinned = rag(corpus, chat).ask("payment terms net 45", folders=ALL, pinned=atlas)
    assert pinned.mode == "pinned" and any("net 45" in e.text for e in pinned.excerpts)


def test_long_questions_cannot_push_the_prompt_past_the_budget(corpus):
    from rug.rag import QuestionTooLong

    chat = FakeChat(echo_first_excerpt)
    settings = dict(context_char_budget=4000, max_question_chars=2000)
    long_q = Q + " " + "please consider the following background. " * 35  # ~1500 chars
    answer = rag(corpus, chat, **settings).ask(long_q, folders=ALL)
    prompt_chars = sum(len(m["content"]) for m in chat.calls[0])
    assert answer.excerpts and prompt_chars <= 4000

    with pytest.raises(QuestionTooLong):
        rag(corpus, chat, **settings).ask("x" * 2500, folders=ALL)
