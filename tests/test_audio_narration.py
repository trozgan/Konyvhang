"""Hungarian narration rules: what is read, how short cells are joined, and that finished audio is kept."""

import json
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from test_audio_contracts import build_book, html, opf
from test_audio_gaps import ScriptedNarrator, item

from konyvhang import audio, llm


def seg(text: str, heading: bool = False) -> dict[str, Any]:
    return {"text": text, "heading": heading}


def pieces(segments: list[dict[str, Any]], spoken: dict[str, str] | None = None) -> list[str]:
    book = [{"segments": segments}]
    audio.add_pieces(book, spoken or {})
    return [p["text"] for p in book[0]["pieces"]]


def test_similarity_reads_the_digits_whisper_writes_as_hungarian_words() -> None:
    assert audio.similarity("Huszonöt év", "25 év") == 1.0
    assert audio.similarity("Négy pont öt. Babanevelés", "4.5. Baba nevelés") >= audio.MIN_SIMILARITY
    spelled = "amelyek az ezerkilencszázhetvenes és ezerkilencszáznyolcvanas években váltak felnőtté."
    assert audio.similarity(spelled, "amelyek az 1970-es és 1980-as években váltak felnőtté.") >= audio.MIN_SIMILARITY


def test_section_numbers_are_left_out_but_numbered_words_and_lists_stay() -> None:
    assert audio.prepare(seg("4.5. Babanevelés", heading=True)) == "Babanevelés"
    assert audio.prepare(seg("1 Bevezetés és áttekintés", heading=True)) == "Bevezetés és áttekintés"
    assert audio.prepare(seg("3. fejezet", heading=True)) == "3. fejezet"
    assert audio.prepare(seg("6.1. A randizás tényleg olyan, mint egy húspiac")) == (
        "A randizás tényleg olyan, mint egy húspiac"
    )
    assert audio.prepare(seg("1. Más-más információ birtokában vagyunk")) == "1. Más-más információ birtokában vagyunk"


def test_page_references_and_fill_in_blanks_are_not_read() -> None:
    assert audio.prepare(seg("Ahogy írja (p. 350), és később (350. o.) is.")) == "Ahogy írja, és később is."
    assert audio.prepare(seg("Lásd ott (pp. 76–77).")) == "Lásd ott."
    assert audio.prepare(seg("1. Üres______________")) == "1. Üres"
    assert pieces([seg("(pp. 76–77)"), seg("Utána ez jön.")]) == ["Utána ez jön."]


def test_runs_of_short_cells_are_read_as_one_list() -> None:
    cells = [seg("Arc"), seg("nagy"), seg("halk"), seg("lassú")]
    assert pieces([seg("Jellemzők", heading=True), *cells, seg("Egy mondat.")]) == [
        "Jellemzők",
        "Arc, nagy, halk, lassú",
        "Egy mondat.",
    ]
    assert pieces([seg("Üdvözlettel:"), seg("Ali")]) == ["Üdvözlettel: Ali"]
    assert pieces([seg("– Igen."), seg("– Nem.")]) == ["– Igen.", "– Nem."]  # short lines of dialogue stay apart
    assert pieces([seg("Egy cella"), seg("Ez már egy hosszabb sor, nem cella")]) == [
        "Egy cella",
        "Ez már egy hosszabb sor, nem cella",
    ]


def test_spoken_forms_are_looked_up_after_preparing_the_text() -> None:
    spoken = {"5 = Inkább egyetértek": "öt: inkább egyetértek"}
    assert pieces([seg("4.5. Babanevelés", heading=True), seg("5 = Inkább egyetértek")], spoken) == [
        "Babanevelés",
        "öt: inkább egyetértek",
    ]


def test_finished_audio_keeps_the_text_it_was_made_from() -> None:
    old = "Négy pont öt. Babanevelés"
    book = [{"segments": [seg("4.5. Babanevelés", heading=True), seg("nagy"), seg("halk")]}]
    finished = {old, "nagy", "halk"}
    audio.add_pieces(book, {"4.5. Babanevelés": old}, finished.__contains__)
    assert [p["text"] for p in book[0]["pieces"]] == [old, "nagy", "halk"]

    finished.discard("halk")  # a half-finished group is read the new way
    audio.add_pieces(book, {"4.5. Babanevelés": old}, finished.__contains__)
    assert [p["text"] for p in book[0]["pieces"]] == [old, "nagy, halk"]


def test_roman_numerals_are_spelled_but_initials_are_not_sent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wd = build_book(tmp_path, "OEBPS/content.opf", {"OEBPS/content.opf": opf("", "")})
    sent: list[list[str]] = []

    def fake_call(prompt: str, system: str, model: str | None) -> llm.Result:
        sent.append(json.loads(prompt))
        return llm.Result(json.dumps(["második rész Funkciók"] * len(sent[-1]), ensure_ascii=False))

    monkeypatch.setattr(llm, "caller", lambda provider: fake_call)
    audio.spell_numbers(wd, ["II. rész Funkciók", "M. hatása rám:", "a XX. században"], lambda m: None)
    assert sent == [["II. rész Funkciók", "a XX. században"]]


def test_run_keeps_finished_pieces_and_spells_only_what_it_still_has_to_read(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wd = build_book(
        tmp_path,
        "OEBPS/content.opf",
        {
            "OEBPS/content.opf": opf(item("a", "ch.xhtml"), '<itemref idref="a"/>'),
            "OEBPS/ch.xhtml": html("<h2>4.5. Babanevelés</h2><p>Kész volt 2 éve.</p><p>Új 3 alma.</p>"),
        },
    )
    old = "Négy pont öt. Babanevelés"
    cache = {"4.5. Babanevelés": old, "Kész volt 2 éve.": "Kész volt két éve."}
    (wd.root / "speech.json").write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    pieces_dir = wd.root / "audio" / "pieces"
    pieces_dir.mkdir(parents=True)
    for text in (old, "Kész volt két éve."):
        audio.write_wav(pieces_dir / f"{audio.piece_key(text)}.wav", np.zeros(240, dtype=np.int16))
    sent: list[list[str]] = []

    def fake_call(prompt: str, system: str, model: str | None) -> llm.Result:
        sent.append(json.loads(prompt))
        return llm.Result('["Új három alma."]')

    monkeypatch.setattr(llm, "caller", lambda provider: fake_call)
    narrator = ScriptedNarrator(lambda text, attempt: text)
    monkeypatch.setattr(audio, "get_narrator", lambda voice, log: narrator)

    audio.run(wd, Path("voice"), [], log=lambda m: None)

    assert sent == [["Új 3 alma."]]
    assert [text for text, _ in narrator.takes] == ["Új három alma."]
    manifest = json.loads((wd.root / "audio" / "chapters" / "OEBPS_ch.json").read_text(encoding="utf-8"))
    assert manifest["keys"] == [audio.piece_key(t) for t in (old, "Kész volt két éve.", "Új három alma.")]
