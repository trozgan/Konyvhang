"""The translation loop: one claude call per chunk, validated, saved at once."""

import json
from collections.abc import Callable
from pathlib import Path

from lxml import etree

from . import llm, segment, validate
from .workdir import WorkDir, write_json

PROMPTS = Path(__file__).parent / "prompts"
CONTEXT_SEGMENTS = 10

Caller = llm.Caller


def book_caller(wd: WorkDir) -> Caller:
    """The model provider chosen for this book (books from before providers existed use claude)."""
    return llm.caller(wd.load_state().get("provider", "claude"))


def system_prompt(profile: str) -> str:
    return (
        (PROMPTS / "common.md").read_text(encoding="utf-8")
        + "\n"
        + (PROMPTS / f"{profile}.md").read_text(encoding="utf-8")
    )


def build_prompt(glossary: str, previous: list[str], segs: list[dict]) -> str:
    source = "\n".join(f'<seg id="{i + 1}">{s["src"]}</seg>' for i, s in enumerate(segs))
    context = "\n\n".join(previous)
    return (
        f"<glossary>\n{glossary}\n</glossary>\n\n"
        f"<previous_translation>\n{context}\n</previous_translation>\n\n"
        f"<source>\n{source}\n</source>\n"
    )


def translate_segments(
    wd: WorkDir, segs: list[dict], previous: list[str], call: Caller, log: Callable[[str], None]
) -> list[str]:
    """Translate segments; returns markup per segment, in order.

    A failed response is retried once with the error attached. If that fails too,
    the segments are halved and each half is translated on its own.
    """
    state = wd.load_state()
    system = system_prompt(state["profile"])
    source = {str(i + 1): s["src"] for i, s in enumerate(segs)}
    prompt = build_prompt(wd.glossary_text(), previous, segs)

    error = None
    for attempt in range(2):
        full_prompt = prompt
        if error:
            full_prompt += (
                "\n<previous_attempt_error>\nAz előző válaszod hibás volt:\n"
                f"{error}\nJavítsd, és add vissza újra az összes szegmenst.\n</previous_attempt_error>\n"
            )
        try:
            result = call(full_prompt, system, state["model"])
            wd.add_usage(result.usage)
            parsed = validate.parse_and_check(result.text, source)
            return [inner_markup(parsed[str(i + 1)]) for i in range(len(segs))]
        except (validate.ValidationError, llm.LLMError) as e:
            if isinstance(e, llm.UsageLimitError):
                raise
            error = str(e)
            log(f"  hibás válasz ({attempt + 1}. próba): {error.splitlines()[0][:200]}")

    if len(segs) == 1:
        raise validate.ValidationError(f"Egyetlen szegmens sem fordítható hibátlanul: {error}")
    half = len(segs) // 2
    log(f"  kettévágás: {half} + {len(segs) - half} szegmens")
    first = translate_segments(wd, segs[:half], previous, call, log)
    tail = [segment.plain_text(m) for m in first[-CONTEXT_SEGMENTS:]]
    return first + translate_segments(wd, segs[half:], tail, call, log)


def inner_markup(seg: etree._Element) -> str:
    xml = etree.tostring(seg, encoding="unicode", with_tail=False)
    return xml[xml.index(">") + 1 : xml.rindex("</seg>")] if not xml.endswith("/>") else ""


def run(wd: WorkDir, call: Caller | None = None, max_chunks: int | None = None, log=print) -> bool:
    """Translate every unfinished chunk. Returns True when the whole book is done."""
    call = call or book_caller(wd)
    chunks = wd.load_chunks()
    previous: list[str] = []
    done_now = 0
    for chunk in chunks:
        if chunk["translation"] is not None:
            previous = [segment.plain_text(m) for m in chunk["translation"][-CONTEXT_SEGMENTS:]]
            continue
        if max_chunks is not None and done_now >= max_chunks:
            return False
        words = sum(len(segment.plain_text(s["src"]).split()) for s in chunk["segments"])
        log(f"[{chunk['id']}/{len(chunks):04d}] {len(chunk['segments'])} szegmens, {words} szó")
        try:
            translation = translate_segments(wd, chunk["segments"], previous, call, log)
        except llm.UsageLimitError as e:
            log(f"Elfogyott a keret, a futás leáll. Folytatás később ugyanezzel a paranccsal.\n{e}")
            return False
        except (validate.ValidationError, llm.LLMError) as e:  # progress is saved; a rerun retries this chunk
            log(f"A(z) {chunk['id']}. darab nem sikerült, a futás leáll:\n{e}")
            return False
        chunk["translation"] = translation
        wd.save_chunk(chunk)
        previous = [segment.plain_text(m) for m in translation[-CONTEXT_SEGMENTS:]]
        done_now += 1

    if not wd.labels_path.exists():
        translate_labels(wd, call, log)
    return True


def collect_labels(wd: WorkDir) -> list[str]:
    from .build import label_sources  # build owns the TOC/title locations

    return [text for _, text in label_sources(wd)]


def translate_labels(wd: WorkDir, call: Caller, log) -> None:
    labels = collect_labels(wd)
    if not labels:
        wd.labels_path.write_text("{}", encoding="utf-8")
        return
    log(f"Tartalomjegyzék és cím: {len(labels)} címke")
    state = wd.load_state()
    unique = list(dict.fromkeys(labels))
    prompt = (
        f"<glossary>\n{wd.glossary_text()}\n</glossary>\n\n"
        f"<labels>\n{json.dumps(unique, ensure_ascii=False)}\n</labels>\n"
    )
    try:
        result = call(prompt, (PROMPTS / "labels.md").read_text(encoding="utf-8"), state["model"])
        wd.add_usage(result.usage)
        text = result.text
        translated = llm.parse_json(text, "[")
    except llm.UsageLimitError as e:
        log(f"Elfogyott a keret a címkék előtt; a következő futás pótolja.\n{e}")
        return
    except (ValueError, llm.LLMError) as e:
        log(f"A címkék fordítása nem sikerült, az eredetiek maradnak: {e}")
        return
    if not is_string_list(translated) or len(translated) != len(unique):
        log("A címkék száma eltér, az eredetiek maradnak.")
        return
    write_json(wd.labels_path, dict(zip(unique, translated, strict=True)))


def is_string_list(value: object) -> bool:
    """Model output is untrusted: a JSON answer must really be a list of strings."""
    return isinstance(value, list) and all(isinstance(v, str) for v in value)
