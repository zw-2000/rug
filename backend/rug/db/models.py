import uuid
from datetime import datetime
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    CheckConstraint,
    Computed,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    PrimaryKeyConstraint,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBED_DIM = 768  # must match the migration; changing models means a new migration + re-index

# English stemming for prose plus a 'simple' copy so codes like SR-1098 match verbatim.
TSV_EXPR = (
    "setweight(to_tsvector('english'::regconfig, heading_path), 'B') || "
    "to_tsvector('english'::regconfig, text) || "
    "to_tsvector('simple'::regconfig, heading_path || ' ' || text)"
)


class Base(DeclarativeBase):
    pass


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    path: Mapped[str] = mapped_column(Text, unique=True)  # relative to docs_dir, posix
    folder: Mapped[str] = mapped_column(Text, index=True)  # top-level permission folder
    filename: Mapped[str] = mapped_column(Text)
    ext: Mapped[str] = mapped_column(String(16))
    title: Mapped[str] = mapped_column(Text)
    title_source: Mapped[str] = mapped_column(String(16))  # "core" | "style" | "filename"
    size: Mapped[int] = mapped_column(BigInteger)
    mtime: Mapped[float] = mapped_column(Float)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    version_key: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), default="ok")  # "ok" | "error"
    error: Mapped[str | None] = mapped_column(Text)
    indexed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    chunks: Mapped[list["Chunk"]] = relationship(
        back_populates="document", cascade="all, delete-orphan", passive_deletes=True
    )

    __table_args__ = (Index("ix_documents_folder_version", "folder", "version_key"),)


class Chunk(Base):
    __tablename__ = "chunks"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("documents.id", ondelete="CASCADE"), index=True
    )
    ord: Mapped[int] = mapped_column(Integer)
    heading_path: Mapped[str] = mapped_column(Text, default="")
    kind: Mapped[str] = mapped_column(String(16))  # "text" | "table" | "image_text"
    text: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    tsv: Mapped[Any] = mapped_column(TSVECTOR, Computed(TSV_EXPR, persisted=True))

    document: Mapped[Document] = relationship(back_populates="chunks")


class DocumentSummary(Base):
    """Machine-written overview of a document, keyed by content hash so copies, renames
    and moves share it. Its embedding is the document-level vector for find-the-doc."""

    __tablename__ = "document_summaries"

    sha256: Mapped[str] = mapped_column(String(64), primary_key=True)
    summary: Mapped[str] = mapped_column(Text)
    embedding: Mapped[list[float]] = mapped_column(Vector(EMBED_DIM))
    model: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IndexRun(Base):
    __tablename__ = "index_runs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    total: Mapped[int] = mapped_column(Integer, default=0)
    processed: Mapped[int] = mapped_column(Integer, default=0)
    counts: Mapped[dict[str, int]] = mapped_column(JSONB, default=dict)
    errors: Mapped[list[dict[str, str]]] = mapped_column(JSONB, default=list)


class User(Base):
    __tablename__ = "users"

    username: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, default="")
    first_login: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    last_login: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    disabled: Mapped[bool] = mapped_column(Boolean, default=False)


class SessionRow(Base):
    __tablename__ = "sessions"

    id: Mapped[str] = mapped_column(Text, primary_key=True)  # sha256 of the cookie token
    username: Mapped[str] = mapped_column(Text, index=True)
    display_name: Mapped[str] = mapped_column(Text, default="")
    groups: Mapped[list[str]] = mapped_column(ARRAY(Text))
    is_admin: Mapped[bool] = mapped_column(Boolean)
    csrf_token: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    last_seen: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ip: Mapped[str | None] = mapped_column(Text)


class GroupFolder(Base):
    __tablename__ = "group_folders"
    __table_args__ = (PrimaryKeyConstraint("group_dn", "folder"),)

    group_dn: Mapped[str] = mapped_column(Text)
    folder: Mapped[str] = mapped_column(Text)
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UserOverride(Base):
    __tablename__ = "user_overrides"
    __table_args__ = (
        PrimaryKeyConstraint("username", "folder"),
        CheckConstraint("effect IN ('allow', 'deny')", name="ck_user_overrides_effect"),
    )

    username: Mapped[str] = mapped_column(Text)
    folder: Mapped[str] = mapped_column(Text)
    effect: Mapped[str] = mapped_column(String(8))
    created_by: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AuditEntry(Base):
    """Append-only (a database trigger rejects UPDATE and DELETE)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    actor: Mapped[str] = mapped_column(Text)
    action: Mapped[str] = mapped_column(Text)
    target: Mapped[str] = mapped_column(Text, default="")
    detail: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    ip: Mapped[str | None] = mapped_column(Text)
