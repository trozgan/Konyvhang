"""Audiobook from the translated chunks with Higgs TTS 3 (MLX), checked by Whisper.

Each spine file is a chapter; footnotes are read after the paragraph that cites them.
Long paragraphs are split at sentence ends. Every piece is saved as soon as it passes
the check, so an interrupted run resumes. Finished chapters become .m4a files at once;
the full book becomes one .m4b with chapter marks.
"""

import difflib
import hashlib
import json
import re
import subprocess
import time
import wave
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

import numpy as np

from . import epub, segment
from .workdir import WorkDir, write_json

TTS_MODEL = "bosonai/higgs-audio-v3-tts-4b"
STT_MODEL = "mlx-community/whisper-large-v3-turbo-asr-fp16"
SAMPLE_RATE = 24000
TEMPERATURE = 0.8  # model card recommendation for voice cloning
TOP_K = 50
MAX_PIECE_CHARS = 350
MIN_SIMILARITY = 0.85
ATTEMPTS = 3
BATCH_SIZE = 8  # measured on M4 Pro: RTF 0.91 one by one, 0.23 at 8, 0.22 at 16
WINDOW = 32
PAUSE = {"paragraph": 0.6, "heading": 1.2, "chapter": 2.0}
HEADING_TAGS = {"h1", "h2", "h3", "h4", "h5", "h6"}
SENTENCE_END = re.compile(r"(?<=[.!?…])\s+")
CLAUSE_END = re.compile(r"(?<=[;:,–])\s+")
SPACE = re.compile(r"\s+")
URL = re.compile(r"(?:https?://)?(?:www\.)?((?:[\w-]+\.)+(?:com|org|net|edu|gov|io|hu|uk))(?:/\S*)?", re.I)


# text --------------------------------------------------------------------
def speech_text(markup: str) -> str:
    """Plain text to read aloud: no footnote markers, line breaks become spaces."""
    markup = re.sub(r'<br n="\d+"/>', " ", markup)
    markup = re.sub(r'<sup n="\d+">.*?</sup>', "", markup)
    markup = re.sub(  # links without letters: note references such as [*] or 12
        r'<a n="\d+">([^<]*)</a>', lambda m: m.group(0) if segment.LETTER.search(m.group(1)) else "", markup
    )
    return URL.sub(r"\1", segment.plain_text(markup))  # a web address is read as its domain only


def split_long(text: str, patterns: tuple[re.Pattern[str], ...] = ()) -> list[str]:
    """Pieces of at most MAX_PIECE_CHARS: cut at sentence ends, then clause marks, then spaces.

    The model tends to drop the end of very long inputs, so no piece may stay long.
    """
    patterns = patterns or (SENTENCE_END, CLAUSE_END, SPACE)
    if len(text) <= MAX_PIECE_CHARS or not patterns:
        return [text]
    pieces, current = [], ""
    for part in patterns[0].split(text):
        if current and len(current) + 1 + len(part) > MAX_PIECE_CHARS:
            pieces.append(current)
            current = part
        else:
            current = f"{current} {part}".strip()
    pieces.append(current)
    return [q for p in pieces for q in split_long(p, patterns[1:])]


SKIP_TYPES = {"cover", "titlepage", "toc", "copyright-page", "index"}  # not read aloud
NOTE_TYPES = {"footnote", "endnote", "rearnote"}


def landmark_files(zf: zipfile.ZipFile, book: epub.Book) -> set[str]:
    """Spine files the publisher marks in the nav landmarks as cover, title page, TOC, copyright."""
    if not book.nav_path:
        return set()
    nav = epub.parse_xml(zf.read(book.nav_path))
    base = book.nav_path.rsplit("/", 1)[0] if "/" in book.nav_path else ""
    out = set()
    for a in nav.iterfind(".//{http://www.w3.org/1999/xhtml}a"):
        if segment.semantics(a) & SKIP_TYPES and a.get("href"):
            out.add(resolve(base, cast(str, a.get("href")))[0])
    return out


def resolve(base: str, href: str) -> tuple[str, str]:
    """(file, fragment) of an href relative to the directory `base`."""
    import posixpath

    path, _, frag = href.partition("#")
    return (posixpath.normpath(posixpath.join(base, path)) if path else "", frag)


NoteKey = tuple[str, str]  # (file, element id) of a footnote


@dataclass
class SegmentInfo:
    heading: bool
    note: NoteKey | None  # the footnote this segment belongs to
    refs: list[NoteKey]  # footnotes this segment cites


def chapters(wd: WorkDir, skip: list[str]) -> list[dict[str, Any]]:
    """Translated text grouped by spine file: [{"file", "title", "segments": [{"text", "heading"}]}].

    Pages the publisher marks as cover, title page, TOC or copyright are left out.
    Footnotes are read right after the paragraph that refers to them, not as chapters.
    """
    info: dict[tuple[str, int], SegmentInfo] = {}
    note_of_file: dict[str, NoteKey] = {}  # footnote file -> its note, for references without a fragment
    with zipfile.ZipFile(wd.source) as zf:
        book = epub.read_book(zf)
        skipped = landmark_files(zf, book)
        for path in book.spine:
            tree = epub.parse_xml(zf.read(path))
            base = path.rsplit("/", 1)[0] if "/" in path else ""
            body = next(el for el in tree.iter() if segment.local(el) == "body")
            if segment.semantics(body) & SKIP_TYPES:
                skipped.add(path)
            for el in body.iter():
                if isinstance(el.tag, str) and el is not body and segment.semantics(el) & SKIP_TYPES:
                    skipped.add(path)
            for idx, el in enumerate(segment.find_segments(tree)):
                note = next(
                    ((path, a.get("id") or "") for a in [el, *el.iterancestors()] if segment.semantics(a) & NOTE_TYPES),
                    None,
                )
                if note:
                    note_of_file.setdefault(path, note)
                refs = []
                for a in el.iter("{http://www.w3.org/1999/xhtml}a"):
                    if "noteref" in segment.semantics(a) and a.get("href"):
                        file, frag = resolve(base, cast(str, a.get("href")))
                        refs.append((file or path, frag))
                info[(path, idx)] = SegmentInfo(segment.local(el) in HEADING_TAGS, note, refs)

    texts = {}
    for chunk in wd.load_chunks():
        if chunk["translation"] is None:
            break  # stop at the first gap so chapters stay in reading order
        for seg, markup in zip(chunk["segments"], chunk["translation"], strict=True):
            texts[(seg["file"], seg["idx"])] = speech_text(markup)

    notes: dict[NoteKey, list[str]] = {}
    for key, text in texts.items():
        note = info[key].note
        if note:
            notes.setdefault(note, []).append(text)

    out: list[dict[str, Any]] = []
    for (file, idx), text in texts.items():
        meta = info[(file, idx)]
        if meta.note or file in skipped or any(s in file for s in skip):
            continue
        if not segment.LETTER.search(text):
            continue
        if not out or out[-1]["file"] != file:
            out.append({"file": file, "title": text[:80], "segments": []})
        out[-1]["segments"].append({"text": text, "heading": meta.heading})
        for ref in meta.refs:
            fallback = note_of_file.get(ref[0])
            note_texts = notes.get(ref) or (notes.get(fallback) if fallback else None)
            if note_texts:
                note_text = " ".join(t for t in note_texts if segment.LETTER.search(t))
                out[-1]["segments"].append({"text": f"Lábjegyzet: {note_text}", "heading": False})
    return out


def add_pieces(book: list[dict[str, Any]], spoken: dict[str, str]) -> None:
    """Split each segment (with numbers spelled out) into pieces with the pause after them."""
    for chapter in book:
        chapter["pieces"] = []
        for seg in chapter["segments"]:
            parts = split_long(spoken.get(seg["text"], seg["text"]))
            for i, part in enumerate(parts):
                last = i == len(parts) - 1
                pause = PAUSE["heading"] if seg["heading"] else PAUSE["paragraph"] if last else 0.25
                chapter["pieces"].append({"text": part, "pause": pause})


# numbers -------------------------------------------------------------------
SPEECH_BATCH = 60
DIGIT = re.compile(r"\d")


def spell_numbers(wd: WorkDir, texts: list[str], log: Callable[[str], None]) -> dict[str, str]:
    """Spoken forms for texts with digits, made once by Claude and cached in speech.json."""
    from . import llm

    path = wd.root / "speech.json"
    cache = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    todo = list(dict.fromkeys(t for t in texts if DIGIT.search(t) and t not in cache))
    system = (Path(__file__).parent / "prompts" / "speech.md").read_text(encoding="utf-8")
    state = wd.load_state()
    provider = state.get("provider", "claude")
    call = llm.caller(provider)
    model = llm.LIGHT_MODEL.get(provider) or state["model"]
    for i in range(0, len(todo), SPEECH_BATCH):
        batch = todo[i : i + SPEECH_BATCH]
        log(f"Számok kiírása betűvel: {i + len(batch)}/{len(todo)} bekezdés")
        try:
            result = call(json.dumps(batch, ensure_ascii=False), system, model)
            text = result.text
            spoken = llm.parse_json(text, "[")
        except (ValueError, llm.LLMError) as e:
            log(f"  nem sikerült, ezek a bekezdések számjegyekkel maradnak: {str(e)[:200]}")
            continue
        if len(spoken) != len(batch):
            log("  az elemszám eltér, ezek a bekezdések számjegyekkel maradnak")
            continue
        cache.update(zip(batch, spoken, strict=True))
        write_json(path, cache)
    return cache


# checks --------------------------------------------------------------------
def normalize(text: str) -> str:
    text = re.sub(r"\d+", " ", text.lower())
    return " ".join(re.findall(r"[^\W\d_]+", text))


def similarity(expected: str, heard: str) -> float:
    # autojunk would drop frequent characters in texts over 200 chars and wreck the ratio
    return difflib.SequenceMatcher(None, normalize(expected), normalize(heard), autojunk=False).ratio()


# audio files ---------------------------------------------------------------
def write_wav(path: Path, pcm: np.ndarray) -> None:
    tmp = path.with_suffix(".tmp")
    with wave.open(str(tmp), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(pcm.astype(np.int16).tobytes())
    tmp.replace(path)


def to_pcm(audio: np.ndarray) -> np.ndarray:
    pcm: np.ndarray = (np.clip(audio, -1.0, 1.0) * 32767).astype(np.int16)
    return pcm


def read_wav(path: Path) -> np.ndarray:
    with wave.open(str(path), "rb") as w:
        return np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.int16)


def to_aac(src: Path, dest: Path) -> None:
    tmp = dest.with_name(dest.stem + ".tmp" + dest.suffix)  # readers never see a half-written file
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", "-i", str(src), "-c:a", "aac", "-b:a", "64k", str(tmp)], check=True
    )
    tmp.replace(dest)


# generation ----------------------------------------------------------------
class Narrator:
    def __init__(self, voice: Path) -> None:
        from mlx_audio.stt.utils import load_model as load_stt
        from mlx_audio.tts import load as load_tts

        self.tts = load_tts(TTS_MODEL)
        self.stt = load_stt(STT_MODEL)
        self.ref_text = (voice / "voice.txt").read_text(encoding="utf-8").strip()
        self.ref_codes = self.tts.encode_reference_audio(str(voice / "voice.wav"))

    def speak(self, text: str, seed: int) -> np.ndarray:
        result = next(
            self.tts.generate(
                text=text,
                ref_audio_codes=self.ref_codes,
                ref_text=self.ref_text,
                temperature=TEMPERATURE,
                top_k=TOP_K,
                seed=seed,
                max_new_tokens=frame_limit([text]),
            )
        )
        return np.array(result.audio, dtype=np.float32)

    def speak_batch(self, texts: list[str], seed: int) -> list[np.ndarray]:
        """One pass on the GPU for several texts; about 4x faster than one by one at BATCH_SIZE 8."""
        results = self.tts.batch_generate(
            texts=texts,
            ref_audio_codes=self.ref_codes,
            ref_text=self.ref_text,
            temperature=TEMPERATURE,
            top_k=TOP_K,
            seed=seed,
            max_new_tokens=frame_limit(texts),
        )
        return [np.array(r.audio, dtype=np.float32) for r in sorted(results, key=lambda r: r.sequence_idx)]

    def hear(self, path: Path) -> str:
        text: str = self.stt.generate(str(path), language="hu").text.strip()
        return text


def frame_limit(texts: list[str]) -> int:
    """Generation cap: about 25 codec frames per second, generous speaking time per character."""
    return max(400, int(max(map(len, texts)) * 0.12 * 25))


_narrators: dict[Path, Narrator] = {}


def get_narrator(voice: Path, log: Callable[[str], None]) -> Narrator:
    """Load the models once per process; --follow reuses them across passes."""
    if voice not in _narrators:
        log("Modellek betöltése…")
        _narrators[voice] = Narrator(voice)
    return _narrators[voice]


def piece_key(text: str) -> str:
    return hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:16]


def piece_seed(text: str, attempt: int) -> int:
    return int(hashlib.sha1(text.encode(), usedforsecurity=False).hexdigest()[:8], 16) + attempt


def generate_piece(narrator: "Narrator", text: str, check: Path) -> tuple[float, np.ndarray, str]:
    """Best of up to ATTEMPTS takes alone (seeds 1.., the batch used 0), judged by Whisper."""
    best: tuple[float, np.ndarray, str] | None = None
    for attempt in range(1, ATTEMPTS + 1):
        audio = to_pcm(narrator.speak(text, piece_seed(text, attempt)))
        write_wav(check, audio)
        heard = narrator.hear(check)
        score = similarity(text, heard)
        if best is None or score > best[0]:
            best = (score, audio, heard)
        if score >= MIN_SIMILARITY:
            break
    assert best is not None  # noqa: S101 - narrows the type for mypy; ATTEMPTS >= 1
    return best


def run(
    wd: WorkDir, voice: Path, skip: list[str], log: Callable[[str], None] = lambda m: print(m, flush=True)
) -> Path | None:
    audio_dir = wd.root / "audio"
    pieces_dir = audio_dir / "pieces"
    chapters_dir = audio_dir / "chapters"
    pieces_dir.mkdir(parents=True, exist_ok=True)
    chapters_dir.mkdir(exist_ok=True)
    report_path = audio_dir / "report.json"
    report = json.loads(report_path.read_text(encoding="utf-8")) if report_path.exists() else {}

    book = chapters(wd, skip)
    if not book:
        log("Még nincs felolvasható lefordított rész.")
        return None
    add_pieces(book, spell_numbers(wd, [s["text"] for c in book for s in c["segments"]], log))
    missing = list(
        dict.fromkeys(
            p["text"] for c in book for p in c["pieces"] if not (pieces_dir / f"{piece_key(p['text'])}.wav").exists()
        )
    )
    total = sum(len(c["pieces"]) for c in book)
    log(f"{len(book)} fejezet, {total} hangdarab, ebből {len(missing)} hiányzik, hang: {voice}")
    started = time.perf_counter()
    check = pieces_dir / "_check.wav"

    # Windows keep reading order roughly; sorting inside one makes batch members similar in
    # length, because a batch takes as long as its longest text.
    for w in range(0, len(missing), WINDOW):
        window = sorted(missing[w : w + WINDOW], key=len)
        for b in range(0, len(window), BATCH_SIZE):
            batch = window[b : b + BATCH_SIZE]
            narrator = get_narrator(voice, log)
            takes = narrator.speak_batch(batch, piece_seed(batch[0], 0))
            scores = []
            for text, take in zip(batch, takes, strict=True):
                pcm = to_pcm(take)
                write_wav(check, pcm)
                heard = narrator.hear(check)
                best = (similarity(text, heard), pcm, heard)
                if best[0] < MIN_SIMILARITY:  # retry alone, with other seeds
                    retry = generate_piece(narrator, text, check)
                    best = max(best, retry, key=lambda t: t[0])
                key = piece_key(text)
                write_wav(pieces_dir / f"{key}.wav", best[1])  # the piece file appears only once it is final
                if best[0] < MIN_SIMILARITY:
                    report[key] = {"text": text, "heard": best[2], "score": round(best[0], 3)}
                    write_json(report_path, report)
                scores.append(best[0])
            done = min(w + b + len(batch), len(missing))
            elapsed = time.perf_counter() - started
            eta = elapsed / done * (len(missing) - done) / 60
            log(
                f"{done}/{len(missing)} hangdarab  (legrosszabb egyezés {min(scores):.2f}, "
                f"eltelt {elapsed / 60:.0f} perc, hátra kb. {eta:.0f} perc)"
            )
        for chapter in book:
            complete = all((pieces_dir / f"{piece_key(p['text'])}.wav").exists() for p in chapter["pieces"])
            if complete and build_chapter(chapter, pieces_dir, chapters_dir):
                log(f"Fejezet kész: {chapter_path(chapters_dir, chapter, '.m4a')}  ({chapter['title'][:50]})")

    for chapter in book:
        if build_chapter(chapter, pieces_dir, chapters_dir):
            log(f"Fejezet kész: {chapter_path(chapters_dir, chapter, '.m4a')}  ({chapter['title'][:50]})")

    out = wd.out_dir / f"{wd.book_id}.hu.m4b"
    build_m4b(wd, book, chapters_dir, out)
    log(f"Hangoskönyv kész: {out}")
    if report:
        log(f"{len(report)} hangdarab gyanús (Whisper-egyezés {MIN_SIMILARITY} alatt): {report_path}")
    return out


def chapter_path(chapters_dir: Path, chapter: dict[str, Any], suffix: str) -> Path:
    return chapters_dir / (Path(chapter["file"]).stem + suffix)


def build_chapter(chapter: dict[str, Any], pieces_dir: Path, chapters_dir: Path) -> bool:
    """Join the chapter's pieces with pauses into an .m4a. Skipped when the same pieces were joined before."""
    keys = [piece_key(p["text"]) for p in chapter["pieces"]]
    manifest = chapter_path(chapters_dir, chapter, ".json")
    m4a = chapter_path(chapters_dir, chapter, ".m4a")
    if m4a.exists() and manifest.exists() and json.loads(manifest.read_text(encoding="utf-8")) == {"keys": keys}:
        return False
    parts = []
    for key, piece in zip(keys, chapter["pieces"], strict=True):
        parts += [read_wav(pieces_dir / f"{key}.wav"), silence(piece["pause"])]
    parts.append(silence(PAUSE["chapter"]))
    wav = chapter_path(chapters_dir, chapter, ".tmp.wav")
    write_wav(wav, np.concatenate(parts))
    to_aac(wav, m4a)
    wav.unlink()
    manifest.write_text(json.dumps({"keys": keys}), encoding="utf-8")
    return True


def duration_ms(path: Path) -> int:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    ).stdout
    return round(float(out) * 1000)


# metadata ------------------------------------------------------------------
def book_metadata(wd: WorkDir) -> dict[str, Any]:
    """Hungarian title, authors, cover image and TOC chapter titles, from the source EPUB and labels.json."""
    labels: dict[str, str] = json.loads(wd.labels_path.read_text(encoding="utf-8")) if wd.labels_path.exists() else {}

    def hu(text: str) -> str:
        text = " ".join(text.split())
        return labels.get(text, text)

    with zipfile.ZipFile(wd.source) as zf:
        book = epub.read_book(zf)
        opf = epub.parse_xml(zf.read(book.opf_path))
        base = book.opf_path.rsplit("/", 1)[0] if "/" in book.opf_path else ""
        titles = [hu(t.text or "") for t in opf.iterfind(".//dc:title", epub.NS)]
        authors = [" ".join((c.text or "").split()) for c in opf.iterfind(".//dc:creator", epub.NS)]

        cover = None
        items = {i.get("id"): i for i in opf.iterfind(".//opf:manifest/opf:item", epub.NS)}
        meta = opf.find(".//opf:meta[@name='cover']", epub.NS)
        candidates = [i for i in items.values() if "cover-image" in (i.get("properties") or "").split()]
        if meta is not None and meta.get("content") in items:
            candidates.append(items[meta.get("content")])
        for item in candidates:
            if item.get("media-type") in {"image/jpeg", "image/png"}:
                href = cast(str, item.get("href"))
                cover = (resolve(base, href)[0], zf.read(resolve(base, href)[0]))
                break

        toc: dict[str, str] = {}
        if book.nav_path:
            nav = epub.parse_xml(zf.read(book.nav_path))
            nav_base = book.nav_path.rsplit("/", 1)[0] if "/" in book.nav_path else ""
            toc_nav = next(
                (n for n in nav.iter("{http://www.w3.org/1999/xhtml}nav") if "toc" in segment.semantics(n)), None
            )
            for a in toc_nav.iter("{http://www.w3.org/1999/xhtml}a") if toc_nav is not None else []:
                file = resolve(nav_base, a.get("href") or "")[0]
                toc.setdefault(file, hu("".join(a.itertext())))
    return {"titles": titles, "authors": authors, "cover": cover, "toc": toc}


def ffmeta_escape(text: str) -> str:
    return re.sub(r"([=;#\\\n])", r"\\\1", text)


def build_m4b(wd: WorkDir, book: list[dict[str, Any]], chapters_dir: Path, out: Path) -> None:
    wd.out_dir.mkdir(exist_ok=True)
    info = book_metadata(wd)
    title = ": ".join(info["titles"][:2]) or wd.book_id
    authors = ", ".join(info["authors"])
    meta = [
        ";FFMETADATA1",
        f"title={ffmeta_escape(title)}",
        f"album={ffmeta_escape(title)}",
        f"artist={ffmeta_escape(authors)}",
        f"album_artist={ffmeta_escape(authors)}",
        "genre=Audiobook",
        "language=hun",
    ]
    start = 0
    concat = []
    for chapter in book:
        m4a = chapter_path(chapters_dir, chapter, ".m4a")
        concat.append(f"file '{m4a.resolve()}'")
        end = start + duration_ms(m4a)
        name = info["toc"].get(chapter["file"]) or chapter["title"]
        meta += ["[CHAPTER]", "TIMEBASE=1/1000", f"START={start}", f"END={end}", f"title={ffmeta_escape(name)}"]
        start = end
    meta_path = chapters_dir / "chapters.txt"
    meta_path.write_text("\n".join(meta) + "\n", encoding="utf-8")
    list_path = chapters_dir / "concat.txt"
    list_path.write_text("\n".join(concat) + "\n", encoding="utf-8")

    # The chapters are already AAC: join them without re-encoding, which takes seconds
    # instead of decoding the whole book into memory.
    inputs = ["-f", "concat", "-safe", "0", "-i", str(list_path), "-i", str(meta_path)]
    maps = ["-map", "0:a", "-map_metadata", "1", "-map_chapters", "1"]
    if info["cover"]:
        cover_path = chapters_dir / ("cover" + Path(info["cover"][0]).suffix)
        cover_path.write_bytes(info["cover"][1])
        inputs += ["-i", str(cover_path)]
        maps += ["-map", "2:v", "-disposition:v:0", "attached_pic"]
    tmp = out.with_name(out.stem + ".tmp.m4b")  # players never see a half-written book
    subprocess.run(
        ["ffmpeg", "-y", "-loglevel", "error", *inputs, *maps, "-c", "copy", "-f", "mp4", str(tmp)], check=True
    )
    tmp.replace(out)


def follow(
    wd: WorkDir,
    voice: Path,
    skip: list[str],
    poll: int = 60,
    log: Callable[[str], None] = lambda m: print(m, flush=True),
) -> None:
    """Keep up with a running translation: one pass per newly translated chunk, until the book is done."""
    while True:
        translated = sum(c["translation"] is not None for c in wd.load_chunks())
        run(wd, voice, skip, log)
        total = len(wd.chunk_paths())
        if translated == total:
            log("A teljes könyv hangja elkészült.")
            return
        log(f"Lefordítva {translated}/{total} darab; várok a következőre…")
        while sum(c["translation"] is not None for c in wd.load_chunks()) == translated:
            time.sleep(poll)
