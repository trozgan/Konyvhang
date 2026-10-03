import zipfile
from pathlib import Path

from konyvhang import epub


def test_read_book_spine_skips_nav(epub_file: Path) -> None:
    with zipfile.ZipFile(epub_file) as zf:
        book = epub.read_book(zf)
    assert book.opf_path == "OEBPS/content.opf"
    assert book.spine == ["OEBPS/text/ch1.xhtml", "OEBPS/text/ch2.xhtml"]
    assert book.nav_path == "OEBPS/nav.xhtml"
    assert book.ncx_path == "OEBPS/toc.ncx"


def test_roundtrip_without_changes_is_byte_identical(epub_file: Path, tmp_path: Path) -> None:
    out = tmp_path / "out.epub"
    epub.write_epub(str(epub_file), str(out), {})
    with zipfile.ZipFile(epub_file) as a, zipfile.ZipFile(out) as b:
        assert b.namelist()[0] == "mimetype"
        assert b.getinfo("mimetype").compress_type == zipfile.ZIP_STORED
        assert sorted(a.namelist()) == sorted(b.namelist())
        for name in a.namelist():
            assert a.read(name) == b.read(name)


def test_parse_xml_handles_html_entities() -> None:
    tree = epub.parse_xml(b'<p xmlns="http://www.w3.org/1999/xhtml">a&nbsp;b &amp; c&mdash;d</p>')
    assert tree.getroot().text == "a b & c—d"
