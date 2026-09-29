"""Document-ID extraction shared by the resolver and keyword search.

An ID is letters + optional separator + digits ("SR-1098", "sr1098", "SR 1098", "CR-01"),
normalised to upper-case letters immediately followed by the digits ("SR1098").
"""

import re
from dataclasses import dataclass

from rug.config import get_settings

# Words that look like "letters + number" but are not document IDs.
_NOT_IDS = frozenset(
    "V VER VERSION REV REVISION COPY DRAFT FINAL NO NR PAGE PAGES SECTION CLAUSE ITEM TOP "
    "FIG FIGURE TABLE APPENDIX STEP PART CHAPTER ARTICLE".split()
)


@dataclass(frozen=True)
class IdMatch:
    norm: str  # "SR1098"
    prefix: str  # "SR"
    digits: str  # "1098"
    start: int
    end: int


def find_ids(text: str, pattern: str | None = None) -> list[IdMatch]:
    rx = re.compile(pattern or get_settings().id_pattern)
    out: list[IdMatch] = []
    for m in rx.finditer(text):
        prefix, sep, digits = m.group(1), m.group(2), m.group(3)
        if prefix.upper() in _NOT_IDS:
            continue
        if sep == " " and len(digits) == 4 and 1900 <= int(digits) <= 2099:
            continue  # "MSA 2025" is a year, not an ID
        out.append(IdMatch(prefix.upper() + digits, prefix.upper(), digits, m.start(), m.end()))
    return out


def id_set(text: str) -> frozenset[str]:
    return frozenset(m.norm for m in find_ids(text))


def strip_ids(text: str, matches: list[IdMatch] | None = None) -> str:
    """`text` with ID spans replaced by spaces (so neighbouring words stay separate)."""
    chars = list(text)
    for m in matches if matches is not None else find_ids(text):
        chars[m.start : m.end] = " " * (m.end - m.start)
    return "".join(chars)
