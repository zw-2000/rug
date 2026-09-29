"""Scan the docs folder and bring the catalog + chunk index in line with it.

Per file:
  size+mtime unchanged                       -> unchanged (no hashing)
  known path, same hash                      -> touch stat only
  known path, new hash                       -> re-index
  new path, hash of a doc whose path vanished -> move/rename: update path/folder only
  new path, hash of a doc that still exists   -> copy: clone chunk rows, no re-embedding
  new path, unknown hash                      -> index
  known path gone from disk                   -> delete (chunks cascade)
Files that fail to load are recorded with status="error" and retried only when they change.
"""

import hashlib
import logging
import uuid
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.orm import Session

from rug import loaders
from rug.chunking import chunk_sections
from rug.config import Settings, get_settings
from rug.db.models import Chunk, Document, IndexRun
from rug.llm import Embedder
from rug.versions import version_key

log = logging.getLogger(__name__)

_LOCK_KEY = 0x72756701  # "rug" + 1: one indexer at a time across processes


class IndexerBusy(RuntimeError):
    pass


@dataclass(frozen=True)
class DiskFile:
    rel: str  # posix path relative to docs_dir
    size: int
    mtime: float

    @property
    def folder(self) -> str:
        parts = PurePosixPath(self.rel).parts
        return parts[0] if len(parts) > 1 else ""

    @property
    def filename(self) -> str:
        return PurePosixPath(self.rel).name


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def scan_disk(root: Path) -> dict[str, DiskFile]:
    exts = loaders.supported_extensions()
    found: dict[str, DiskFile] = {}
    for p in root.rglob("*"):
        rel_parts = p.relative_to(root).parts
        if any(part.startswith(".") for part in rel_parts) or p.name.startswith("~$"):
            continue  # hidden files/dirs and Office lock files
        if p.suffix.lower() not in exts or not p.is_file():
            continue
        st = p.stat()
        rel = PurePosixPath(*rel_parts).as_posix()
        found[rel] = DiskFile(rel, st.st_size, st.st_mtime)
    return found


class Indexer:
    def __init__(
        self,
        session: Session,
        embedder: Embedder,
        docs_dir: Path | None = None,
        settings: Settings | None = None,
    ):
        self.db = session
        self.embedder = embedder
        self.s = settings or get_settings()
        self.root = Path(docs_dir or self.s.docs_dir)

    # -- public ---------------------------------------------------------------------------

    def run(self) -> IndexRun:
        if not self.root.is_dir():
            raise FileNotFoundError(f"docs dir not found: {self.root}")
        lock_conn = self.db.get_bind().engine.connect()
        try:
            if not lock_conn.execute(select(func.pg_try_advisory_lock(_LOCK_KEY))).scalar():
                raise IndexerBusy("another indexer run is in progress")
            return self._run()
        finally:
            lock_conn.execute(select(func.pg_advisory_unlock(_LOCK_KEY)))
            lock_conn.close()

    # -- internals ------------------------------------------------------------------------

    def _run(self) -> IndexRun:
        disk = scan_disk(self.root)
        known = {d.path: d for d in self.db.scalars(select(Document))}
        run = IndexRun(total=len(disk), processed=0, counts={}, errors=[])
        self.db.add(run)
        self.db.commit()

        counts: Counter[str] = Counter()
        errors: list[dict[str, str]] = []
        gone = {p: d for p, d in known.items() if p not in disk}
        gone_by_hash = {d.sha256: d for d in gone.values()}

        # Known paths first so a copy's source is settled before new paths are matched.
        ordered = sorted(disk.values(), key=lambda f: (f.rel not in known, f.rel))
        for i, f in enumerate(ordered, 1):
            try:
                outcome = self._sync_file(f, known, gone, gone_by_hash)
            except Exception as e:  # never let one file stop the run
                self.db.rollback()
                log.exception("indexing %s failed", f.rel)
                errors.append({"path": f.rel, "error": str(e)})
                outcome = "failed"
            counts[outcome] += 1
            if outcome != "unchanged" or i % 200 == 0:
                run.processed = i
                self.db.commit()

        for doc in gone.values():
            self.db.delete(doc)
            counts["deleted"] += 1

        run.processed = len(ordered)
        run.counts = dict(counts)
        run.errors = errors
        run.finished_at = datetime.now(UTC)
        self.db.commit()
        log.info("index run %s: %s", run.id, dict(counts))
        return run

    def _sync_file(
        self,
        f: DiskFile,
        known: dict[str, Document],
        gone: dict[str, Document],
        gone_by_hash: dict[str, Document],
    ) -> str:
        doc = known.get(f.rel)
        if doc and doc.size == f.size and doc.mtime == f.mtime:
            return "unchanged"

        digest = sha256_file(self.root / f.rel)
        if doc:
            if doc.sha256 == digest:
                doc.size, doc.mtime = f.size, f.mtime
                return "unchanged"
            self._index_into(doc, f, digest)
            return "updated" if doc.status == "ok" else "failed"

        moved = gone_by_hash.pop(digest, None)
        if moved is not None:
            del gone[moved.path]
            self._retag(moved, f)
            known[f.rel] = moved
            return "moved"

        source = self.db.scalars(select(Document).where(Document.sha256 == digest).limit(1)).first()
        doc = Document(id=uuid.uuid4(), path=f.rel)
        self.db.add(doc)
        if source is not None:
            self._copy_from(doc, source, f)
            known[f.rel] = doc
            return "copied"
        self._index_into(doc, f, digest)
        known[f.rel] = doc
        return "added" if doc.status == "ok" else "failed"

    def _set_location(self, doc: Document, f: DiskFile) -> None:
        doc.path = f.rel
        doc.folder = f.folder
        doc.filename = f.filename
        doc.ext = PurePosixPath(f.rel).suffix.lower()
        doc.version_key = version_key(f.filename)
        doc.size, doc.mtime = f.size, f.mtime

    def _retag(self, doc: Document, f: DiskFile) -> None:
        self._set_location(doc, f)
        if doc.title_source == "filename":
            doc.title = PurePosixPath(f.filename).stem

    def _copy_from(self, doc: Document, src: Document, f: DiskFile) -> None:
        self._set_location(doc, f)
        doc.sha256 = src.sha256
        doc.title_source = src.title_source
        doc.title = PurePosixPath(f.filename).stem if src.title_source == "filename" else src.title
        doc.status, doc.error = src.status, src.error
        self.db.flush()
        self.db.execute(
            text(
                "INSERT INTO chunks (document_id, ord, heading_path, kind, text, embedding) "
                "SELECT :new, ord, heading_path, kind, text, embedding "
                "FROM chunks WHERE document_id = :src ORDER BY ord"
            ),
            {"new": doc.id, "src": src.id},
        )

    def _index_into(self, doc: Document, f: DiskFile, digest: str) -> None:
        self._set_location(doc, f)
        doc.sha256 = digest
        doc.indexed_at = datetime.now(UTC)
        if inspect(doc).persistent:
            self.db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        try:
            loaded = loaders.load(self.root / f.rel)
        except Exception as e:
            doc.title = PurePosixPath(f.filename).stem
            doc.title_source = "filename"
            doc.status, doc.error = "error", f"load failed: {e}"
            return
        doc.title = loaded.title or PurePosixPath(f.filename).stem
        doc.title_source = loaded.title_source
        chunks = chunk_sections(loaded.sections, self.s.chunk_chars, self.s.chunk_overlap)
        # Title + heading in the embedded text (not the stored text) helps queries that
        # name the document or section.
        vectors = (
            self.embedder.embed_documents(
                [f"{doc.title}\n{c.heading_path}\n{c.text}" for c in chunks]
            )
            if chunks
            else []
        )
        doc.status = "ok"
        doc.error = "; ".join(loaded.warnings) or None
        self.db.flush()
        self.db.add_all(
            Chunk(
                document_id=doc.id,
                ord=n,
                heading_path=c.heading_path,
                kind=c.kind,
                text=c.text,
                embedding=v,
            )
            for n, (c, v) in enumerate(zip(chunks, vectors, strict=True))
        )
