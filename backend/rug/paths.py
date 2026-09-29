"""Safe access to files under the document root (NAS share)."""

from pathlib import Path


class UnsafePath(ValueError):
    pass


def safe_doc_path(root: Path, rel: str) -> Path:
    """`root / rel`, provided it is a regular file that is not a symlink and stays inside
    `root`. Raises UnsafePath otherwise. Called again at read time, not just when indexing,
    because a path can be swapped for a link between discovery and use."""
    if not rel or rel.startswith("/") or "\x00" in rel:
        raise UnsafePath(f"refusing path: {rel!r}")
    p = root / rel
    if p.is_symlink() or not p.resolve().is_relative_to(root.resolve()):
        raise UnsafePath(f"refusing symlink or path outside the docs root: {rel}")
    return p
