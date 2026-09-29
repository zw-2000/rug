"""Work out which document a question names.

Cascade (scored per candidate, over the newest version of each document in scope):
  1. document IDs ("SR-1098", "sr1098", "SR 1098", "CR-01") compared as normalised sets;
  2. document type words (SOW / CR / MSA / NDA, from an editable vocabulary) as a bonus
     or penalty;
  3. IDF-weighted, typo-tolerant coverage of the document's name tokens by the question.

Outcomes:
  single      one clear winner: answer from it
  ambiguous   several close candidates (or an unresolved version tie): ask the user
  none        the question does not name a document: search the whole scope instead
  unknown_id  the question names an ID in a series this scope uses (e.g. SR-) but no such
              document is visible; answering from a *different* document would be wrong

Scope is applied first: nothing outside `folders` is ever scored, so a forbidden document
can neither be offered as a candidate nor influence the outcome.
"""

import math
import re
from collections import Counter
from dataclasses import dataclass, field
from typing import Literal

from rapidfuzz import fuzz
from sqlalchemy.orm import Session

from rug.catalog import DocRow, current_documents
from rug.config import Settings, get_settings
from rug.db.models import DocType
from rug.ids import IdMatch, find_ids, id_set, strip_ids
from rug.scope import Folders

DEFAULT_DOC_TYPES: dict[str, tuple[str, ...]] = {
    "SOW": ("sow", "statement of work", "statements of work"),
    "CR": ("cr", "change request", "change requests"),
    "MSA": ("msa", "master services agreement", "master service agreement"),
    "NDA": ("nda", "non-disclosure agreement", "non disclosure agreement", "nondisclosure"),
}
_STOP = frozenset(
    "the of and for to a an in on at by with from about what whats does do is are was were "
    "this that these those please tell me can you which who when where how".split()
)
_TOKEN = re.compile(r"[a-z0-9]+")
_FUZZY_MIN = 0.85


@dataclass(frozen=True)
class Candidate:
    doc: DocRow
    score: float
    id_match: bool = False


@dataclass
class Resolution:
    kind: Literal["single", "ambiguous", "none", "unknown_id"]
    candidates: list[Candidate] = field(default_factory=list)
    topic: str = ""  # the question with the document reference removed
    unknown_ids: list[str] = field(default_factory=list)
    scored: list[Candidate] = field(default_factory=list)  # weak name signal for find-the-doc
    # IDs recognised in the question: only series this scope actually uses (see resolve()).
    ids: list[IdMatch] = field(default_factory=list)

    @property
    def doc(self) -> DocRow | None:
        return self.candidates[0].doc if self.kind == "single" else None


def detect_types(text: str, vocab: dict[str, tuple[str, ...]]) -> set[str]:
    low = text.lower()
    found = set()
    for doc_type, phrases in vocab.items():
        for phrase in phrases:
            if re.search(rf"(?<![a-z0-9]){re.escape(phrase)}(?![a-z0-9])", low):
                found.add(doc_type)
                break
    return found


def _strip_types(text: str, vocab: dict[str, tuple[str, ...]]) -> str:
    phrases = sorted((p for ps in vocab.values() for p in ps), key=len, reverse=True)
    for phrase in phrases:
        text = re.sub(rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])", " ", text, flags=re.I)
    return text


def name_tokens(text: str, vocab: dict[str, tuple[str, ...]] | None = None) -> list[str]:
    """Name words of a document or question. IDs and (given `vocab`) document-type words are
    left out: IDs have their own score and types their own bonus/penalty, so neither can make
    a name look covered."""
    text = strip_ids(text)
    if vocab:
        text = _strip_types(text, vocab)
    return [t for t in _TOKEN.findall(text.lower()) if t not in _STOP and len(t) > 1]


def _token_match(token: str, query_tokens: set[str]) -> float:
    if token in query_tokens:
        return 1.0
    if len(token) < 4:
        return 0.0
    best = 0.0
    for q in query_tokens:
        if len(q) >= 4 and abs(len(q) - len(token)) <= 3:
            best = max(best, fuzz.ratio(token, q) / 100)
    return best if best >= _FUZZY_MIN else 0.0


def _coverage(tokens: set[str], other: set[str], idf: dict[str, float]) -> float:
    """IDF-weighted share of `tokens` found (fuzzily) in `other`. Numeric tokens (years, batch
    numbers) only ever count when matched: nobody has to type "2025" to name a document."""
    total = hit = 0.0
    for t in tokens:
        m = _token_match(t, other)
        if t.isdigit() and m == 0.0:
            continue
        w = idf.get(t, 1.0)
        total += w
        hit += w * m
    return hit / total if total else 0.0


def _name_score(
    doc_tokens: set[str], q_tokens: set[str], vocab: set[str], idf: dict[str, float]
) -> float:
    """Both directions must agree: the question names most of the document's name, and the
    document's name contains what the question names. `vocab` restricts the question side to
    words that name some document in scope, so topical words do not count against a match."""
    if not doc_tokens:
        return 0.0
    named = {t for t in q_tokens if t in vocab}
    return min(_coverage(doc_tokens, q_tokens, idf), _coverage(named, doc_tokens, idf))


@dataclass
class _DocFeatures:
    doc: DocRow
    ids: frozenset[str]
    types: set[str]
    file_tokens: list[str]
    title_tokens: list[str]


def _features(doc: DocRow, vocab: dict[str, tuple[str, ...]]) -> _DocFeatures:
    ids = id_set(doc.filename) | id_set(doc.title)
    label = f"{doc.version_key} {doc.title}"
    types = detect_types(strip_ids(label), vocab)
    types |= {i_prefix for m in find_ids(label) if (i_prefix := m.prefix) in vocab}
    title_tokens = name_tokens(doc.title, vocab) if doc.title_source != "filename" else []
    return _DocFeatures(doc, ids, types, name_tokens(doc.version_key, vocab), title_tokens)


def load_doc_types(db: Session) -> dict[str, tuple[str, ...]]:
    """The administrator-edited vocabulary; the built-in defaults when there is none."""
    rows = db.query(DocType.name, DocType.phrases).all()
    return {r.name: tuple(r.phrases) for r in rows} or DEFAULT_DOC_TYPES


class Resolver:
    def __init__(
        self,
        db: Session,
        settings: Settings | None = None,
        doc_types: dict[str, tuple[str, ...]] | None = None,
    ):
        self.db = db
        self.s = settings or get_settings()
        self.vocab = doc_types or load_doc_types(db)

    def resolve(self, query: str, *, folders: Folders) -> Resolution:
        docs = current_documents(self.db, folders=folders)
        feats = [_features(d, self.vocab) for d in docs]

        # "net 45", "AUD 18,400" or "ISO 27001" have the shape of an ID but are ordinary words.
        # A token is only an ID when its letter prefix is a series some visible document uses
        # (SR-, CR-, ...); everything else stays in the text as plain words.
        known_ids = frozenset().union(*(f.ids for f in feats)) if feats else frozenset()
        known_prefixes = {re.sub(r"\d+$", "", i) for i in known_ids}
        q_matches = [m for m in find_ids(query, self.s.id_pattern) if m.prefix in known_prefixes]
        q_ids = frozenset(m.norm for m in q_matches)
        q_tokens = set(name_tokens(query, self.vocab))
        # Type words the user typed ("change request", "SOW") are stronger evidence than types
        # implied by an ID prefix ("CR-01"), so they are tracked separately.
        typed = detect_types(strip_ids(query, q_matches), self.vocab)
        q_types = typed | {m.prefix for m in q_matches if m.prefix in self.vocab}

        # Any named ID from a series this scope uses but that no visible document carries makes
        # the question unanswerable, even if other IDs in it do exist: answering from those
        # would silently drop the part the user asked about.
        unknown = q_ids - known_ids
        if unknown:
            return Resolution("unknown_id", topic=query, unknown_ids=sorted(unknown), ids=q_matches)

        df: Counter[str] = Counter()
        for f in feats:
            df.update(set(f.file_tokens) | set(f.title_tokens))
        n = max(len(feats), 1)
        idf = {t: math.log(1 + n / c) for t, c in df.items()}
        vocab_tokens = set(df)

        scored: list[Candidate] = []
        for f in feats:
            cov = max(
                _name_score(set(f.file_tokens), q_tokens, vocab_tokens, idf),
                _name_score(set(f.title_tokens), q_tokens, vocab_tokens, idf),
            )
            inter = q_ids & f.ids
            if q_ids:
                # Containment (does the document carry every ID asked for?) decides; the
                # exactness of the match (Jaccard) only orders documents that all carry it,
                # so "SR-1098" prefers the SOW over its change request, yet "CR-01" alone
                # still finds the change request despite its extra ID.
                contained = len(inter) / len(q_ids)
                exact = len(inter) / len(q_ids | f.ids)
                score = 0.4 * contained + 0.3 * exact + 0.3 * cov if inter else 0.15 * cov
            else:
                score = cov
            if q_types and f.types:
                if not q_types & f.types:
                    score -= 0.35 if typed else 0.20
                elif inter:  # a type match only adds to an ID match, never to a name match alone
                    score += 0.35 if typed else 0.10
            scored.append(Candidate(f.doc, max(0.0, min(1.0, score)), bool(inter)))
        scored.sort(key=lambda c: (-c.score, c.doc.filename))
        weak = [c for c in scored if c.score >= 0.2][:20]

        if not scored:
            return Resolution("none", topic=query, ids=q_matches)
        top = scored[0]
        needed = self.s.resolver_accept_id if top.id_match else self.s.resolver_accept_named
        if top.score < needed:
            return Resolution("none", topic=query, scored=weak, ids=q_matches)

        if top.doc.tie:  # distinct files sharing the newest mtime: the user picks, not the score
            group = [
                c
                for c in scored
                if (c.doc.folder, c.doc.version_key) == (top.doc.folder, top.doc.version_key)
            ]
            if len(group) > 1:
                return Resolution(
                    "ambiguous",
                    group[: self.s.resolver_max_candidates],
                    topic=query,
                    scored=weak,
                    ids=q_matches,
                )

        close = [c for c in scored if c.score >= top.score - self.s.resolver_margin]
        close = close[: self.s.resolver_max_candidates]
        if len(close) > 1:
            return Resolution("ambiguous", close, topic=query, scored=weak, ids=q_matches)
        return Resolution(
            "single",
            close,
            topic=self._topic(query, q_matches, top.doc),
            scored=weak,
            ids=q_matches,
        )

    def series_ids(self, query: str, *, folders: Folders) -> list[IdMatch]:
        """IDs in `query` that belong to a series used by documents in scope (for callers that
        skip resolution, e.g. a pinned document)."""
        feats = [_features(d, self.vocab) for d in current_documents(self.db, folders=folders)]
        prefixes = {re.sub(r"\d+$", "", i) for f in feats for i in f.ids}
        return [m for m in find_ids(query, self.s.id_pattern) if m.prefix in prefixes]

    def _topic(self, query: str, q_matches, doc: DocRow) -> str:  # type: ignore[no-untyped-def]
        """The question minus the words that only named the document.

        Only filename tokens, IDs and type words are dropped. Title words are kept: a title
        like "Statement of Work" or "Cold-Chain Monitoring" is often what the question is
        about, and leaving a few name words in a single-document search is harmless.
        """
        text = strip_ids(query, q_matches)
        drop = set(name_tokens(doc.version_key, self.vocab))
        for phrases in self.vocab.values():
            drop.update(p for p in phrases if " " not in p)
        for phrase in sorted(drop, key=len, reverse=True):
            text = re.sub(
                rf"(?<![A-Za-z0-9]){re.escape(phrase)}(?![A-Za-z0-9])", " ", text, flags=re.I
            )
        text = re.sub(r"\s+", " ", text).strip(" \t—–-:,;.")
        return text
