"""The per-book work directory: source copy, chunks, glossary, state."""

import json
import zipfile
from pathlib import Path
from typing import Any

import yaml

from . import epub, segment

MAX_WORDS = 8000


class WorkDir:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.source = root / "source.epub"
        self.chunks_dir = root / "chunks"
        self.glossary_path = root / "glossary.yaml"
        self.state_path = root / "state.json"
        self.labels_path = root / "labels.json"
        self.out_dir = root / "out"

    @property
    def book_id(self) -> str:
        return self.root.name

    def exists(self) -> bool:
        return self.state_path.exists()

    # state ---------------------------------------------------------------
    def load_state(self) -> dict[str, Any]:
        state: dict[str, Any] = json.loads(self.state_path.read_text(encoding="utf-8"))
        return state

    def save_state(self, state: dict[str, Any]) -> None:
        write_json(self.state_path, state)

    def add_usage(self, usage: dict[str, Any]) -> None:
        state = self.load_state()
        total = state.setdefault("usage", {})
        for key, value in usage.items():
            total[key] = total.get(key, 0) + value
        total["calls"] = total.get("calls", 0) + 1
        self.save_state(state)

    # glossary ------------------------------------------------------------
    def glossary_text(self) -> str:
        return self.glossary_path.read_text(encoding="utf-8") if self.glossary_path.exists() else ""

    def save_glossary(self, glossary: dict[str, Any]) -> None:
        self.glossary_path.write_text(
            yaml.safe_dump(glossary, allow_unicode=True, sort_keys=False, width=100), encoding="utf-8"
        )

    # chunks --------------------------------------------------------------
    def chunk_paths(self) -> list[Path]:
        return sorted(self.chunks_dir.glob("*.json"))

    def load_chunks(self) -> list[dict[str, Any]]:
        return [json.loads(p.read_text(encoding="utf-8")) for p in self.chunk_paths()]

    def save_chunk(self, chunk: dict[str, Any]) -> None:
        write_json(self.chunks_dir / f"{chunk['id']}.json", chunk)

    def write_chunks(self, references: bool = False) -> int:
        """Split the book into chunks of whole segments and save them."""
        self.chunks_dir.mkdir(parents=True, exist_ok=True)
        chunks = build_chunks(self.source, references)
        for chunk in chunks:
            self.save_chunk(chunk)
        return len(chunks)


def write_json(path: Path, data: object) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(path)


def build_chunks(epub_path: Path, references: bool = False) -> list[dict[str, Any]]:
    """Pack whole spine files into chunks up to MAX_WORDS; split a file only if it alone is longer.

    Bibliographies and indexes stay out unless `references` is set: they remain in English.

    A chunk segment is {"file", "idx", "src"}: idx is the position in
    find_segments() of that file, src is the model markup.
    """
    with zipfile.ZipFile(epub_path) as zf:
        book = epub.read_book(zf)
        files = []
        for path in book.spine:
            tree = epub.parse_xml(zf.read(path))
            segs = [
                {"file": path, "idx": i, "src": segment.to_markup(el)}
                for i, el in enumerate(segment.find_segments(tree))
                if references or not segment.in_reference_matter(el)
            ]
            if segs:
                files.append(segs)

    def words(segs: list[dict[str, Any]]) -> int:
        return sum(len(segment.plain_text(s["src"]).split()) for s in segs)

    groups: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    for segs in files:
        if current and words(current) + words(segs) > MAX_WORDS:
            groups.append(current)
            current = []
        if words(segs) > MAX_WORDS:
            groups.extend(split_by_words(segs))
        else:
            current.extend(segs)
    if current:
        groups.append(current)
    return [{"id": f"{i + 1:04d}", "segments": segs, "translation": None} for i, segs in enumerate(groups)]


def split_by_words(segs: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    total = sum(len(segment.plain_text(s["src"]).split()) for s in segs)
    parts = -(-total // MAX_WORDS)
    target = total / parts
    out: list[list[dict[str, Any]]] = []
    current: list[dict[str, Any]] = []
    count = 0
    for s in segs:
        current.append(s)
        count += len(segment.plain_text(s["src"]).split())
        if count >= target and len(out) < parts - 1:
            out.append(current)
            current, count = [], 0
    if current:
        out.append(current)
    return out
