"""Scan the docs folder and bring the catalog + chunk index in line with it.

Per file:
  size+mtime unchanged                        -> unchanged (no hashing)
  known path, same hash                       -> touch stat only
  known path, new hash                        -> re-index
  new path, hash of a doc whose path vanished -> move/rename: update path/folder; chunks are
                                                 re-embedded only if the title changed
  new path, hash of a doc that still exists   -> copy: clone chunk rows (re-embed only if the
                                                 filename-derived title differs)
  new path, unknown hash                      -> index
  known path gone from disk                   -> delete (chunks cascade)
Files that fail to load are recorded with status="error" and retried only when they change.
Environment failures (Ollama down, Tesseract missing) write nothing and are retried next run.
Symlinks are never followed, so a link cannot pull a file into another permission folder.
"""

import hashlib
import logging
import os
import stat
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

from sqlalchemy import delete, func, inspect, select, text
from sqlalchemy.orm import Session

from rug import embedmeta, loaders
from rug.chunking import chunk_sections
from rug.config import Settings, get_settings
from rug.db.models import EMBED_DIM, Chunk, Document, IndexRun
from rug.llm import Embedder
from rug.loaders.base import LoaderEnvironmentError
from rug.paths import safe_doc_path
from rug.versions import version_key

log = logging.getLogger(__name__)

_LOCK_KEY = 0x72756701  # "rug" + 1: one indexer at a time across processes


class IndexerBusy(RuntimeError):
    pass


class MassDeletionRefused(RuntimeError):
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
    """Regular files with a registered extension. Hidden entries, Office lock files and
    symlinks (to files or directories) are skipped; files vanishing mid-scan are ignored."""
    exts = loaders.supported_extensions()
    found: dict[str, DiskFile] = {}
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        base = Path(dirpath)
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and not (base / d).is_symlink()]
        for name in filenames:
            if name.startswith((".", "~$")) or Path(name).suffix.lower() not in exts:
                continue
            p = base / name
            try:
                st = p.lstat()
            except OSError:  # deleted or renamed between listing and stat
                continue
            if not stat.S_ISREG(st.st_mode):  # symlinks, sockets, ...
                continue
            if st.st_size == 0:  # a reserved upload name or an empty save: never a valid document
                continue
            rel = p.relative_to(root).as_posix()
            found[rel] = DiskFile(rel, st.st_size, st.st_mtime)
    return found


class Indexer:
    def __init__(
        self,
        session: Session,
        embedder: Embedder,
        docs_dir: Path | None = None,
        settings: Settings | None = None,
        allow_mass_delete: bool = False,
    ):
        self.db = session
        self.embedder = embedder
        self.s = settings or get_settings()
        self.root = Path(docs_dir or self.s.docs_dir)
        self.allow_mass_delete = allow_mass_delete
        if self.s.embed_dim != EMBED_DIM:
            raise ValueError(
                f"RUG_EMBED_DIM={self.s.embed_dim} but the database stores {EMBED_DIM}-dim "
                "vectors; switching embedding size needs a migration and a full re-index"
            )

    # -- public ---------------------------------------------------------------------------

    def run(self) -> IndexRun:
        if not self.root.is_dir():
            raise FileNotFoundError(f"docs dir not found: {self.root}")
        embedmeta.check(self.db, self.s)
        lock_conn = self.db.get_bind().engine.connect()
        try:
            if not lock_conn.execute(select(func.pg_try_advisory_lock(_LOCK_KEY))).scalar():
                raise IndexerBusy("another indexer run is in progress")
            return self._run()
        finally:
            lock_conn.execute(select(func.pg_advisory_unlock(_LOCK_KEY)))
            lock_conn.close()

    def reembed_all(self, on_progress: Callable[[int, int], None] | None = None) -> dict[str, int]:
        """Rebuild every chunk and summary vector with the configured embedding model, then
        record it. Until it finishes the index is marked unusable, so an interrupted run cannot
        leave a half-old, half-new index that looks healthy; run it again to finish."""
        from rug.db.models import DocumentSummary

        lock_conn = self.db.get_bind().engine.connect()
        try:
            if not lock_conn.execute(select(func.pg_try_advisory_lock(_LOCK_KEY))).scalar():
                raise IndexerBusy("another indexer run is in progress")
            embedmeta.record(self.db, embedmeta.fingerprint(self.s) + embedmeta.UNFINISHED)
            ids = list(
                self.db.scalars(
                    select(Document.id).where(Document.status == "ok").order_by(Document.id)
                )
            )
            for n, doc_id in enumerate(ids, 1):
                doc = self.db.get(Document, doc_id)
                if doc is not None:
                    self._reembed(doc)
                self.db.commit()
                if on_progress:
                    on_progress(n, len(ids))
            summaries = 0
            for row in list(self.db.scalars(select(DocumentSummary))):
                row.embedding = self.embedder.embed_documents([row.summary])[0]
                summaries += 1
            self.db.commit()
            embedmeta.record(self.db, embedmeta.fingerprint(self.s))
            return {"documents": len(ids), "summaries": summaries}
        finally:
            lock_conn.execute(select(func.pg_advisory_unlock(_LOCK_KEY)))
            lock_conn.close()

    def is_busy(self) -> bool:
        """True while another process holds the indexer lock (a scan is running)."""
        conn = self.db.get_bind().engine.connect()
        try:
            got = bool(conn.execute(select(func.pg_try_advisory_lock(_LOCK_KEY))).scalar())
            if got:
                conn.execute(select(func.pg_advisory_unlock(_LOCK_KEY)))
            return not got
        finally:
            conn.close()

    def sync_one(self, rel: str) -> str:
        """Index a single file now (used right after an upload). Takes the same lock as a full
        run, so raises IndexerBusy while one is in progress: the next scan picks the file up.
        Returns the outcome ("added", "copied", "updated", "unchanged", "failed")."""
        embedmeta.check(self.db, self.s)
        f = self._disk_file(rel)
        lock_conn = self.db.get_bind().engine.connect()
        try:
            if not lock_conn.execute(select(func.pg_try_advisory_lock(_LOCK_KEY))).scalar():
                raise IndexerBusy("another indexer run is in progress")
            try:
                known = {}
                doc = self.db.scalars(select(Document).where(Document.path == rel)).first()
                if doc is not None:
                    known[rel] = doc
                outcome = self._sync_file(f, known, {}, {})
                self.db.commit()
                return outcome
            except Exception:
                self.db.rollback()
                raise
        finally:
            lock_conn.execute(select(func.pg_advisory_unlock(_LOCK_KEY)))
            lock_conn.close()

    def _disk_file(self, rel: str) -> DiskFile:
        p = safe_doc_path(self.root, rel)
        st = p.lstat()
        return DiskFile(rel, st.st_size, st.st_mtime)

    # -- internals ------------------------------------------------------------------------

    def _run(self) -> IndexRun:
        disk = scan_disk(self.root)
        known = {d.path: d for d in self.db.scalars(select(Document))}
        gone = {p: d for p, d in known.items() if p not in disk}
        if known and not disk and not self.allow_mass_delete:
            raise MassDeletionRefused(self._mass_delete_msg(len(known), len(known), 0))

        run = IndexRun(total=len(disk), processed=0, counts={}, errors=[])
        self.db.add(run)
        self.db.commit()

        counts: Counter[str] = Counter()
        errors: list[dict[str, str]] = []
        gone_by_hash = {d.sha256: d for d in gone.values()}

        # Known paths first so a copy's source is settled before new paths are matched.
        ordered = sorted(disk.values(), key=lambda f: (f.rel not in known, f.rel))
        for i, f in enumerate(ordered, 1):
            try:
                outcome = self._sync_file(f, known, gone, gone_by_hash)
                self.db.flush()  # surface DB errors for this file inside its own handler
            except Exception as e:  # never let one file stop the run
                self.db.rollback()
                log.exception("indexing %s failed", f.rel)
                errors.append({"path": f.rel, "error": str(e)})
                outcome = "failed"
            else:
                doc = known.get(f.rel)
                if outcome == "failed" and doc is not None:
                    errors.append({"path": f.rel, "error": doc.error or "failed"})
            counts[outcome] += 1
            if outcome != "unchanged" or i % 200 == 0:
                run.processed = i
                self.db.commit()

        # Moves were matched above, so `gone` now holds only true deletions.
        refused = None
        if (
            gone
            and not self.allow_mass_delete
            and len(gone) / len(known) > self.s.max_delete_fraction
        ):
            refused = self._mass_delete_msg(len(gone), len(known), len(disk))
            errors.append({"path": "", "error": refused})
        else:
            for doc in gone.values():
                self.db.delete(doc)
                counts["deleted"] += 1

        run.processed = len(ordered)
        run.counts = dict(counts)
        run.errors = errors
        run.finished_at = datetime.now(UTC)
        self.db.commit()
        log.info("index run %s: %s", run.id, dict(counts))
        if refused:
            raise MassDeletionRefused(refused)
        return run

    def _mass_delete_msg(self, n_gone: int, n_known: int, n_disk: int) -> str:
        return (
            f"scan of {self.root} would delete {n_gone} of {n_known} indexed documents "
            f"({n_disk} files found); deletions skipped. Is the NAS share mounted? "
            "Re-run with --allow-mass-delete if this is intended."
        )

    def _path(self, f: DiskFile) -> Path:
        """Re-check at open time that the file is still a regular file inside the root."""
        return safe_doc_path(self.root, f.rel)

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

        digest = sha256_file(self._path(f))
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
        known[f.rel] = doc
        if source is not None:
            self._copy_from(doc, source, f)
            return "copied"
        self._index_into(doc, f, digest)
        return "added" if doc.status == "ok" else "failed"

    def _set_location(self, doc: Document, f: DiskFile) -> None:
        doc.path = f.rel
        doc.folder = f.folder
        doc.filename = f.filename
        doc.ext = PurePosixPath(f.rel).suffix.lower()
        doc.version_key = version_key(f.filename)
        doc.size, doc.mtime = f.size, f.mtime

    def _retag(self, doc: Document, f: DiskFile) -> None:
        old_title = doc.title
        self._set_location(doc, f)
        if doc.title_source == "filename":
            doc.title = PurePosixPath(f.filename).stem
        if doc.title != old_title:
            self._reembed(doc)

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
        if doc.title != src.title:
            self._reembed(doc)

    def _embed(self, title: str, chunks: list[tuple[str, str]]) -> list[list[float]]:
        """Embed (heading_path, text) pairs. The title and heading are part of the embedded
        text (not the stored text) to help queries that name the document or section."""
        if not chunks:
            return []
        return self.embedder.embed_documents([f"{title}\n{h}\n{t}" for h, t in chunks])

    def _reembed(self, doc: Document) -> None:
        """Refresh vectors after a title change, keeping chunk rows (and their ids)."""
        self.db.flush()
        rows = list(
            self.db.scalars(select(Chunk).where(Chunk.document_id == doc.id).order_by(Chunk.ord))
        )
        vectors = self._embed(doc.title, [(c.heading_path, c.text) for c in rows])
        for c, v in zip(rows, vectors, strict=True):
            c.embedding = v

    def _index_into(self, doc: Document, f: DiskFile, digest: str) -> None:
        self._set_location(doc, f)
        doc.sha256 = digest
        doc.indexed_at = datetime.now(UTC)
        if inspect(doc).persistent:
            self.db.execute(delete(Chunk).where(Chunk.document_id == doc.id))
        try:
            loaded = loaders.load(self._path(f))
        except LoaderEnvironmentError:
            raise  # not the file's fault: roll back and retry on the next run
        except Exception as e:
            doc.title = PurePosixPath(f.filename).stem
            doc.title_source = "filename"
            doc.status, doc.error = "error", f"load failed: {e}"
            return
        doc.title = loaded.title or PurePosixPath(f.filename).stem
        doc.title_source = loaded.title_source
        chunks = chunk_sections(loaded.sections, self.s.chunk_chars, self.s.chunk_overlap)
        vectors = self._embed(doc.title, [(c.heading_path, c.text) for c in chunks])
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
