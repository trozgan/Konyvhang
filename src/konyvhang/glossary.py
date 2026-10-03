"""Build the glossary: extract per slice of the book, then merge with Hungarian forms."""

import json
from pathlib import Path

from . import claude_cli, segment
from .translate import PROMPTS, Caller
from .workdir import WorkDir

SLICE_WORDS = 40000
PROFILE_NAMES = {"fiction": "szépirodalom", "nonfiction": "szakkönyv"}


def parse_json_object(text: str) -> dict:
    return claude_cli.parse_json(text, "{")


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


def call_json(call: Caller, wd: WorkDir, prompt: str, system: str, model: str) -> dict:
    """Call and parse a JSON object; one retry on unparsable output."""
    for attempt in range(2):
        result = call(prompt, system, model)
        wd.add_usage(result.usage)
        try:
            return parse_json_object(result.text)
        except ValueError:
            if attempt:
                raise
    raise AssertionError("unreachable")


def build(wd: WorkDir, call: Caller = claude_cli.call, log=print) -> None:
    """Extraction results are cached in glossary_parts/, so an interrupted run resumes."""
    state = wd.load_state()
    parts_dir = wd.root / "glossary_parts"
    parts_dir.mkdir(exist_ok=True)
    texts = slices(wd)
    extract_system = (PROMPTS / "glossary_extract.md").read_text()

    parts = []
    for i, text in enumerate(texts):
        part_path = parts_dir / f"{i + 1:03d}.json"
        if not part_path.exists():
            log(f"Szójegyzék: {i + 1}/{len(texts)}. szelet kigyűjtése")
            part = call_json(call, wd, f"<book_slice>\n{text}\n</book_slice>", extract_system, state["model"])
            part_path.write_text(json.dumps(part, ensure_ascii=False, indent=1))
        parts.append(json.loads(part_path.read_text()))

    log("Szójegyzék: összefésülés és magyar alakok")
    prompt = (
        f"Műfaj: {PROFILE_NAMES[state['profile']]}\n\n"
        f"<extracted>\n{json.dumps(parts, ensure_ascii=False, indent=1)}\n</extracted>\n"
    )
    glossary = call_json(call, wd, prompt, (PROMPTS / "glossary_merge.md").read_text(), state["model"])
    wd.save_glossary(glossary)
    log(f"Szójegyzék kész: {wd.glossary_path}")
