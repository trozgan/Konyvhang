import json
import subprocess
import zipfile

import numpy as np
import pytest

from konyvhang import audio
from konyvhang.workdir import WorkDir


def test_speech_text_drops_footnote_markers_and_keeps_line_breaks():
    markup = 'Családunknak<br n="1"/>szeretettel, <a n="2"><sup n="3">1</sup></a>mondta.'
    assert audio.speech_text(markup) == "Családunknak szeretettel, mondta."


def test_split_long_at_sentence_ends():
    text = " ".join(["Ez egy mondat, amely elég hosszú ahhoz, hogy számítson."] * 20)
    pieces = audio.split_long(text)
    assert len(pieces) > 1
    assert all(len(p) <= audio.MAX_PIECE_CHARS for p in pieces)
    assert " ".join(pieces) == text


def test_similarity_ignores_case_punctuation_and_digits():
    assert audio.similarity("– Megveszi a hajamat? – kérdezte.", "Megveszi a hajamat, kérdezte") == 1.0
    assert audio.similarity("Huszonöt év", "25 év") < 1.0
    assert audio.similarity("Egy teljesen más mondat.", "Semmi köze hozzá") < audio.MIN_SIMILARITY


def test_follow_waits_for_new_chunks_and_stops_when_book_is_done(monkeypatch):
    states = iter([[1, 0, 0], [1, 0, 0], [1, 1, 0], [1, 1, 1], [1, 1, 1]])
    current = {"chunks": [1, 0, 0]}

    class FakeWorkDir:
        def load_chunks(self):
            return [{"translation": [] if t else None} for t in current["chunks"]]

        def chunk_paths(self):
            return current["chunks"]

    def fake_sleep(_):
        current["chunks"] = next(states)

    passes = []
    monkeypatch.setattr(audio, "run", lambda wd, voice, skip, log: passes.append(list(current["chunks"])))
    monkeypatch.setattr(audio.time, "sleep", fake_sleep)
    audio.follow(FakeWorkDir(), None, [], log=lambda m: None)
    assert passes == [[1, 0, 0], [1, 1, 0], [1, 1, 1]]


def test_similarity_handles_long_texts_with_spelling_variants():
    text = "Ebből a nézőpontból Bertha néni gondolatai és tettei ugyanolyan észszerűek. " * 4
    heard = text.replace("Bertha", "Berta")
    assert audio.similarity(text, heard) > 0.95


def test_add_pieces_uses_spoken_form_before_splitting():
    book = [{"segments": [{"text": "A 2. fejezet.", "heading": False}, {"text": "Cím", "heading": True}]}]
    audio.add_pieces(book, {"A 2. fejezet.": "A második fejezet."})
    assert book[0]["pieces"] == [
        {"text": "A második fejezet.", "pause": audio.PAUSE["paragraph"]},
        {"text": "Cím", "pause": audio.PAUSE["heading"]},
    ]


# chapters, footnotes, metadata ---------------------------------------------


X = 'xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
FILES = {
    "OEBPS/content.opf": """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>The Book</dc:title><dc:title>A Subtitle</dc:title><dc:creator>Ann Author</dc:creator></metadata>
<manifest><item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>
<item id="c" href="copy.xhtml" media-type="application/xhtml+xml"/>
<item id="a" href="ch1.xhtml" media-type="application/xhtml+xml"/>
<item id="f" href="note.xhtml" media-type="application/xhtml+xml"/>
<item id="img" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/></manifest>
<spine><itemref idref="c"/><itemref idref="a"/><itemref idref="f"/></spine></package>""",
    "OEBPS/nav.xhtml": f"""<html {X}><body>
<nav epub:type="toc"><ol><li><a href="ch1.xhtml">Chapter One</a></li></ol></nav>
<nav epub:type="landmarks"><ol><li><a epub:type="copyright-page" href="copy.xhtml">Copyright</a></li></ol></nav>
</body></html>""",
    "OEBPS/copy.xhtml": f"<html {X}><body><p>Copyright 2023 by someone.</p></body></html>",
    "OEBPS/ch1.xhtml": f"""<html {X}><body><h1>Chapter One</h1>
<p>First paragraph.<a epub:type="noteref" href="note.xhtml#n1">[*]</a></p><p>Second paragraph.</p></body></html>""",
    "OEBPS/note.xhtml": f"""<html {X}><body><div epub:type="footnote" id="n1">
<p><a href="ch1.xhtml">*</a> The note text.</p></div></body></html>""",
}


@pytest.fixture
def book_wd(tmp_path):
    src = tmp_path / "b.epub"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>',
        )
        for name, data in FILES.items():
            zf.writestr(name, data)
        cover = tmp_path / "cover.jpg"
        subprocess.run(
            ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=red:s=16x16", "-frames:v", "1", str(cover)],
            check=True,
        )
        zf.write(cover, "OEBPS/cover.jpg")
    wd = WorkDir(tmp_path / "work" / "b")
    wd.root.mkdir(parents=True)
    wd.source.write_bytes(src.read_bytes())
    wd.save_state({"profile": "fiction", "model": "opus", "source_name": "b.epub"})
    wd.write_chunks()
    for chunk in wd.load_chunks():  # "translate" by echoing the source
        chunk["translation"] = [s["src"] for s in chunk["segments"]]
        wd.save_chunk(chunk)
    wd.labels_path.write_text(json.dumps({"The Book": "A könyv", "Chapter One": "Első fejezet"}), encoding="utf-8")
    return wd


def test_chapters_skip_landmarks_and_read_footnotes_inline(book_wd):
    book = audio.chapters(book_wd, [])
    assert [c["file"] for c in book] == ["OEBPS/ch1.xhtml"]
    assert [s["text"] for s in book[0]["segments"]] == [
        "Chapter One",
        "First paragraph.",
        "Lábjegyzet: The note text.",
        "Second paragraph.",
    ]
    assert book[0]["segments"][0]["heading"]


def test_m4b_has_metadata_chapters_and_cover(book_wd):
    book = audio.chapters(book_wd, [])
    audio.add_pieces(book, {})
    pieces_dir = book_wd.root / "audio" / "pieces"
    chapters_dir = book_wd.root / "audio" / "chapters"
    pieces_dir.mkdir(parents=True)
    chapters_dir.mkdir()
    for piece in book[0]["pieces"]:
        audio.write_wav(pieces_dir / f"{audio.piece_key(piece['text'])}.wav", np.zeros(2400, dtype=np.int16))
    assert audio.build_chapter(book[0], pieces_dir, chapters_dir)
    assert not audio.build_chapter(book[0], pieces_dir, chapters_dir)  # same pieces: no rebuild

    info = audio.book_metadata(book_wd)
    assert info["titles"] == ["A könyv", "A Subtitle"]
    assert info["toc"] == {"OEBPS/ch1.xhtml": "Első fejezet"}
    assert info["cover"][0] == "OEBPS/cover.jpg"

    out = book_wd.root / "out.m4b"
    audio.build_m4b(book_wd, book, chapters_dir, out)
    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_chapters", "-show_format", "-show_streams", "-of", "json", str(out)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    )
    streams = {s["codec_type"]: s for s in probe["streams"]}  # plus a "data" track for the chapters
    assert streams["audio"]["codec_name"] == "aac"
    assert streams["video"]["disposition"]["attached_pic"] == 1
    assert probe["format"]["tags"]["title"] == "A könyv: A Subtitle"
    assert probe["format"]["tags"]["artist"] == "Ann Author"
    assert [c["tags"]["title"] for c in probe["chapters"]] == ["Első fejezet"]


def test_run_batches_retries_bad_takes_and_resumes(book_wd, monkeypatch):
    class FakeNarrator:
        """Each take's first sample identifies its text; the batch take of "Second paragraph." is garbled."""

        def __init__(self):
            self.batches, self.singles, self.takes = [], [], []

        def take(self, text, garbled):
            self.takes.append("valami más" if garbled else text)
            return np.full(240, len(self.takes) / 32767, dtype=np.float32)

        def speak_batch(self, texts, seed):
            self.batches.append(list(texts))
            return [self.take(t, t == "Second paragraph.") for t in texts]

        def speak(self, text, seed):
            self.singles.append(text)
            return self.take(text, False)

        def hear(self, path):
            return self.takes[int(audio.read_wav(path)[0]) - 1]

    fake = FakeNarrator()
    monkeypatch.setattr(audio, "get_narrator", lambda voice, log: fake)

    out = audio.run(book_wd, None, [], log=lambda m: None)
    assert out.exists()
    assert len(fake.batches) == 1 and len(fake.batches[0]) == 4
    assert fake.singles == ["Second paragraph."]
    assert not (book_wd.root / "audio" / "report.json").exists()

    fake.batches.clear()
    audio.run(book_wd, None, [], log=lambda m: None)  # everything exists: nothing is generated
    assert fake.batches == []


def test_run_with_nothing_translated_yet_does_nothing(book_wd):
    for chunk in book_wd.load_chunks():
        chunk["translation"] = None
        book_wd.save_chunk(chunk)
    assert audio.run(book_wd, None, [], log=lambda m: None) is None


def test_split_long_cuts_a_long_sentence_at_clauses_and_spaces():
    names = ", ".join(f"Valaki Hosszúnévnek{i}" for i in range(60)) + "."
    pieces = audio.split_long(names)
    assert all(len(p) <= audio.MAX_PIECE_CHARS for p in pieces)
    assert " ".join(pieces) == names
    no_marks = " ".join(["szó"] * 200)
    assert all(len(p) <= audio.MAX_PIECE_CHARS for p in audio.split_long(no_marks))


def test_speech_text_reads_urls_as_domains():
    text = audio.speech_text("Lásd http://onlineethics.org/Topics/Prof/shuttle_telecon.aspx [már nem elérhető].")
    assert text == "Lásd onlineethics.org [már nem elérhető]."
    assert audio.speech_text("Lásd whatisessential.org.") == "Lásd whatisessential.org."
