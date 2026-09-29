import io
import zipfile
from pathlib import Path

import docx
import pytest

from rug import loaders, uploads
from rug.indexer import Indexer, scan_disk
from rug.uploads import UploadRejected, sanitize_filename, store


def docx_bytes(text="Scope: build a widget.") -> bytes:
    d = docx.Document()
    d.add_paragraph(text)
    buf = io.BytesIO()
    d.save(buf)
    return buf.getvalue()


def chunked(data: bytes, n=1000):
    for i in range(0, len(data), n):
        yield data[i : i + n]


@pytest.fixture
def nas(docs_dir: Path) -> Path:
    (docs_dir / "sales").mkdir()
    return docs_dir


def leftovers(root: Path) -> list[str]:
    return sorted(p.name for p in root.rglob("*") if p.is_file())


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("plan.docx", "plan.docx"),
        ("C:\\Users\\me\\Plan v2.DOCX", "Plan v2.DOCX"),
        ("../../etc/plan.docx", "plan.docx"),
        ("  spaced name.docx ", "spaced name.docx"),
        ("caf\u0065\u0301.docx", "caf\u00e9.docx"),  # NFC
    ],
)
def test_sanitize_ok(raw, expected):
    assert sanitize_filename(raw) == expected


@pytest.mark.parametrize(
    "raw,status",
    [
        ("", 400),
        ("..", 400),
        ("a\x00b.docx", 400),
        ("a\nb.docx", 400),
        (".hidden.docx", 400),
        ("~$lock.docx", 400),
        ("a<b>.docx", 400),
        ("a?.docx", 400),
        ("a:b.docx", 400),
        ("CON.docx", 400),
        ("com1.docx", 400),
        ("report .docx", 400),
        ("x.exe", 415),
        ("x.docx.exe", 415),
        ("noext", 415),
        ("x.pdf", 415),
        ("x.docm", 415),
    ],
)
def test_sanitize_rejects(raw, status):
    with pytest.raises(UploadRejected) as e:
        sanitize_filename(raw)
    assert e.value.status == status


def test_long_names_are_capped():
    name = sanitize_filename("é" * 300 + ".docx")
    assert len(name.encode()) <= uploads.MAX_NAME_BYTES and name.endswith(".docx")


def test_store_and_never_overwrite(nas):
    data = docx_bytes()
    assert store(nas, "sales", "a.docx", chunked(data), 10**7) == "sales/a.docx"
    assert store(nas, "sales", "a.docx", chunked(data), 10**7) == "sales/a (1).docx"
    assert (
        store(nas, "sales", "A.DOCX", chunked(data), 10**7) == "sales/A (2).DOCX"
    )  # case-insensitive
    assert leftovers(nas) == ["A (2).DOCX", "a (1).docx", "a.docx"]
    loaders.validate(nas / "sales" / "a.docx")


@pytest.mark.parametrize(
    "body,status",
    [
        (b"", 400),
        (b"MZ not a zip at all", 415),
        (b"PK\x03\x04 but truncated", 415),
    ],
)
def test_content_validation(nas, body, status):
    with pytest.raises(UploadRejected) as e:
        store(nas, "sales", "a.docx", chunked(body), 10**7)
    assert e.value.status == status
    assert leftovers(nas) == []


def test_zip_without_word_document_rejected(nas):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("hello.txt", "hi")
    with pytest.raises(UploadRejected) as e:
        store(nas, "sales", "a.docx", chunked(buf.getvalue()), 10**7)
    assert e.value.status == 415 and leftovers(nas) == []


def test_size_cap_enforced_while_streaming(nas):
    seen = []

    def endless():
        for _ in range(10_000):
            seen.append(1)
            yield b"x" * 1024

    with pytest.raises(UploadRejected) as e:
        store(nas, "sales", "a.docx", endless(), 10 * 1024)
    assert e.value.status == 413
    assert len(seen) < 20  # stopped early, did not consume the whole stream
    assert leftovers(nas) == []


def test_source_error_leaves_nothing(nas):
    def broken():
        yield b"PK"
        raise ConnectionError("client went away")

    with pytest.raises(ConnectionError):
        store(nas, "sales", "a.docx", broken(), 10**7)
    assert leftovers(nas) == []


@pytest.mark.parametrize("folder", ["", "..", "../x", "a/b", ".git", "nope"])
def test_folder_must_exist_and_be_top_level(nas, folder):
    with pytest.raises(UploadRejected):
        store(nas, folder, "a.docx", chunked(docx_bytes()), 10**7)
    assert not (nas.parent / "x").exists() and not (nas / "nope").exists()
    assert leftovers(nas) == []


def test_symlinked_folder_rejected(nas, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (nas / "link").symlink_to(outside)
    with pytest.raises(UploadRejected):
        store(nas, "link", "a.docx", chunked(docx_bytes()), 10**7)
    assert list(outside.iterdir()) == []


def test_scanner_ignores_temp_and_reservation(nas):
    (nas / "sales" / ".rug-upload-abc.docx").write_bytes(docx_bytes())
    (nas / "sales" / "reserved.docx").write_bytes(b"")
    assert scan_disk(nas) == {}


def test_sync_one_indexes_just_that_file(db, embedder, nas):
    rel = store(
        nas, "sales", "Boost SR-1 SOW.docx", chunked(docx_bytes("Scope is widgets.")), 10**7
    )
    idx = Indexer(db, embedder, nas)
    assert idx.sync_one(rel) == "added"
    assert idx.sync_one(rel) == "unchanged"
    from rug.db.models import Document

    doc = db.query(Document).one()
    assert doc.path == rel and doc.folder == "sales" and doc.status == "ok"
