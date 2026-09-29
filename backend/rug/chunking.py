"""Split loader sections into retrieval chunks.

Consecutive prose under the same heading is packed up to `max_chars` with a word-aligned
overlap. Tables split on row boundaries and repeat the header row in every piece. OCR text
is kept in its own chunks so it can be cited as image text.
"""

from dataclasses import dataclass
from itertools import groupby

from rug.loaders.base import Section

HEADING_SEP = " > "


@dataclass
class Chunk:
    heading_path: str
    kind: str
    text: str


def _split_long(text: str, max_chars: int) -> list[str]:
    """Split one oversized block on word boundaries."""
    out, cur = [], ""
    for word in text.split(" "):
        if cur and len(cur) + 1 + len(word) > max_chars:
            out.append(cur)
            cur = word
        else:
            cur = f"{cur} {word}" if cur else word
    if cur:
        out.append(cur)
    return out


def _tail(text: str, n: int) -> str:
    if n <= 0 or len(text) <= n:
        return "" if n <= 0 else text
    cut = text[-n:]
    space = cut.find(" ")
    return cut[space + 1 :] if space != -1 else cut


def _pack_prose(paragraphs: list[str], max_chars: int, overlap: int) -> list[str]:
    pieces: list[str] = []
    for p in paragraphs:
        pieces.extend(_split_long(p, max_chars) if len(p) > max_chars else [p])
    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if cur and len(cur) + 1 + len(piece) > max_chars:
            chunks.append(cur)
            carry = _tail(cur, overlap)
            fits = carry and len(carry) + 1 + len(piece) <= max_chars
            cur = f"{carry}\n{piece}" if fits else piece
        else:
            cur = f"{cur}\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


def _pack_table(table: str, max_chars: int) -> list[str]:
    header, *rows = table.split("\n")
    chunks: list[str] = []
    cur = header
    for row in rows:
        if len(cur) + 1 + len(row) > max_chars and cur != header:
            chunks.append(cur)
            cur = header
        cur = f"{cur}\n{row}"
    chunks.append(cur)
    out: list[str] = []
    for piece in chunks:
        out.extend(_split_long(piece, max_chars) if len(piece) > max_chars else [piece])
    return out


def chunk_sections(sections: list[Section], max_chars: int, overlap: int) -> list[Chunk]:
    out: list[Chunk] = []
    for (path, kind), group in groupby(sections, key=lambda s: (s.heading_path, s.kind)):
        heading = HEADING_SEP.join(path)
        items = [s.text for s in group]
        if kind == "table":
            texts = [c for t in items for c in _pack_table(t, max_chars)]
        else:
            texts = _pack_prose(items, max_chars, overlap if kind == "text" else 0)
        out.extend(Chunk(heading, kind, t) for t in texts)
    return out
