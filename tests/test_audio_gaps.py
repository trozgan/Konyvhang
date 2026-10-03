"""Edge cases of the audiobook module: models faked, ffmpeg real, tiny audio."""

import json
import subprocess
import sys
import types
import zipfile
from types import SimpleNamespace as NS

import numpy as np
import pytest

from konyvhang import audio, llm
from konyvhang.workdir import WorkDir

X = 'xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops"'
CONTAINER = (
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="OEBPS/content.opf"/></rootfiles></container>'
)


def opf(manifest: str, spine: str, meta: str = "") -> str:
    return f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
<dc:title>Book</dc:title><dc:creator>Ann</dc:creator>{meta}</metadata>
<manifest>{manifest}</manifest><spine>{spine}</spine></package>"""


def item(id_: str, href: str, media: str = "application/xhtml+xml", props: str = "") -> str:
    extra = f' properties="{props}"' if props else ""
    return f'<item id="{id_}" href="{href}" media-type="{media}"{extra}/>'


def make_book(tmp_path, files: dict[str, str | bytes], translate=None) -> WorkDir:
    """An EPUB from `files`, chunked, with translations echoing the source (or `translate(src)`)."""
    src = tmp_path / "b.epub"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER)
        for name, data in files.items():
            zf.writestr(name, data)
    wd = WorkDir(tmp_path / "work" / "b")
    wd.root.mkdir(parents=True)
    wd.source.write_bytes(src.read_bytes())
    wd.save_state({"profile": "fiction", "model": "opus", "source_name": "b.epub"})
    wd.write_chunks()
    for chunk in wd.load_chunks():
        chunk["translation"] = [translate(s["src"]) if translate else s["src"] for s in chunk["segments"]]
        wd.save_chunk(chunk)
    return wd


# chapters --------------------------------------------------------------------
def test_chapters_skip_marked_pages_and_handle_odd_notes(tmp_path):
    files = {
        "OEBPS/content.opf": opf(
            item("cv", "cover.xhtml") + item("ix", "ix.xhtml") + item("a", "ch.xhtml") + item("n", "note.xhtml"),
            '<itemref idref="cv"/><itemref idref="ix"/><itemref idref="a"/><itemref idref="n"/>',
        ),
        # no nav: landmarks cannot mark anything
        "OEBPS/cover.xhtml": f'<html {X}><body epub:type="cover"><p>Cover text here.</p></body></html>',
        "OEBPS/ix.xhtml": f'<html {X}><body><section epub:type="index"><p>Index words.</p></section></body></html>',
        "OEBPS/ch.xhtml": f"""<html {X}><body>
<p>Cites by file.<a epub:type="noteref" href="note.xhtml">[*]</a></p>
<p>Cites nothing real.<a epub:type="noteref" href="missing.xhtml#x">[*]</a></p>
<p>Becomes digits.</p></body></html>""",
        "OEBPS/note.xhtml": (
            f'<html {X}><body><aside epub:type="footnote" id="n1"><p>By file.</p></aside></body></html>'
        ),
    }
    wd = make_book(tmp_path, files, translate=lambda src: "123" if src == "Becomes digits." else src)

    book = audio.chapters(wd, [])

    assert [c["file"] for c in book] == ["OEBPS/ch.xhtml"]
    assert [s["text"] for s in book[0]["segments"]] == [
        "Cites by file.",
        "Lábjegyzet: By file.",
        "Cites nothing real.",
    ]


# numbers ---------------------------------------------------------------------
def test_spell_numbers_caches_good_batches_and_skips_bad_ones(tmp_path, monkeypatch):
    wd = WorkDir(tmp_path / "w")
    wd.root.mkdir()
    wd.save_state({"profile": "fiction", "model": "opus", "source_name": "x.epub"})
    answers = iter(['["egy"]', "nem json", '["kettő", "három"]', "[4]"])
    seen = []

    def fake_call(prompt, system, model):
        seen.append(model)
        answer = next(answers, None)
        if answer is None:
            raise llm.LLMError("elfogyott")
        return llm.Result(answer)

    monkeypatch.setattr(llm, "caller", lambda provider: fake_call)
    monkeypatch.setattr(audio, "SPEECH_BATCH", 1)
    logs = []
    texts = ["1 alma", "2 körte", "3 szilva", "4 dió", "5 mogyoró", "szöveg szám nélkül", "1 alma"]

    cache = audio.spell_numbers(wd, texts, logs.append)

    assert cache == {"1 alma": "egy"}
    assert seen == ["sonnet"] * 5  # claude's light model, one call per batch
    assert sum("nem sikerült" in m for m in logs) == 2  # bad JSON, then an error
    assert sum("elemszám eltér" in m for m in logs) == 2  # too many items, then a number instead of text
    assert json.loads((wd.root / "speech.json").read_text(encoding="utf-8")) == {"1 alma": "egy"}

    seen.clear()
    assert audio.spell_numbers(wd, ["1 alma"], logs.append) == {"1 alma": "egy"}
    assert seen == []  # served from speech.json


# models ----------------------------------------------------------------------
@pytest.fixture
def fake_mlx(monkeypatch):
    """mlx_audio stand-ins: TTS returns tiny arrays, STT hears a fixed text."""
    calls = {"tts": 0, "stt": 0}

    class TTS:
        def encode_reference_audio(self, path):
            calls["ref"] = path
            return "codes"

        def generate(self, **kw):
            calls["generate"] = kw
            yield NS(audio=[0.5, -0.5])

        def batch_generate(self, **kw):
            calls["batch"] = kw
            # out of order on purpose: speak_batch must sort by sequence_idx
            return [NS(sequence_idx=i, audio=[float(i)]) for i in reversed(range(len(kw["texts"])))]

    class STT:
        def generate(self, path, language):
            calls["heard"] = (path, language)
            return NS(text="  hallott  ")

    def load_tts(name):
        calls["tts"] += 1
        return TTS()

    def load_stt(name):
        calls["stt"] += 1
        return STT()

    for name in ("mlx_audio", "mlx_audio.stt"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    tts_mod = types.ModuleType("mlx_audio.tts")
    tts_mod.load = load_tts
    stt_mod = types.ModuleType("mlx_audio.stt.utils")
    stt_mod.load_model = load_stt
    monkeypatch.setitem(sys.modules, "mlx_audio.tts", tts_mod)
    monkeypatch.setitem(sys.modules, "mlx_audio.stt.utils", stt_mod)
    monkeypatch.setattr(audio, "_narrators", {})
    return calls


def test_narrator_wraps_the_models_and_is_loaded_once(tmp_path, fake_mlx):
    voice = tmp_path / "voice"
    voice.mkdir()
    (voice / "voice.txt").write_text(" Minta szöveg. \n", encoding="utf-8")
    logs = []

    narrator = audio.get_narrator(voice, logs.append)
    assert audio.get_narrator(voice, logs.append) is narrator
    assert logs == ["Modellek betöltése…"]
    assert fake_mlx["tts"] == fake_mlx["stt"] == 1
    assert narrator.ref_text == "Minta szöveg."
    assert fake_mlx["ref"] == str(voice / "voice.wav")

    np.testing.assert_array_equal(narrator.speak("rövid", 7), np.array([0.5, -0.5], dtype=np.float32))
    assert fake_mlx["generate"]["seed"] == 7
    assert fake_mlx["generate"]["max_new_tokens"] == 400  # the floor for short texts

    takes = narrator.speak_batch(["a", "b", "c"], 1)
    assert [float(t[0]) for t in takes] == [0.0, 1.0, 2.0]
    assert fake_mlx["batch"]["ref_audio_codes"] == "codes"

    assert narrator.hear(tmp_path / "x.wav") == "hallott"
    assert fake_mlx["heard"] == (str(tmp_path / "x.wav"), "hu")


def test_frame_limit_grows_with_the_longest_text():
    assert audio.frame_limit(["x" * 1000, "y"]) == 3000


class ScriptedNarrator:
    """Takes carry an id in their first sample; `heard` maps a take id to what Whisper hears."""

    def __init__(self, heard_for):
        self.heard_for = heard_for  # (text, attempt) -> heard text
        self.takes: list[tuple[str, int]] = []
        self.attempts: dict[str, int] = {}

    def _take(self, text):
        attempt = self.attempts.get(text, 0)
        self.attempts[text] = attempt + 1
        self.takes.append((text, attempt))
        return np.full(240, len(self.takes) / 32767, dtype=np.float32)

    def speak_batch(self, texts, seed):
        return [self._take(t) for t in texts]

    def speak(self, text, seed):
        return self._take(text)

    def hear(self, path):
        text, attempt = self.takes[int(audio.read_wav(path)[0]) - 1]
        return self.heard_for(text, attempt)


def test_generate_piece_keeps_the_best_of_all_failed_takes(tmp_path):
    text = "Ebből a nézőpontból minden ugyanolyan észszerű."
    heard = {0: "Ebből a nézőpontból", 1: "Ebből", 2: "semmi"}  # attempts after the batch take: 1, 2, 3
    narrator = ScriptedNarrator(lambda t, attempt: heard.get(attempt - 1, ""))

    score, _, best_heard = audio.generate_piece(narrator, text, tmp_path / "check.wav")

    assert narrator.attempts[text] == audio.ATTEMPTS  # never good enough: every attempt used
    assert best_heard == "Ebből a nézőpontból"
    assert score < audio.MIN_SIMILARITY


# the whole run ------------------------------------------------------------------
def two_chapter_book(tmp_path) -> WorkDir:
    files = {
        "OEBPS/content.opf": opf(
            item("a", "one.xhtml") + item("b", "two.xhtml"), '<itemref idref="a"/><itemref idref="b"/>'
        ),
        "OEBPS/one.xhtml": f"<html {X}><body><p>Az első fejezet szövege.</p></body></html>",
        "OEBPS/two.xhtml": f"<html {X}><body><p>A második fejezet szövege.</p><p>Ezt rosszul hallja.</p></body></html>",
    }
    return make_book(tmp_path, files)


def test_run_reports_bad_pieces_builds_chapters_window_by_window_and_rebuilds_lost_ones(tmp_path, monkeypatch):
    wd = two_chapter_book(tmp_path)
    narrator = ScriptedNarrator(lambda text, attempt: "valami egészen más" if text.startswith("Ezt") else text)
    monkeypatch.setattr(audio, "get_narrator", lambda voice, log: narrator)
    monkeypatch.setattr(audio, "WINDOW", 1)  # one piece per window: chapter two is unfinished after window one
    monkeypatch.setattr(audio, "BATCH_SIZE", 1)
    logs = []

    out = audio.run(wd, None, [], log=logs.append)

    assert out is not None and out.exists()
    report = json.loads((wd.root / "audio" / "report.json").read_text(encoding="utf-8"))
    assert [r["text"] for r in report.values()] == ["Ezt rosszul hallja."]
    assert any("1 hangdarab gyanús" in m for m in logs)
    assert sum(m.startswith("Fejezet kész") for m in logs) == 2

    # A lost chapter file is rebuilt from the saved pieces without generating anything.
    (wd.root / "audio" / "chapters" / "OEBPS_one.m4a").unlink()
    narrator.takes.clear()
    logs.clear()
    audio.run(wd, None, [], log=logs.append)
    assert narrator.takes == []
    assert [m for m in logs if m.startswith("Fejezet kész")] == [
        f"Fejezet kész: {wd.root / 'audio' / 'chapters' / 'OEBPS_one.m4a'}  (Az első fejezet szövege.)"
    ]


# metadata -----------------------------------------------------------------------
def png_bytes(tmp_path) -> bytes:
    path = tmp_path / "c.png"
    subprocess.run(
        ["ffmpeg", "-loglevel", "error", "-f", "lavfi", "-i", "color=c=blue:s=8x8", "-frames:v", "1", str(path)],
        check=True,
    )
    return path.read_bytes()


def test_book_metadata_finds_the_cover_through_meta_name(tmp_path):
    files = {
        "OEBPS/content.opf": opf(
            item("a", "ch.xhtml") + item("odd", "ch.xhtml", props="cover-image") + item("img", "c.png", "image/png"),
            '<itemref idref="a"/>',
            meta='<meta name="cover" content="img"/>',
        ),
        "OEBPS/ch.xhtml": f"<html {X}><body><p>Szöveg.</p></body></html>",
        "OEBPS/c.png": png_bytes(tmp_path),
    }
    info = audio.book_metadata(make_book(tmp_path, files))
    assert info["cover"][0] == "OEBPS/c.png"  # the non-image cover-image item is passed over
    assert info["toc"] == {}  # no nav


def test_book_without_cover_or_nav_still_makes_an_m4b(tmp_path, monkeypatch):
    files = {
        "OEBPS/content.opf": opf(
            item("a", "ch.xhtml") + item("odd", "ch.xhtml", props="cover-image"), '<itemref idref="a"/>'
        ),
        "OEBPS/ch.xhtml": f"<html {X}><body><p>Rövid szöveg.</p></body></html>",
    }
    wd = make_book(tmp_path, files)
    assert audio.book_metadata(wd)["cover"] is None
    narrator = ScriptedNarrator(lambda text, attempt: text)
    monkeypatch.setattr(audio, "get_narrator", lambda voice, log: narrator)

    out = audio.run(wd, None, [], log=lambda m: None)

    probe = json.loads(
        subprocess.run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_chapters", "-of", "json", str(out)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            check=True,
        ).stdout
    )
    assert "video" not in {s["codec_type"] for s in probe["streams"]}
    assert [c["tags"]["title"] for c in probe["chapters"]] == ["Rövid szöveg."]


# file names ------------------------------------------------------------------------
def test_hrefs_are_percent_decoded_and_chapters_in_different_folders_get_different_files(tmp_path):
    assert audio.resolve("OEBPS", "text/ch%201.xhtml#n1") == ("OEBPS/text/ch 1.xhtml", "n1")
    one = audio.chapter_path(tmp_path, {"file": "OEBPS/part1/chapter.xhtml"}, ".m4a")
    two = audio.chapter_path(tmp_path, {"file": "OEBPS/part2/chapter.xhtml"}, ".m4a")
    assert one != two
    assert one.name == "OEBPS_part1_chapter.m4a"


def test_m4b_in_a_folder_with_a_quote_in_its_name(tmp_path, monkeypatch):
    (tmp_path / "O'Brien").mkdir()  # ffmpeg's concat list quotes paths with '
    wd = make_book(
        tmp_path / "O'Brien",
        {
            "OEBPS/content.opf": opf(item("a", "ch.xhtml"), '<itemref idref="a"/>'),
            "OEBPS/ch.xhtml": f"<html {X}><body><p>Rövid szöveg.</p></body></html>",
        },
    )
    monkeypatch.setattr(audio, "get_narrator", lambda voice, log: ScriptedNarrator(lambda text, attempt: text))
    out = audio.run(wd, None, [], log=lambda m: None)
    assert out is not None and out.exists()
