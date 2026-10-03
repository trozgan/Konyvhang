"""Integrity of the translation core: what reaches the model, the segment mapping and a valid EPUB.

These tests pin behaviour that the rest of the suite executes but never checks.
"""

import json
import zipfile
from pathlib import Path

import pytest
from conftest import CH2
from lxml import etree

from konyvhang import build, epub, segment, translate, validate
from konyvhang.workdir import WorkDir, build_chunks, split_by_words

XHTML = "http://www.w3.org/1999/xhtml"
OPS = "http://www.idpf.org/2007/ops"


def rewrite(path: Path, changes: dict[str, str]) -> None:
    with zipfile.ZipFile(path) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    files.update({n: d.encode("utf-8") for n, d in changes.items()})
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


def read_entry(path: Path, name: str) -> str:
    with zipfile.ZipFile(path) as zf:
        return zf.read(name).decode("utf-8")


def make_wd(tmp_path: Path, epub_path: Path) -> WorkDir:
    wd = WorkDir(tmp_path / "work" / "book")
    wd.root.mkdir(parents=True)
    wd.source.write_bytes(epub_path.read_bytes())
    wd.save_state({"profile": "fiction", "model": "opus", "source_name": "book.epub"})
    wd.write_chunks()
    return wd


def body(inner: str) -> etree._ElementTree:
    return etree.ElementTree(etree.fromstring(f'<html xmlns="{XHTML}"><body>{inner}</body></html>'))


# validate -----------------------------------------------------------------
def test_inline_tags_must_keep_their_numbers() -> None:
    source = {"1": '<i n="1">a</i> and <b n="2">b</b>'}
    with pytest.raises(validate.ValidationError, match="címkéi eltérnek"):  # tag types swapped between numbers
        validate.parse_and_check('<seg id="1"><b n="1">a</b> és <i n="2">b</i></seg>', source)
    same_tags = {"1": '<i n="1">a</i> and <i n="2">b</i>'}
    with pytest.raises(validate.ValidationError, match="címkéi eltérnek"):  # one number twice, the other lost
        validate.parse_and_check('<seg id="1"><i n="1">a</i> és <i n="1">b</i></seg>', same_tags)
    reordered = validate.parse_and_check('<seg id="1"><i n="2">b</i> és <i n="1">a</i></seg>', same_tags)
    assert [el.get("n") for el in reordered["1"]] == ["2", "1"]


def test_answer_with_only_a_closing_seg_tag_is_rejected() -> None:
    with pytest.raises(validate.ValidationError):
        validate.parse_segments("Sorry. </seg>")


def test_every_problem_of_an_answer_is_reported() -> None:
    source = {"1": "x", "2": '<i n="1">y</i>'}
    with pytest.raises(validate.ValidationError) as exc:
        validate.parse_and_check('<seg id="2">y</seg>', source)
    message = str(exc.value)
    assert "Hiányzó szegmensek: 1." in message
    assert "A 2 szegmens címkéi eltérnek" in message


# translate.inner_markup -----------------------------------------------------
def test_inner_markup_of_an_empty_segment_is_empty() -> None:
    segs = validate.parse_segments('<seg id="1"/>\n<seg id="2">x <i n="1">y</i></seg>\n')
    assert translate.inner_markup(segs["1"]) == ""
    assert translate.inner_markup(segs["2"]) == 'x <i n="1">y</i>'


# segment ------------------------------------------------------------------
def test_blocks_without_letters_are_not_segments() -> None:
    tree = body("<p>Before</p><p>2024</p><p>12<br/>34</p><p/><p> </p><p>After</p>")
    assert [segment.to_markup(s) for s in segment.find_segments(tree)] == ["Before", "After"]


def test_text_directly_in_body_is_not_a_segment() -> None:
    assert segment.find_segments(body("Loose text")) == []


def test_semantics_reads_epub_type_and_aria_roles() -> None:
    def sem(attrs: str) -> set[str]:
        return segment.semantics(etree.fromstring(f'<aside xmlns:epub="{OPS}" {attrs}/>'))

    assert sem("") == set()
    assert sem('epub:type="footnote"') == {"footnote"}
    assert sem('role="doc-footnote"') == {"doc-footnote", "footnote"}
    assert sem('epub:type="noteref" role="doc-endnote"') == {"noteref", "doc-endnote", "endnote"}
    tree = body('<section role="doc-bibliography"><p>Smith, J. A Book.</p></section>')
    assert segment.in_reference_matter(segment.find_segments(tree)[0])


def test_local_name_of_a_comment_is_empty() -> None:
    assert segment.local(etree.Comment("x")) == ""


# epub ---------------------------------------------------------------------
def test_spine_keeps_chapters_after_a_non_xhtml_or_unknown_item(epub_file: Path) -> None:
    opf = read_entry(epub_file, "OEBPS/content.opf")
    opf = opf.replace('<itemref idref="ch2"/>', '<itemref idref="img"/><itemref idref="ghost"/><itemref idref="ch2"/>')
    rewrite(epub_file, {"OEBPS/content.opf": opf})
    with zipfile.ZipFile(epub_file) as zf:
        assert epub.read_book(zf).spine == ["OEBPS/text/ch1.xhtml", "OEBPS/text/ch2.xhtml"]


def test_fragment_in_a_manifest_href_is_ignored(epub_file: Path) -> None:
    opf = read_entry(epub_file, "OEBPS/content.opf").replace('href="text/ch2.xhtml"', 'href="text/ch2.xhtml#start"')
    rewrite(epub_file, {"OEBPS/content.opf": opf})
    with zipfile.ZipFile(epub_file) as zf:
        assert epub.read_book(zf).spine[-1] == "OEBPS/text/ch2.xhtml"


def test_mimetype_is_written_first_and_stored(tmp_path: Path) -> None:
    src, dest = tmp_path / "in.epub", tmp_path / "out.epub"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("META-INF/container.xml", "<container/>", compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("mimetype", "application/epub+zip", compress_type=zipfile.ZIP_DEFLATED)
        zf.writestr("img.png", b"png", compress_type=zipfile.ZIP_STORED)
    epub.write_epub(str(src), str(dest), {"META-INF/container.xml": b"<new/>"})
    with zipfile.ZipFile(dest) as zf:
        infos = zf.infolist()
        assert [(i.filename, i.compress_type) for i in infos] == [
            ("mimetype", zipfile.ZIP_STORED),
            ("META-INF/container.xml", zipfile.ZIP_DEFLATED),
            ("img.png", zipfile.ZIP_STORED),
        ]
        assert zf.read("mimetype") == b"application/epub+zip"
        assert zf.read("META-INF/container.xml") == b"<new/>"


def test_entities_declared_in_a_document_are_not_expanded(tmp_path: Path) -> None:
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET", encoding="utf-8")
    doc = (
        f'<!DOCTYPE html [<!ENTITY x "INJECTED"><!ENTITY s SYSTEM "{secret.as_uri()}">]>'
        f'<html xmlns="{XHTML}"><body><p>a&x;b&s;c</p></body></html>'
    )
    tree = epub.parse_xml(doc.encode("utf-8"))
    assert "".join(tree.getroot().itertext()) == "a&x;b&s;c"  # references stay references
    out = epub.serialize(tree)
    assert b"SECRET" not in out
    assert b"<p>a&x;b&s;c</p>" in out


def test_external_dtd_is_not_loaded(tmp_path: Path) -> None:
    dtd = tmp_path / "broken.dtd"
    dtd.write_text("this is not a DTD <<<", encoding="utf-8")
    doc = f'<!DOCTYPE html SYSTEM "{dtd.as_uri()}"><html xmlns="{XHTML}"><body><p>ok</p></body></html>'
    assert "".join(epub.parse_xml(doc.encode("utf-8")).getroot().itertext()) == "ok"


def test_deeply_nested_documents_parse() -> None:
    doc = f'<html xmlns="{XHTML}"><body>{"<div>" * 300}<p>deep</p>{"</div>" * 300}</body></html>'
    tree = epub.parse_xml(doc.encode("utf-8"))
    assert [segment.to_markup(s) for s in segment.find_segments(tree)] == ["deep"]


def test_uppercase_html_entities_become_characters() -> None:
    tree = epub.parse_xml(b"<p>&Eacute;va &AElig;</p>")
    assert tree.getroot().text == "Éva Æ"


# build --------------------------------------------------------------------
@pytest.fixture
def three_chunks(epub_file: Path, tmp_path: Path) -> WorkDir:
    """The test book in three chunks: ch1 start, ch1 rest, ch2."""
    wd = make_wd(tmp_path, epub_file)
    segs = wd.load_chunks()[0]["segments"]
    ch1 = [s for s in segs if s["file"].endswith("ch1.xhtml")]
    ch2 = [s for s in segs if s["file"].endswith("ch2.xhtml")]
    for i, part in enumerate([ch1[:2], ch1[2:], ch2], start=1):
        wd.save_chunk({"id": f"{i:04d}", "segments": part, "translation": None})
    return wd


def translate_chunk(wd: WorkDir, index: int) -> None:
    chunk = wd.load_chunks()[index]
    chunk["translation"] = [f"Ő {s['src']}" for s in chunk["segments"]]
    wd.save_chunk(chunk)


def test_partial_build_translates_chunks_after_an_untranslated_one(three_chunks: WorkDir) -> None:
    wd = three_chunks
    translate_chunk(wd, 0)
    translate_chunk(wd, 2)
    out = build.build(wd, partial=True, log=lambda m: None)
    ch1 = read_entry(out, "OEBPS/text/ch1.xhtml")
    assert "Ő Chapter One" in ch1 and "Mary looked up." in ch1 and "Ő Mary" not in ch1
    assert "Ő Chapter Two" in read_entry(out, "OEBPS/text/ch2.xhtml")


def test_built_chapters_are_utf8_xhtml_with_their_doctype(three_chunks: WorkDir) -> None:
    translate_chunk(three_chunks, 0)
    out = build.build(three_chunks, partial=True, log=lambda m: None)
    with zipfile.ZipFile(out) as zf:
        data = zf.read("OEBPS/text/ch1.xhtml")
    assert data.startswith(b"<?xml version='1.0' encoding='utf-8'?>\n<!DOCTYPE html>\n")
    assert "Ő Chapter One".encode() in data


def test_building_twice_replaces_the_output(three_chunks: WorkDir) -> None:
    first = build.build(three_chunks, partial=True, log=lambda m: None)
    translate_chunk(three_chunks, 2)
    second = build.build(three_chunks, partial=True, log=lambda m: None)
    assert second == first
    assert "Ő Chapter Two" in read_entry(second, "OEBPS/text/ch2.xhtml")


def test_unfinished_book_is_refused_with_a_message(three_chunks: WorkDir) -> None:
    with pytest.raises(SystemExit) as exc:
        build.build(three_chunks, log=lambda m: None)
    assert isinstance(exc.value.code, str) and "--partial" in exc.value.code  # a message, not exit status 0


def test_translation_shorter_than_its_chunk_is_rejected(three_chunks: WorkDir) -> None:
    chunk = three_chunks.load_chunks()[2]
    chunk["translation"] = ["Ő csak egy"]
    three_chunks.save_chunk(chunk)
    with pytest.raises(ValueError, match="shorter"):
        build.build(three_chunks, partial=True, log=lambda m: None)


def test_nav_and_ncx_get_hungarian_labels_and_language(three_chunks: WorkDir) -> None:
    labels = {"The Test Book": "A tesztkönyv", "Chapter One": "Első fejezet"}
    three_chunks.labels_path.write_text(json.dumps(labels), encoding="utf-8")
    out = build.build(three_chunks, partial=True, log=lambda m: None)
    ncx = etree.fromstring(read_entry(out, "OEBPS/toc.ncx").encode("utf-8"))
    assert ncx.findtext(".//ncx:docTitle/ncx:text", namespaces=epub.NS) == "A tesztkönyv"
    assert ncx.get("lang") is None  # the NCX is not XHTML
    nav = etree.fromstring(read_entry(out, "OEBPS/nav.xhtml").encode("utf-8"))
    assert nav.get("lang") == "hu" and nav.get(build.XML_LANG) == "hu"


def test_set_lang_marks_any_html_root_and_any_root_with_lang() -> None:
    html = etree.ElementTree(etree.fromstring(f'<html xmlns="{XHTML}"><body/></html>'))
    build.set_lang(html)
    assert html.getroot().get("lang") == "hu"
    other = etree.ElementTree(etree.fromstring('<svg lang="en"><text>x</text></svg>'))
    build.set_lang(other)
    assert other.getroot().get("lang") == "hu"


def test_label_text_collapses_whitespace() -> None:
    assert build.label_text(etree.fromstring("<a>\n  Chapter\n\t <b>Two</b>  </a>")) == "Chapter Two"


# workdir ------------------------------------------------------------------
def test_usage_accumulates_across_calls(tmp_path: Path) -> None:
    wd = WorkDir(tmp_path / "b")
    wd.root.mkdir()
    wd.save_state({})
    wd.add_usage({"input_tokens": 10, "cost_usd": 0.5})
    wd.add_usage({"input_tokens": 5})
    assert wd.load_state()["usage"] == {"input_tokens": 15, "cost_usd": 0.5, "calls": 2}


def test_missing_glossary_reads_as_empty(tmp_path: Path) -> None:
    assert WorkDir(tmp_path / "b").glossary_text() == ""


def test_split_by_words_makes_even_parts(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("konyvhang.workdir.MAX_WORDS", 4)
    segs = [{"src": f"w{i}"} for i in range(9)]
    assert [len(p) for p in split_by_words(segs)] == [3, 3, 3]


def test_files_that_fill_a_chunk_exactly_share_it(epub_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def words(chunk: dict[str, list[dict[str, str]]]) -> int:
        return sum(len(segment.plain_text(s["src"]).split()) for s in chunk["segments"])

    (whole,) = build_chunks(epub_file)
    total = words(whole)
    monkeypatch.setattr("konyvhang.workdir.MAX_WORDS", total)
    assert len(build_chunks(epub_file)) == 1
    monkeypatch.setattr("konyvhang.workdir.MAX_WORDS", total - 1)
    assert len(build_chunks(epub_file)) == 2


def test_reference_matter_is_left_out_by_default(epub_file: Path, tmp_path: Path) -> None:
    bib = f'<section epub:type="bibliography" xmlns:epub="{OPS}"><p>Smith, J. A Book.</p></section></body>'
    rewrite(epub_file, {"OEBPS/text/ch2.xhtml": CH2.replace("</body>", bib)})
    wd = make_wd(tmp_path, epub_file)

    def chunked() -> str:
        return " ".join(s["src"] for c in wd.load_chunks() for s in c["segments"])

    assert "Smith" not in chunked() and "Smith" not in str(build_chunks(epub_file))
    wd.write_chunks(references=True)
    assert "Smith" in chunked()
