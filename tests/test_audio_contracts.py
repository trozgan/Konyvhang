"""Audiobook contracts the other tests execute but never pin: models faked, ffmpeg real, tiny audio."""

import json
import subprocess
import wave
import zipfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast

import numpy as np
import pytest
from test_audio_gaps import ScriptedNarrator, X, item, two_chapter_book

from konyvhang import audio, llm, workdir
from konyvhang.workdir import WorkDir


def opf(manifest: str, spine: str, meta: str = "") -> str:
    return f"""<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0"><metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
{meta}</metadata><manifest>{manifest}</manifest><spine>{spine}</spine></package>"""


def build_book(
    tmp_path: Path,
    opf_path: str,
    files: Mapping[str, str],
    translated: Callable[[int], bool] = lambda i: True,
    translate: Callable[[str], str] = lambda src: src,
) -> WorkDir:
    """An EPUB whose package document is at `opf_path`, chunked; `translated` chunks get `translate(src)`."""
    src = tmp_path / "b.epub"
    with zipfile.ZipFile(src, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr(
            "META-INF/container.xml",
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
            f'<rootfiles><rootfile full-path="{opf_path}"/></rootfiles></container>',
        )
        for name, data in files.items():
            zf.writestr(name, data)
    wd = WorkDir(tmp_path / "work" / "b")
    wd.root.mkdir(parents=True)
    wd.source.write_bytes(src.read_bytes())
    wd.save_state({"profile": "fiction", "model": "opus", "source_name": "b.epub"})
    wd.write_chunks()
    for i, chunk in enumerate(wd.load_chunks()):
        if translated(i):
            chunk["translation"] = [translate(s["src"]) for s in chunk["segments"]]
            wd.save_chunk(chunk)
    return wd


def html(body: str) -> str:
    return f"<html {X}><body>{body}</body></html>"


# chapters --------------------------------------------------------------------------
def test_a_partly_translated_book_reads_the_chapters_before_the_first_gap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(workdir, "MAX_WORDS", 3)  # every two-word chapter becomes its own chunk
    names = ["one", "two", "three"]
    wd = build_book(
        tmp_path,
        "OEBPS/content.opf",
        {
            "OEBPS/content.opf": opf(
                "".join(item(n, f"{n}.xhtml") for n in names), "".join(f'<itemref idref="{n}"/>' for n in names)
            ),
            "OEBPS/one.xhtml": html("<p>Első fejezet.</p>"),
            "OEBPS/two.xhtml": html("<p>Második fejezet.</p>"),
            "OEBPS/three.xhtml": html("<p>Harmadik fejezet.</p>"),
        },
        translated=lambda i: i != 1,  # the middle chunk is still being translated
    )
    assert len(wd.chunk_paths()) == 3

    book = audio.chapters(wd, [])

    assert [c["file"] for c in book] == ["OEBPS/one.xhtml"]  # chapter three waits: reading order


LONG_HEADING = "Az első fejezet, amelynek a címe szándékosan hosszabb nyolcvan karakternél, hogy levágják"


def notes_book(tmp_path: Path) -> WorkDir:
    """Package document at the ZIP root, chapters in nested folders, two notes in one notes file."""
    spine = ["ch", "notes", "two", "legal", "skipme"]
    hrefs = {
        "ch": "ch.xhtml",
        "notes": "text/notes.xhtml",
        "two": "text/sub/two.xhtml",
        "legal": "text/legal.xhtml",
        "skipme": "text/skipme.xhtml",
    }
    return build_book(
        tmp_path,
        "content.opf",
        {
            "content.opf": opf(
                "".join(item(k, hrefs[k]) for k in spine), "".join(f'<itemref idref="{k}"/>' for k in spine)
            ),
            "ch.xhtml": html(
                f"<h1>{LONG_HEADING}</h1>"
                '<p>Két jegyzet.<a epub:type="noteref" href="text/notes.xhtml#n2">2</a>'
                '<a epub:type="noteref" href="text/notes.xhtml#n1">1</a></p>'
                '<p>Lásd <a href="text/notes.xhtml">a jegyzeteket</a> később.</p>'
                "<p>Ezerkilencszáznyolcvannégy.</p>"
                "<p>Utána még egy bekezdés.</p>"
            ),
            "text/notes.xhtml": html(
                '<aside epub:type="footnote" id="n1"><p>Első jegyzet eleje.</p><p>Első jegyzet vége.</p></aside>'
                '<aside epub:type="footnote" id="n2"><p>Második jegyzet.</p></aside>'
            ),
            "text/sub/two.xhtml": html('<p>Második fejezet.<a epub:type="noteref" href="../notes.xhtml#n2">*</a></p>'),
            "text/legal.xhtml": html('<section epub:type="copyright-page"><p>Minden jog fenntartva.</p></section>'),
            "text/skipme.xhtml": html("<p>Kihagyandó rész.</p>"),
        },
        translate=lambda src: "1984." if src == "Ezerkilencszáznyolcvannégy." else src,
    )


def test_footnotes_are_read_by_id_after_the_citing_paragraph_and_ordinary_links_stay_text(tmp_path: Path) -> None:
    book = audio.chapters(notes_book(tmp_path), ["skipme"])

    # the notes file, an in-body copyright section and the --skip pattern are not chapters
    assert [c["file"] for c in book] == ["ch.xhtml", "text/sub/two.xhtml"]
    assert len(LONG_HEADING) > 80
    assert book[0]["title"] == LONG_HEADING[:80]
    assert book[0]["segments"] == [
        {"text": LONG_HEADING, "heading": True},
        {"text": "Két jegyzet.", "heading": False},
        {"text": "Lábjegyzet: Második jegyzet.", "heading": False},  # each reference finds its own note
        {"text": "Lábjegyzet: Első jegyzet eleje. Első jegyzet vége.", "heading": False},
        {"text": "Lásd a jegyzeteket később.", "heading": False},  # a plain link is no note reference
        # a translation without letters ("1984.") is silent, but the chapter goes on after it
        {"text": "Utána még egy bekezdés.", "heading": False},
    ]
    assert book[1]["segments"] == [
        {"text": "Második fejezet.", "heading": False},
        {"text": "Lábjegyzet: Második jegyzet.", "heading": False},  # ../ resolved from text/sub/
    ]


def test_skip_patterns_leave_out_only_the_matching_files(tmp_path: Path) -> None:
    book = audio.chapters(notes_book(tmp_path), ["sub/"])
    assert [c["file"] for c in book] == ["ch.xhtml", "text/skipme.xhtml"]


def test_speech_text_keeps_links_with_words_and_drops_number_links() -> None:
    assert audio.speech_text('see <a n="1">the map</a><a n="2">12</a>') == "see the map"


# resume keys and seeds ------------------------------------------------------------
def test_piece_key_is_stable_because_finished_audio_is_found_by_it() -> None:
    assert audio.piece_key("abc") == "a9993e364706816a"  # SHA-1 prefix; changing it regenerates every book


def test_piece_seed_is_stable_and_differs_per_attempt() -> None:
    assert audio.piece_seed("abc", 0) == 0xA9993E36
    assert audio.piece_seed("abc", 2) == 0xA9993E36 + 2


class SeedRecorder(ScriptedNarrator):
    def __init__(self, heard_for: Callable[[str, int], str]) -> None:
        super().__init__(heard_for)
        self.seeds: list[tuple[str, int]] = []

    def speak_batch(self, texts: list[str], seed: int) -> list[np.ndarray]:
        self.seeds.append(("batch", seed))
        return super().speak_batch(texts, seed)

    def speak(self, text: str, seed: int) -> np.ndarray:
        self.seeds.append(("single", seed))
        return super().speak(text, seed)


def test_every_retry_uses_its_own_seed_and_ties_keep_the_first_take(tmp_path: Path) -> None:
    text = "Egy mondat, amit sosem hall jól."
    narrator = SeedRecorder(lambda t, attempt: "más")

    _, pcm, _ = audio.generate_piece(cast(audio.Narrator, narrator), text, tmp_path / "check.wav")

    assert narrator.seeds == [("single", audio.piece_seed(text, a)) for a in range(1, audio.ATTEMPTS + 1)]
    assert int(pcm[0]) == 1  # all takes score the same: the first one stays


# similarity of 2 * 17 / (23 + 17) is exactly the threshold
THRESHOLD_TEXT, THRESHOLD_HEARD = "x" * 23, "x" * 17


def test_a_take_exactly_at_the_threshold_is_accepted(tmp_path: Path) -> None:
    assert audio.similarity(THRESHOLD_TEXT, THRESHOLD_HEARD) == audio.MIN_SIMILARITY
    narrator = ScriptedNarrator(lambda t, attempt: THRESHOLD_HEARD)

    score, _, _ = audio.generate_piece(cast(audio.Narrator, narrator), THRESHOLD_TEXT, tmp_path / "check.wav")

    assert score == audio.MIN_SIMILARITY
    assert narrator.attempts == {THRESHOLD_TEXT: 1}


def test_run_seeds_batches_builds_finished_chapters_early_and_reports_bad_pieces(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wd = two_chapter_book(tmp_path)
    narrator = SeedRecorder(lambda text, attempt: "valami egészen más" if text.startswith("Ezt") else text)
    voices: list[Path] = []

    def get_narrator(voice: Path, log: Callable[[str], None]) -> SeedRecorder:
        voices.append(voice)
        return narrator

    monkeypatch.setattr(audio, "get_narrator", get_narrator)
    monkeypatch.setattr(audio, "WINDOW", 1)
    monkeypatch.setattr(audio, "BATCH_SIZE", 1)
    logs: list[str] = []

    audio.run(wd, Path("hang"), [], log=logs.append)

    assert set(voices) == {Path("hang")}
    first = "Az első fejezet szövege."
    assert narrator.seeds[0] == ("batch", audio.piece_seed(first, 0))
    # chapter one is joined as soon as its window is done, before chapter two is read
    assert logs.index(next(m for m in logs if m.startswith("Fejezet kész"))) < logs.index(
        next(m for m in logs if m.startswith("2/3 hangdarab"))
    )
    bad = "Ezt rosszul hallja."
    heard = "valami egészen más"
    report = json.loads((wd.root / "audio" / "report.json").read_text(encoding="utf-8"))
    assert report == {
        audio.piece_key(bad): {"text": bad, "heard": heard, "score": round(audio.similarity(bad, heard), 3)}
    }


def test_run_neither_retries_nor_reports_a_batch_take_at_the_threshold(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wd = build_book(
        tmp_path,
        "OEBPS/content.opf",
        {
            "OEBPS/content.opf": opf(item("a", "ch.xhtml"), '<itemref idref="a"/>'),
            "OEBPS/ch.xhtml": html(f"<p>{THRESHOLD_TEXT}</p>"),
        },
    )
    narrator = SeedRecorder(lambda t, attempt: THRESHOLD_HEARD)
    monkeypatch.setattr(audio, "get_narrator", lambda voice, log: narrator)

    audio.run(wd, Path("voice"), [], log=lambda m: None)

    assert [kind for kind, _ in narrator.seeds] == ["batch"]
    assert not (wd.root / "audio" / "report.json").exists()


# numbers ---------------------------------------------------------------------------
def test_spell_numbers_sends_batches_of_sixty_with_the_speech_prompt_and_the_book_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wd = WorkDir(tmp_path / "w")
    wd.root.mkdir()
    wd.save_state({"profile": "fiction", "provider": "openai", "model": "gpt-x", "source_name": "x.epub"})
    calls: list[tuple[str, str, str | None]] = []
    providers: list[str] = []

    def fake_call(prompt: str, system: str, model: str | None) -> llm.Result:
        calls.append((prompt, system, model))
        return llm.Result(json.dumps([f"kimondva {t}" for t in json.loads(prompt)], ensure_ascii=False))

    def fake_caller(provider: str) -> llm.Caller:
        providers.append(provider)
        return fake_call

    monkeypatch.setattr(llm, "caller", fake_caller)
    texts = [f"{i}. tétel, hosszú ő" for i in range(61)]
    logs: list[str] = []

    cache = audio.spell_numbers(wd, texts, logs.append)

    assert providers == ["openai"]
    system = (Path(audio.__file__).parent / "prompts" / "speech.md").read_text(encoding="utf-8")
    assert [(json.loads(p), s, m) for p, s, m in calls] == [
        (texts[:60], system, "gpt-x"),
        (texts[60:], system, "gpt-x"),
    ]
    assert "hosszú ő" in calls[0][0]  # Hungarian letters go to the model as they are
    assert cache == {t: f"kimondva {t}" for t in texts}
    assert [m for m in logs if m.startswith("Számok")] == [
        "Számok kiírása betűvel: 60/61 bekezdés",
        "Számok kiírása betűvel: 61/61 bekezdés",
    ]


def test_spell_numbers_logs_why_a_batch_failed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wd = WorkDir(tmp_path / "w")
    wd.root.mkdir()
    wd.save_state({"profile": "fiction", "model": "opus", "source_name": "x.epub"})

    def failing(prompt: str, system: str, model: str | None) -> llm.Result:
        raise llm.LLMError("a szolgáltató nem válaszolt")

    monkeypatch.setattr(llm, "caller", lambda provider: failing)
    logs: list[str] = []

    assert audio.spell_numbers(wd, ["1 alma"], logs.append) == {}
    assert any("a szolgáltató nem válaszolt" in m for m in logs)


# audio files -------------------------------------------------------------------------
def test_wav_files_are_mono_16_bit_at_the_model_rate(tmp_path: Path) -> None:
    pcm = np.array([0, 1000, -1000], dtype=np.int16)
    audio.write_wav(tmp_path / "x.wav", pcm)
    with wave.open(str(tmp_path / "x.wav"), "rb") as w:
        assert (w.getnchannels(), w.getsampwidth(), w.getframerate(), w.getnframes()) == (1, 2, audio.SAMPLE_RATE, 3)
    np.testing.assert_array_equal(audio.read_wav(tmp_path / "x.wav"), pcm)


def test_silence_has_one_int16_sample_per_tick() -> None:
    pause = audio.silence(0.6)
    assert pause.dtype == np.int16
    assert len(pause) == 14400


def test_to_pcm_clips_to_the_int16_range() -> None:
    pcm = audio.to_pcm(np.array([2.0, -2.0, 0.5], dtype=np.float32))
    assert pcm.dtype == np.int16
    assert pcm.tolist() == [32767, -32767, 16383]


def test_ffmeta_escape_escapes_the_ffmetadata_specials() -> None:
    assert audio.ffmeta_escape("a=b;c#d\\e\nf") == "a\\=b\\;c\\#d\\\\e\\\nf"


def probe(path: Path, *args: str) -> dict[str, Any]:
    out = subprocess.run(
        ["ffprobe", "-v", "error", *args, "-of", "json", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    result: dict[str, Any] = json.loads(out)
    return result


def test_m4b_chapter_marks_follow_the_chapter_lengths(tmp_path: Path) -> None:
    wd = build_book(
        tmp_path,
        "OEBPS/content.opf",
        {
            "OEBPS/content.opf": opf(
                item("a", "one.xhtml") + item("b", "two.xhtml"),
                '<itemref idref="a"/><itemref idref="b"/>',
                meta="<dc:title>Cím</dc:title><dc:title>Alcím</dc:title><dc:title>Harmadik</dc:title>"
                "<dc:creator>Ann</dc:creator><dc:creator>Bob</dc:creator>",
            ),
            "OEBPS/one.xhtml": html("<p>Egy.</p><p>Kettő.</p>"),
            "OEBPS/two.xhtml": html("<p>Három.</p>"),
        },
    )
    book = audio.chapters(wd, [])
    audio.add_pieces(book, {})
    pieces_dir, chapters_dir = tmp_path / "pieces", tmp_path / "chapters"
    pieces_dir.mkdir()
    chapters_dir.mkdir()
    for chapter in book:
        for piece in chapter["pieces"]:
            audio.write_wav(pieces_dir / f"{audio.piece_key(piece['text'])}.wav", np.zeros(2400, dtype=np.int16))
        assert audio.build_chapter(chapter, pieces_dir, chapters_dir)

    # every piece (0.1 s) and its pause (0.6 s) are joined, then the chapter pause (2 s)
    one, two = (chapters_dir / "OEBPS_one.m4a", chapters_dir / "OEBPS_two.m4a")
    assert audio.duration_ms(one) == pytest.approx(3400, abs=100)
    assert audio.duration_ms(two) == pytest.approx(2700, abs=100)
    keys = [audio.piece_key(t) for t in ("Egy.", "Kettő.")]
    assert json.loads((chapters_dir / "OEBPS_one.json").read_text(encoding="utf-8")) == {"keys": keys}

    out = tmp_path / "book.m4b"
    audio.build_m4b(wd, book, chapters_dir, out)

    exact = [
        round(float(probe(m4a, "-show_entries", "format=duration")["format"]["duration"]) * 1000) for m4a in (one, two)
    ]
    info = probe(out, "-show_chapters", "-show_format")
    marks = [(c["time_base"], c["start"], c["end"]) for c in info["chapters"]]
    assert marks == [("1/1000", 0, exact[0]), ("1/1000", exact[0], exact[0] + exact[1])]
    tags = info["format"]["tags"]
    assert (tags["title"], tags["artist"], tags["genre"]) == ("Cím: Alcím", "Ann, Bob", "Audiobook")


# follow ------------------------------------------------------------------------------
class FakeWorkDir:
    def __init__(self, states: list[list[int]]) -> None:
        self.states = iter(states)
        self.current = next(self.states)

    def load_chunks(self) -> list[dict[str, Any]]:
        return [{"translation": [] if t else None} for t in self.current]

    def chunk_paths(self) -> list[int]:
        return self.current


def test_follow_passes_its_arguments_to_every_pass_and_sleeps_for_poll(monkeypatch: pytest.MonkeyPatch) -> None:
    wd = FakeWorkDir([[1, 0], [1, 1]])
    passes: list[tuple[object, ...]] = []
    sleeps: list[float] = []

    def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        wd.current = next(wd.states)

    monkeypatch.setattr(audio, "run", lambda *args: passes.append(args))
    monkeypatch.setattr("konyvhang.audio.time.sleep", fake_sleep)
    voice, skip, logs = Path("hang"), ["toc"], cast(list[str], [])

    audio.follow(cast(WorkDir, wd), voice, skip, poll=7, log=logs.append)

    assert passes == [(wd, voice, skip, logs.append)] * 2
    assert sleeps == [7]
    assert logs == ["Lefordítva 1/2 darab; várok a következőre…", "A teljes könyv hangja elkészült."]
