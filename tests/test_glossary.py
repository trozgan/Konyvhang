"""Glossary building with a fake model: slicing, caching, the JSON retry."""

import pytest

from konyvhang import glossary, llm
from konyvhang.workdir import WorkDir


@pytest.fixture
def wd(epub_file, tmp_path):
    wd = WorkDir(tmp_path / "work" / "book")
    wd.root.mkdir(parents=True)
    wd.source.write_bytes(epub_file.read_bytes())
    wd.save_state({"profile": "nonfiction", "provider": "claude", "model": "opus", "source_name": "book.epub"})
    wd.write_chunks()
    return wd


class FakeModel:
    """Answers extraction with one term per slice and the merge with a glossary."""

    def __init__(self, bad=0):
        self.bad = bad  # this many unparsable answers first
        self.prompts = []

    def __call__(self, prompt, system, model):
        self.prompts.append(prompt)
        if self.bad:
            self.bad -= 1
            return llm.Result("Sorry, no JSON here.", {"input_tokens": 1, "output_tokens": 1})
        if "<book_slice>" in prompt:
            return llm.Result(f'Here: {{"terms": [{{"source": "slice{len(self.prompts)}"}}]}}', {"input_tokens": 3})
        return llm.Result('{"style_notes": "plain", "terms": []}', {"output_tokens": 2})


def test_slices_split_on_word_count(wd, monkeypatch):
    assert len(glossary.slices(wd)) == 1
    monkeypatch.setattr(glossary, "SLICE_WORDS", 20)
    parts = glossary.slices(wd)
    assert len(parts) > 1
    assert "Chapter One" in parts[0]
    monkeypatch.setattr(glossary, "SLICE_WORDS", 1)  # every segment closes a slice: nothing left over
    assert len(glossary.slices(wd)) == sum(len(c["segments"]) for c in wd.load_chunks())


def test_build_extracts_merges_and_caches_parts(wd, monkeypatch):
    monkeypatch.setattr(glossary, "SLICE_WORDS", 20)
    fake = FakeModel()
    logs = []
    glossary.build(wd, call=fake, log=logs.append)

    slices = len(glossary.slices(wd))
    assert len(fake.prompts) == slices + 1  # one per slice, then the merge
    assert "Műfaj: szakkönyv" in fake.prompts[-1]
    assert wd.glossary_path.exists()
    assert "style_notes: plain" in wd.glossary_text()
    assert len(list((wd.root / "glossary_parts").glob("*.json"))) == slices
    assert logs[-1].startswith("Szójegyzék kész")
    assert wd.load_state()["usage"]["calls"] == slices + 1

    again = FakeModel()
    glossary.build(wd, call=again, log=lambda m: None)
    assert len(again.prompts) == 1  # extraction came from the cache, only the merge ran


def test_build_uses_the_books_provider(wd, monkeypatch):
    fake = FakeModel()
    monkeypatch.setattr(llm, "caller", lambda provider: fake)
    glossary.build(wd, log=lambda m: None)
    assert fake.prompts


def test_call_json_retries_once_then_gives_up(wd):
    fake = FakeModel(bad=1)
    assert glossary.call_json(fake, wd, "<book_slice>x</book_slice>", "sys", "opus") == {
        "terms": [{"source": "slice2"}]
    }
    with pytest.raises(ValueError):
        glossary.call_json(FakeModel(bad=2), wd, "prompt", "sys", "opus")


def test_parse_json_object_ignores_surrounding_text():
    assert glossary.parse_json_object('Itt van:\n{"a": [1]}\nKész.') == {"a": [1]}
