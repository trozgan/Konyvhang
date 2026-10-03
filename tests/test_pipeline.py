"""translate + build end to end, with a fake claude."""

import json
import re
import zipfile
from collections.abc import Sequence
from pathlib import Path

import pytest

from konyvhang import build, llm, translate
from konyvhang.workdir import WorkDir


def fake_translation(prompt: str) -> str:
    """Echo every <seg> of the source with 'HU ' before its text."""
    source = prompt.split("<source>")[1]
    return "\n".join(
        f'<seg id="{sid}">HU {body}</seg>' for sid, body in re.findall(r'<seg id="(\d+)">(.*?)</seg>', source)
    )


def fake_labels(prompt: str) -> str:
    labels = json.loads(prompt.split("<labels>")[1].split("</labels>", maxsplit=1)[0])
    return json.dumps([f"HU {x}" for x in labels])


class FakeClaude:
    def __init__(self, script: Sequence[str] = ()) -> None:
        self.script = list(script)  # per call: "ok", "bad" or "limit"
        self.calls = 0

    def __call__(self, prompt: str, system: str, model: str | None) -> llm.Result:
        self.calls += 1
        action = self.script.pop(0) if self.script else "ok"
        if action == "limit":
            raise llm.UsageLimitError("You've hit your limit · resets 5pm")
        if action == "bad":
            return llm.Result("Sorry.")
        text = fake_labels(prompt) if "<labels>" in prompt else fake_translation(prompt)
        return llm.Result(text, {"input_tokens": 10, "output_tokens": 5})


@pytest.fixture
def wd(epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkDir:
    monkeypatch.setattr("konyvhang.workdir.MAX_WORDS", 30)  # ch1 and ch2 become separate chunks
    wd = WorkDir(tmp_path / "work" / "book")
    wd.root.mkdir(parents=True)
    wd.source.write_bytes(epub_file.read_bytes())
    wd.save_state({"profile": "fiction", "model": "opus", "source_name": "book.epub"})
    wd.write_chunks()
    return wd


def test_chunks(wd: WorkDir) -> None:
    chunks = wd.load_chunks()
    assert [len(c["segments"]) for c in chunks] == [5, 3]


def test_full_run_and_build(wd: WorkDir) -> None:
    fake = FakeClaude()
    assert translate.run(wd, call=fake, log=lambda m: None)
    assert fake.calls == 3  # two chunks + labels
    out = build.build(wd, log=lambda m: None)

    with zipfile.ZipFile(out) as zf:
        ch1 = zf.read("OEBPS/text/ch1.xhtml").decode()
        opf = zf.read("OEBPS/content.opf").decode()
        nav = zf.read("OEBPS/nav.xhtml").decode()
        ncx = zf.read("OEBPS/toc.ncx").decode()
        assert zf.read("OEBPS/style.css") == b"p { margin: 0 }"
    assert '<h1 class="title" id="c1">HU Chapter One</h1>' in ch1
    assert '<p class="first">HU It was a <em>dark</em>' in ch1
    assert '<p class="center">* * *</p>' in ch1
    assert '<img src="../img/pic.png" alt="pic"/>' in ch1
    assert 'xml:lang="hu" lang="hu"' in ch1
    assert "<!DOCTYPE html>" in ch1
    assert "<dc:language>hu</dc:language>" in opf
    assert "<dc:title>HU The Test Book</dc:title>" in opf
    assert '<a href="text/ch2.xhtml">HU Chapter Two</a>' in nav
    assert "<text>HU Chapter One</text>" in ncx


def test_bad_response_is_retried(wd: WorkDir) -> None:
    fake = FakeClaude(["bad", "ok"])
    translate.run(wd, call=fake, max_chunks=1, log=lambda m: None)
    assert fake.calls == 2
    assert wd.load_chunks()[0]["translation"] is not None


def test_repeated_bad_response_splits_chunk(wd: WorkDir) -> None:
    fake = FakeClaude(["bad", "bad"])
    translate.run(wd, call=fake, max_chunks=1, log=lambda m: None)
    assert fake.calls == 4  # two failures, then the two halves
    assert len(wd.load_chunks()[0]["translation"]) == 5


def test_usage_limit_stops_and_resumes(wd: WorkDir) -> None:
    first = FakeClaude(["ok", "limit"])
    assert not translate.run(wd, call=first, log=lambda m: None)
    chunks = wd.load_chunks()
    assert chunks[0]["translation"] is not None and chunks[1]["translation"] is None

    second = FakeClaude()
    assert translate.run(wd, call=second, log=lambda m: None)
    assert second.calls == 2  # the second chunk and the labels
    assert wd.load_state()["usage"]["calls"] == 3


def test_build_refuses_unfinished_without_partial(wd: WorkDir) -> None:
    translate.run(wd, call=FakeClaude(), max_chunks=1, log=lambda m: None)
    with pytest.raises(SystemExit):
        build.build(wd, log=lambda m: None)
    build.build(wd, partial=True, log=lambda m: None)


def test_reference_matter_is_not_chunked_unless_asked(tmp_path: Path) -> None:
    from conftest import CH1, make_epub

    from konyvhang.workdir import build_chunks

    bib = (
        '<section epub:type="bibliography" xmlns:epub="http://www.idpf.org/2007/ops"><h2>References</h2>'
        "<p>Smith, J. (2020). A Book. Publisher.</p></section></body>"
    )
    path = make_epub(tmp_path / "b.epub")
    import zipfile

    with zipfile.ZipFile(path) as zf:
        files = {n: zf.read(n) for n in zf.namelist()}
    files["OEBPS/text/ch1.xhtml"] = CH1.replace("</body>", bib).encode()
    with zipfile.ZipFile(path, "w") as zf:
        for n, data in files.items():
            zf.writestr(n, data)

    def texts(references: bool) -> list[str]:
        return [s["src"] for c in build_chunks(path, references) for s in c["segments"]]

    assert not any("Smith" in t or "References" in t for t in texts(False))
    assert any("Smith" in t for t in texts(True)) and "References" in texts(True)
