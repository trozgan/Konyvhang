"""Edge cases of the translation core: segments, EPUB reading, chunking, labels, build."""

import json
import re
import subprocess
import zipfile
from pathlib import Path

import pytest
from conftest import make_epub
from lxml import etree

from konyvhang import build, epub, llm, segment, translate, validate
from konyvhang.workdir import WorkDir, build_chunks, split_by_words

XHTML = "http://www.w3.org/1999/xhtml"


def echo(prompt: str) -> str:
    source = prompt.split("<source>")[1]
    return "\n".join(f'<seg id="{i}">HU {b}</seg>' for i, b in re.findall(r'<seg id="(\d+)">(.*?)</seg>', source))


def make_wd(tmp_path: Path, epub_path: Path, **state: str) -> WorkDir:
    wd = WorkDir(tmp_path / "work" / "book")
    wd.root.mkdir(parents=True)
    wd.source.write_bytes(epub_path.read_bytes())
    wd.save_state({"profile": "fiction", "model": "opus", "source_name": "book.epub", **state})
    wd.write_chunks()
    return wd


def rewrite(path: Path, changes: dict[str, str]) -> None:
    with zipfile.ZipFile(path) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    files.update({n: d.encode("utf-8") for n, d in changes.items()})
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)


# segment ------------------------------------------------------------------
def test_document_without_body_has_no_segments() -> None:
    assert segment.find_segments(etree.ElementTree(etree.fromstring(f'<html xmlns="{XHTML}"/>'))) == []


def test_comments_inside_a_segment_are_skipped() -> None:
    tree = etree.ElementTree(
        etree.fromstring(f'<html xmlns="{XHTML}"><body><p>A <!-- x --><em>b</em></p></body></html>')
    )
    seg = segment.find_segments(tree)[0]
    assert [segment.local(e) for e in segment.inline_elements(seg)] == ["em"]
    assert segment.to_markup(seg) == 'A <em n="1">b</em>'


# epub ---------------------------------------------------------------------
def test_spine_skips_non_xhtml_and_unknown_items(epub_file: Path) -> None:
    with zipfile.ZipFile(epub_file) as zf:
        opf = zf.read("OEBPS/content.opf").decode("utf-8")
    opf = opf.replace('<itemref idref="ch2"/>', '<itemref idref="ch2"/><itemref idref="css"/><itemref idref="ghost"/>')
    rewrite(epub_file, {"OEBPS/content.opf": opf})
    with zipfile.ZipFile(epub_file) as zf:
        assert epub.read_book(zf).spine == ["OEBPS/text/ch1.xhtml", "OEBPS/text/ch2.xhtml"]


# validate -----------------------------------------------------------------
def test_duplicate_and_extra_segments_are_reported() -> None:
    with pytest.raises(validate.ValidationError, match="többször"):
        validate.parse_segments('<seg id="1">a</seg><seg id="1">b</seg>')
    with pytest.raises(validate.ValidationError, match="Fölösleges szegmensek: 2"):
        validate.parse_and_check('<seg id="1">a</seg><seg id="2">b</seg>', {"1": "x"})


# workdir ------------------------------------------------------------------
def test_workdir_exists_and_glossary_roundtrip(tmp_path: Path) -> None:
    wd = WorkDir(tmp_path / "b")
    assert not wd.exists()
    wd.root.mkdir()
    wd.save_glossary({"terms": [{"source": "magi", "hu": "bölcsek"}]})
    assert "bölcsek" in wd.glossary_text()


def test_chunking_skips_empty_files_and_splits_long_ones(epub_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    empty = f'<html xmlns="{XHTML}"><body><p>* * *</p></body></html>'
    rewrite(epub_file, {"OEBPS/text/ch2.xhtml": empty})
    monkeypatch.setattr("konyvhang.workdir.MAX_WORDS", 10)
    chunks = build_chunks(epub_file)
    files = {s["file"] for c in chunks for s in c["segments"]}
    assert files == {"OEBPS/text/ch1.xhtml"}  # ch2 has no letters, ch1 is split
    assert len(chunks) > 1
    assert all(len(c["segments"]) for c in chunks)


def test_split_ends_with_a_closed_part() -> None:
    segs = [{"src": "a " * 10}, {"src": "b " * 9000}]
    assert [len(p) for p in split_by_words(segs)] == [2]


def test_split_by_words_keeps_the_tail(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("konyvhang.workdir.MAX_WORDS", 10)
    segs = [{"src": "w " * 6} for _ in range(5)]
    parts = split_by_words(segs)
    assert [len(p) for p in parts] == [2, 2, 1]


# translate ----------------------------------------------------------------
def test_run_without_caller_uses_the_book_provider(
    epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wd = make_wd(tmp_path, epub_file, provider="codex")
    used: list[str | None] = []

    def fake(prompt: str, system: str, model: str | None) -> llm.Result:
        used.append(model)
        if "<labels>" in prompt:
            return llm.Result(
                json.dumps(["HU"] * len(json.loads(prompt.split("<labels>")[1].split("</labels>", maxsplit=1)[0])))
            )
        return llm.Result(echo(prompt))

    monkeypatch.setattr(llm, "caller", lambda provider: fake if provider == "codex" else None)
    assert translate.run(wd, log=lambda m: None)
    assert used and wd.labels_path.exists()
    used.clear()
    assert translate.run(wd, log=lambda m: None)  # done and labels exist: no calls
    assert used == []


def test_single_segment_that_never_validates_raises(epub_file: Path, tmp_path: Path) -> None:
    wd = make_wd(tmp_path, epub_file)
    seg = wd.load_chunks()[0]["segments"][:1]
    with pytest.raises(validate.ValidationError, match="Egyetlen szegmens"):
        translate.translate_segments(wd, seg, [], lambda p, s, m: llm.Result("nope"), lambda m: None)


def test_book_without_labels_writes_empty_labels(
    epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wd = make_wd(tmp_path, epub_file)
    monkeypatch.setattr(translate, "collect_labels", lambda wd: [])
    translate.translate_labels(wd, lambda *a: pytest.fail("no call expected"), lambda m: None)
    assert json.loads(wd.labels_path.read_text(encoding="utf-8")) == {}


@pytest.mark.parametrize(
    ("answer", "message"),
    [
        (llm.UsageLimitError("limit"), "Elfogyott a keret"),
        (llm.LLMError("boom"), "nem sikerült"),
        ("no json here", "nem sikerült"),
        ('["csak egy"]', "száma eltér"),
    ],
)
def test_label_failures_keep_the_originals(
    epub_file: Path, tmp_path: Path, answer: str | Exception, message: str
) -> None:
    wd = make_wd(tmp_path, epub_file)
    logs: list[str] = []

    def call(prompt: str, system: str, model: str | None) -> llm.Result:
        if isinstance(answer, Exception):
            raise answer
        return llm.Result(answer)

    translate.translate_labels(wd, call, logs.append)
    assert not wd.labels_path.exists()
    assert any(message in line for line in logs)


# build ----------------------------------------------------------------------
def test_labels_without_nav_and_ncx(epub_file: Path) -> None:
    with zipfile.ZipFile(epub_file) as zf:
        book = epub.read_book(zf)
        book.nav_path = book.ncx_path = None
        assert list(build.label_elements(zf, book)) == ["OEBPS/content.opf"]


def test_set_lang_on_non_html_root_and_nested_lang() -> None:
    tree = etree.ElementTree(etree.fromstring('<ncx><p lang="en"><b>x</b></p></ncx>'))
    build.set_lang(tree)
    root = tree.getroot()
    assert root.get("lang") is None and root.get(build.XML_LANG) == "hu"
    assert root[0].get("lang") == "hu"


@pytest.mark.parametrize(("stdout", "stderr", "expected"), [("a\nNo errors", "", "No errors"), ("", "oops", "oops")])
def test_build_runs_epubcheck_when_installed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, stdout: str, stderr: str, expected: str
) -> None:
    src = make_epub(tmp_path / "b.epub")
    wd = make_wd(tmp_path, src)
    monkeypatch.setattr("konyvhang.build.shutil.which", lambda name: "/bin/epubcheck")
    monkeypatch.setattr(
        "konyvhang.build.subprocess.run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout=stdout, stderr=stderr),
    )
    logs: list[str] = []
    build.build(wd, partial=True, log=logs.append)
    assert logs[-1] == expected
