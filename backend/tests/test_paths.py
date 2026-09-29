import pytest

from rug.paths import UnsafePath, safe_doc_path


def test_accepts_regular_file(tmp_path):
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "x.docx").write_bytes(b"x")
    assert safe_doc_path(tmp_path, "a/x.docx") == tmp_path / "a" / "x.docx"


@pytest.mark.parametrize(
    "rel", ["", "/etc/passwd", "a\x00b", "../outside.docx", "a/../../outside.docx"]
)
def test_rejects_bad_paths(tmp_path, rel):
    with pytest.raises(UnsafePath):
        safe_doc_path(tmp_path, rel)


def test_rejects_symlinks(tmp_path):
    outside = tmp_path / "secret.docx"
    outside.write_bytes(b"x")
    root = tmp_path / "root"
    root.mkdir()
    (root / "link.docx").symlink_to(outside)
    with pytest.raises(UnsafePath):
        safe_doc_path(root, "link.docx")
