"""Build the glossary: extract per slice of the book, then merge with Hungarian forms."""

import json
from collections.abc import Callable
from typing import Any

from . import llm, segment
from .translate import PROMPTS, Caller, book_caller
from .workdir import WorkDir, write_json

SLICE_WORDS = 40000
PROFILE_NAMES = {"fiction": "szépirodalom", "nonfiction": "szakkönyv"}


def parse_json_object(text: str) -> dict[str, Any]:
    obj: dict[str, Any] = llm.parse_json(text, "{")
    return obj


def slices(wd: WorkDir) -> list[str]:
    out, current, count = [], [], 0
    for chunk in wd.load_chunks():
        for seg in chunk["segments"]:
            text = segment.plain_text(seg["src"])
            current.append(text)
            count += len(text.split())
            if count >= SLICE_WORDS:
                out.append("\n\n".join(current))
                current, count = [], 0
    if current:
        out.append("\n\n".join(current))
    return out


def call_json(call: Caller, wd: WorkDir, prompt: str, system: str, model: str) -> dict[str, Any]:
    """Call and parse a JSON object; one retry on unparsable output."""
    result = call(prompt, system, model)
    wd.add_usage(result.usage)
    try:
        return parse_json_object(result.text)
    except ValueError:
        result = call(prompt, system, model)
        wd.add_usage(result.usage)
        return parse_json_object(result.text)


def build(wd: WorkDir, call: Caller | None = None, log: Callable[[str], None] = print) -> None:
    """Extraction results are cached in glossary_parts/, so an interrupted run resumes."""
    call = call or book_caller(wd)
    state = wd.load_state()
    parts_dir = wd.root / "glossary_parts"
    parts_dir.mkdir(exist_ok=True)
    texts = slices(wd)
    extract_system = (PROMPTS / "glossary_extract.md").read_text(encoding="utf-8")

    parts = []
    for i, text in enumerate(texts):
        part_path = parts_dir / f"{i + 1:03d}.json"
        if not part_path.exists():
            log(f"Szójegyzék: {i + 1}/{len(texts)}. szelet kigyűjtése")
            part = call_json(call, wd, f"<book_slice>\n{text}\n</book_slice>", extract_system, state["model"])
            write_json(part_path, part)  # atomic: a half-written part would break every later run
        parts.append(json.loads(part_path.read_text(encoding="utf-8")))

    log("Szójegyzék: összefésülés és magyar alakok")
    prompt = (
        f"Műfaj: {PROFILE_NAMES[state['profile']]}\n\n"
        f"<extracted>\n{json.dumps(parts, ensure_ascii=False, indent=1)}\n</extracted>\n"
    )
    glossary = call_json(call, wd, prompt, (PROMPTS / "glossary_merge.md").read_text(encoding="utf-8"), state["model"])
    wd.save_glossary(glossary)
    log(f"Szójegyzék kész: {wd.glossary_path}")
