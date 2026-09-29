"""Loader registry. The set of registered extensions is also the upload allow-list,
so adding a loader for a new format enables both indexing and uploading it."""

from pathlib import Path

from rug.loaders.base import LoadedDocument, Loader, Section
from rug.loaders.docx_loader import DocxLoader

_REGISTRY: dict[str, Loader] = {}


class UnsupportedFormat(ValueError):
    pass


def register(loader: Loader) -> None:
    for ext in loader.extensions:
        _REGISTRY[ext.lower()] = loader


def supported_extensions() -> frozenset[str]:
    return frozenset(_REGISTRY)


def get_loader(path: Path | str) -> Loader:
    ext = Path(path).suffix.lower()
    try:
        return _REGISTRY[ext]
    except KeyError:
        raise UnsupportedFormat(f"no loader registered for {ext!r}") from None


def validate(path: Path) -> None:
    """Raise UnsupportedFormat or ValueError unless `path` is a well-formed file of a
    registered format."""
    get_loader(path).validate(path)


def load(path: Path) -> LoadedDocument:
    return get_loader(path).load(path)


register(DocxLoader())

__all__ = [
    "LoadedDocument",
    "Loader",
    "Section",
    "UnsupportedFormat",
    "get_loader",
    "load",
    "register",
    "validate",
    "supported_extensions",
]
