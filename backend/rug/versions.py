"""Group files that are versions of the same document.

`version_key` strips version markers from a filename so that e.g.
"Boost Connect SR-1098 SOW v2 FINAL (1).docx" and "Boost_Connect_SR-1098_SOW_v1.docx"
share the key "boost connect sr-1098 sow". Groups are always scoped to one folder by the
caller, so a version group never spans permission folders.
"""

import re
from pathlib import PurePosixPath

_MARKERS = [
    r"\bcopy of\b",  # before the bare "copy" alternative so "of" isn't left behind
    r"\(\s*\d+\s*\)",  # "(1)" copy suffixes
    r"\b\d{4}[-_.]?\d{2}[-_.]?\d{2}\b",  # 2024-05-01, 20240501
    r"\b\d{1,2}[-_.]\d{1,2}[-_.]\d{2,4}\b",  # 01.05.2024, 1-5-24
    r"\bv(?:er(?:sion)?)?\s*\d+(?:\.\d+)*\b",  # v2, v1.3, ver 2, version 3
    r"\brev(?:ision)?\s*\d+\b",
    r"\b(?:final|draft|copy|latest|updated|signed|clean|redline|executed|approved)\b",
]
_MARKER_RE = re.compile("|".join(_MARKERS), re.IGNORECASE)


def version_key(filename: str) -> str:
    stem = PurePosixPath(filename).stem
    s = stem.replace("_", " ")
    # Separator-only dashes/dots (" - ", trailing ".") are noise; keep them inside IDs like SR-1098.
    s = re.sub(r"\s[-–.]+\s|\s[-–.]+$|^[-–.]+\s", " ", f" {s} ")
    s = _MARKER_RE.sub(" ", s)
    s = re.sub(r"\s[-–.]+(?=\s|$)", " ", s)
    s = re.sub(r"\s+", " ", s).strip(" -–.").lower()
    return s or stem.lower()
