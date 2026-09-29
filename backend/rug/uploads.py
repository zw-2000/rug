"""Accepting an uploaded document onto the NAS.

Order matters: the body is streamed to a hidden temp file in the target folder (counting
bytes as it goes, so the size cap holds even for a client that lies about Content-Length),
validated by content, then given its final name by reserving that name atomically
(O_CREAT|O_EXCL, stepping "(1)", "(2)" ...) and renaming over the reservation. An existing
file is never overwritten, and the indexer never sees a half-written file (hidden name;
zero-byte reservations are skipped by the scanner).
"""

import os
import re
import secrets
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from rug import loaders
from rug.perms import validate_folder

_ILLEGAL = re.compile(r'[<>:"|?*\x00-\x1f\x7f]')
_RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE)
MAX_NAME_BYTES = 200
MAX_COLLISIONS = 1000


class UploadRejected(Exception):
    """`status` is the HTTP status to answer with; `reason` is safe to show the user."""

    def __init__(self, status: int, reason: str):
        super().__init__(reason)
        self.status, self.reason = status, reason


def sanitize_filename(raw: str) -> str:
    """The bare file name to store, or UploadRejected. Client-side paths ("C:\\x\\a.docx",
    "../a.docx") are reduced to their last component; anything unsafe on the NAS (SMB) or
    for the scanner is rejected rather than silently altered."""
    name = unicodedata.normalize("NFC", raw or "")
    name = name.replace("\\", "/").rsplit("/", 1)[-1].strip()
    if not name or name in {".", ".."}:
        raise UploadRejected(400, "the file has no name")
    if _ILLEGAL.search(name):
        raise UploadRejected(
            400, 'the file name contains characters that are not allowed (<>:"|?*)'
        )
    if name.startswith((".", "~$")):
        raise UploadRejected(400, "the file name may not start with '.' or '~$'")
    stem, ext = os.path.splitext(name)
    if stem != stem.rstrip(" ."):
        raise UploadRejected(400, "the file name may not end with a space or dot")
    if _RESERVED.match(stem.split(".")[0]):
        raise UploadRejected(400, "that file name is reserved")
    if ext.lower() not in loaders.supported_extensions():
        allowed = ", ".join(sorted(loaders.supported_extensions()))
        raise UploadRejected(415, f"only these file types can be uploaded: {allowed}")
    while len((stem + ext).encode()) > MAX_NAME_BYTES:
        stem = stem[:-1]
    return stem + ext


def _folder_dir(root: Path, folder: str) -> Path:
    try:
        validate_folder(folder)
    except ValueError as e:
        raise UploadRejected(400, "invalid folder") from e
    if not folder:
        raise UploadRejected(400, "choose a folder")
    d = root / folder
    if d.is_symlink() or not d.is_dir() or d.resolve().parent != root.resolve():
        raise UploadRejected(404, "that folder does not exist")  # never create top-level folders
    return d


def _reserve(directory: Path, filename: str) -> tuple[Path, int]:
    """Create an empty file under the first free name. Case-insensitive because the NAS
    (SMB) usually is."""
    taken = {n.lower() for n in os.listdir(directory)}
    stem, ext = os.path.splitext(filename)
    for n in range(MAX_COLLISIONS):
        candidate = filename if n == 0 else f"{stem} ({n}){ext}"
        if candidate.lower() in taken:
            continue
        try:
            fd = os.open(directory / candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:  # lost a race with another writer
            continue
        os.close(fd)
        return directory / candidate, n
    raise UploadRejected(409, "too many files with that name")


def store(root: Path, folder: str, raw_name: str, chunks: Iterable[bytes], max_bytes: int) -> str:
    """Write the upload and return its path relative to `root`. Raises UploadRejected;
    on any failure nothing is left behind."""
    name = sanitize_filename(raw_name)
    directory = _folder_dir(root, folder)
    ext = os.path.splitext(name)[1].lower()
    tmp = directory / f".rug-upload-{secrets.token_hex(8)}{ext}"
    final: Path | None = None
    try:
        size = 0
        with open(tmp, "xb") as out:
            for chunk in chunks:
                size += len(chunk)
                if size > max_bytes:
                    raise UploadRejected(413, f"the file is larger than {max_bytes >> 20} MB")
                out.write(chunk)
        if size == 0:
            raise UploadRejected(400, "the file is empty")
        try:
            loaders.validate(tmp)
        except ValueError as e:
            raise UploadRejected(415, f"not a valid {ext} file: {e}") from e
        final, _ = _reserve(directory, name)
        os.replace(tmp, final)
        return final.relative_to(root).as_posix()
    except BaseException:
        for leftover in (tmp, final):
            if leftover is not None:
                try:
                    leftover.unlink()
                except FileNotFoundError:
                    pass
        raise
