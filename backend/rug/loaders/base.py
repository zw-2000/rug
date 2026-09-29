from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol

SectionKind = Literal["text", "table", "image_text"]


class LoaderEnvironmentError(RuntimeError):
    """The machine can't process files right now (e.g. Tesseract missing). Unlike a bad
    file, this must not be recorded against the document: the file is retried next run."""


@dataclass
class Section:
    """One block of document content, in reading order."""

    text: str
    heading_path: tuple[str, ...] = ()
    kind: SectionKind = "text"


@dataclass
class LoadedDocument:
    title: str | None  # None -> caller falls back to the filename
    title_source: str  # "core" | "style" | "filename"
    sections: list[Section] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


class Loader(Protocol):
    extensions: tuple[str, ...]

    def load(self, path: Path) -> LoadedDocument: ...

    def looks_valid(self, head: bytes) -> bool:
        """Cheap content sniff (magic bytes) used to validate uploads."""
        ...
