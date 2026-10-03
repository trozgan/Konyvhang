"""What reaches the model: prompts, system prompts, model names, request shapes and argv.

The fakes here record every argument, so a dropped glossary, system prompt or model
fails a test instead of passing silently.
"""

import json
import re
import subprocess
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, ClassVar

import anthropic
import httpx2
import openai
import pytest

from konyvhang import glossary, llm, translate, validate
from konyvhang.workdir import WorkDir

PROMPTS = Path(translate.__file__).parent / "prompts"


def prompt_file(name: str) -> str:
    return (PROMPTS / name).read_text(encoding="utf-8")


class Recorder:
    """A caller that records (prompt, system, model) and answers from a script.

    Script items: "ok" echoes every source segment with "HU " in front, "bad" is an
    unusable answer, "limit" raises UsageLimitError, and any other string is returned as is.
    """

    def __init__(self, script: Sequence[str] = ()) -> None:
        self.script = list(script)
        self.calls: list[tuple[str, str, str | None]] = []

    @property
    def prompts(self) -> list[str]:
        return [prompt for prompt, _, _ in self.calls]

    def __call__(self, prompt: str, system: str, model: str | None) -> llm.Result:
        self.calls.append((prompt, system, model))
        action = self.script.pop(0) if self.script else "ok"
        if action == "limit":
            raise llm.UsageLimitError("You've hit your limit · resets 5pm")
        if action == "bad":
            return llm.Result("Sorry.")
        if action != "ok":
            return llm.Result(action)
        source = prompt.split("<source>")[1]
        segs = re.findall(r'<seg id="(\d+)">(.*?)</seg>', source)
        return llm.Result("\n".join(f'<seg id="{sid}">HU {body}</seg>' for sid, body in segs))


def sentences(prefix: str, count: int) -> list[dict[str, Any]]:
    return [{"file": "ch.xhtml", "idx": i, "src": f"{prefix} sentence {i + 1}."} for i in range(count)]


def make_book(tmp_path: Path, *chunks: list[dict[str, Any]], model: str = "custom-model-7") -> WorkDir:
    """A work folder with hand-made chunks; no EPUB is needed because labels are stubbed."""
    wd = WorkDir(tmp_path / "work" / "book")
    wd.chunks_dir.mkdir(parents=True)
    wd.save_state({"profile": "fiction", "provider": "claude", "model": model, "source_name": "book.epub"})
    for i, segs in enumerate(chunks):
        wd.save_chunk({"id": f"{i + 1:04d}", "segments": segs, "translation": None})
    return wd


@pytest.fixture(autouse=True)
def no_labels(monkeypatch: pytest.MonkeyPatch) -> None:
    """The hand-made books have no TOC; tests that need labels set their own."""
    monkeypatch.setattr(translate, "collect_labels", lambda wd: [])


def block(prompt: str, tag: str) -> str:
    return prompt.split(f"<{tag}>\n")[1].split(f"\n</{tag}>", maxsplit=1)[0]


# translate: prompt, system and model ------------------------------------------------
def test_translation_sends_glossary_system_prompt_and_the_books_model(tmp_path: Path) -> None:
    wd = make_book(tmp_path, sentences("A", 2))
    wd.save_glossary({"terms": [{"source": "Tom", "target": "Tamás"}]})
    fake = Recorder()
    assert translate.run(wd, call=fake, log=lambda m: None)

    prompt, system, model = fake.calls[0]
    assert model == "custom-model-7"
    assert system == prompt_file("common.md") + "\n" + prompt_file("fiction.md")
    assert prompt.startswith(f"<glossary>\n{wd.glossary_text()}\n</glossary>\n\n")
    assert "Tamás" in wd.glossary_text()
    assert block(prompt, "source") == '<seg id="1">A sentence 1.</seg>\n<seg id="2">A sentence 2.</seg>'


def test_next_chunk_gets_the_last_ten_translated_segments_as_context(tmp_path: Path) -> None:
    wd = make_book(tmp_path, sentences("A", 12), sentences("B", 1))
    fake = Recorder()
    translate.run(wd, call=fake, log=lambda m: None)
    expected = "\n\n".join(f"HU A sentence {i}." for i in range(3, 13))
    assert block(fake.prompts[0], "previous_translation") == ""
    assert block(fake.prompts[1], "previous_translation") == expected


def test_resumed_run_takes_context_from_the_last_finished_chunk(tmp_path: Path) -> None:
    wd = make_book(tmp_path, sentences("A", 12), sentences("B", 1))
    first = wd.load_chunks()[0]
    first["translation"] = [f"Kész {i}." for i in range(1, 13)]
    wd.save_chunk(first)
    fake = Recorder()
    translate.run(wd, call=fake, log=lambda m: None)
    assert len(fake.calls) == 1
    assert block(fake.prompts[0], "previous_translation") == "\n\n".join(f"Kész {i}." for i in range(3, 13))


def test_retry_prompt_carries_the_validation_error(tmp_path: Path) -> None:
    wd = make_book(tmp_path, sentences("A", 1))
    fake = Recorder(["bad", "ok"])
    translate.run(wd, call=fake, log=lambda m: None)

    with pytest.raises(validate.ValidationError) as caught:
        validate.parse_and_check("Sorry.", {"1": "x"})
    first, retry = fake.prompts
    assert retry.startswith(first)
    assert retry[len(first) :] == (
        "\n<previous_attempt_error>\nAz előző válaszod hibás volt:\n"
        f"{caught.value}\nJavítsd, és add vissza újra az összes szegmenst.\n</previous_attempt_error>\n"
    )
    assert fake.calls[1][1:] == fake.calls[0][1:]  # same system prompt and model on the retry


def test_split_halves_keep_context_and_logging(tmp_path: Path) -> None:
    wd = make_book(tmp_path, sentences("A", 24))
    fake = Recorder(["bad", "bad", "ok", "bad", "ok"])  # whole chunk twice, first half, second half twice
    logs: list[str] = []
    assert translate.run(wd, call=fake, log=logs.append)

    second_half = fake.prompts[3]
    assert block(second_half, "source").startswith('<seg id="1">A sentence 13.</seg>')
    assert block(second_half, "previous_translation") == "\n\n".join(f"HU A sentence {i}." for i in range(3, 13))
    assert sum("hibás válasz" in m for m in logs) == 3  # the second half logs its own retry
    assert wd.load_chunks()[0]["translation"][23] == "HU A sentence 24."


def test_max_chunks_stops_after_that_many_and_reports_unfinished(tmp_path: Path) -> None:
    wd = make_book(tmp_path, sentences("A", 1), sentences("B", 1), sentences("C", 1))
    fake = Recorder()
    assert translate.run(wd, call=fake, max_chunks=2, log=lambda m: None) is False
    assert [c["translation"] is not None for c in wd.load_chunks()] == [True, True, False]
    assert not wd.labels_path.exists()

    assert translate.run(wd, call=fake, max_chunks=2, log=lambda m: None) is True
    assert wd.labels_path.exists()


def test_quota_message_reaches_the_log(tmp_path: Path) -> None:
    wd = make_book(tmp_path, sentences("A", 1))
    logs: list[str] = []
    assert not translate.run(wd, call=Recorder(["limit"]), log=logs.append)
    assert any("resets 5pm" in m for m in logs)


def test_labels_use_their_own_system_prompt_and_the_books_model(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    wd = make_book(tmp_path, sentences("A", 1))
    monkeypatch.setattr(translate, "collect_labels", lambda wd: ["Café", "Café", "Two"])
    fake = Recorder(["ok", '["Kávézó", "Kettő"]'])
    translate.run(wd, call=fake, log=lambda m: None)

    prompt, system, model = fake.calls[1]
    assert system == prompt_file("labels.md")
    assert model == "custom-model-7"
    assert block(prompt, "labels") == '["Café", "Two"]'
    assert json.loads(wd.labels_path.read_text(encoding="utf-8")) == {"Café": "Kávézó", "Two": "Kettő"}


def test_books_without_a_saved_provider_use_claude(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    wd = make_book(tmp_path)
    state = wd.load_state()
    del state["provider"]
    wd.save_state(state)
    asked: list[str] = []

    def caller(provider: str) -> llm.Caller:
        asked.append(provider)
        return Recorder()

    monkeypatch.setattr(llm, "caller", caller)
    translate.book_caller(wd)
    assert asked == ["claude"]


# glossary: slice text and extracted parts --------------------------------------------
def glossary_book(tmp_path: Path) -> WorkDir:
    """Segments of 1, 2, 1, 1, 2, 1, 1, 1 words: with SLICE_WORDS = 4 the slices end exactly at 4."""
    words = ["Alpha", "Bravo two", "Charlie", "Delta", "Echo two", "Foxtrot", "Golf", "Hotel"]
    segs = [{"file": "ch.xhtml", "idx": i, "src": w} for i, w in enumerate(words)]
    wd = make_book(tmp_path, segs[:2], segs[2:])
    state = wd.load_state()
    state["profile"] = "nonfiction"
    wd.save_state(state)
    return wd


class GlossaryModel:
    """Answers each slice with its own non-ASCII term and the merge with a glossary."""

    def __init__(self, bad_first: bool = False) -> None:
        self.bad_first = bad_first
        self.calls: list[tuple[str, str, str | None]] = []

    def __call__(self, prompt: str, system: str, model: str | None) -> llm.Result:
        self.calls.append((prompt, system, model))
        if self.bad_first:
            self.bad_first = False
            return llm.Result("no JSON")
        if "<book_slice>" in prompt:
            first_word = block(prompt, "book_slice").split()[0]
            return llm.Result(json.dumps({"terms": [{"source": first_word, "target": f"{first_word}-ő"}]}))
        return llm.Result('{"terms": [{"source": "Alpha", "target": "Alfa"}]}')


def test_glossary_slices_end_where_the_word_count_reaches_the_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(glossary, "SLICE_WORDS", 4)
    assert glossary.slices(glossary_book(tmp_path)) == [
        "Alpha\n\nBravo two\n\nCharlie",
        "Delta\n\nEcho two\n\nFoxtrot",
        "Golf\n\nHotel",
    ]


def test_glossary_sends_each_slice_and_merges_every_extracted_part(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(glossary, "SLICE_WORDS", 4)
    wd = glossary_book(tmp_path)
    fake = GlossaryModel()
    glossary.build(wd, call=fake, log=lambda m: None)

    extract, merge = fake.calls[:3], fake.calls[3]
    assert [p for p, _, _ in extract] == [f"<book_slice>\n{text}\n</book_slice>" for text in glossary.slices(wd)]
    assert all(s == prompt_file("glossary_extract.md") and m == "custom-model-7" for _, s, m in extract)

    parts = [{"terms": [{"source": w, "target": f"{w}-ő"}]} for w in ("Alpha", "Delta", "Golf")]
    assert merge == (
        f"Műfaj: szakkönyv\n\n<extracted>\n{json.dumps(parts, ensure_ascii=False, indent=1)}\n</extracted>\n",
        prompt_file("glossary_merge.md"),
        "custom-model-7",
    )
    assert "glossary_parts" in [p.name for p in wd.root.iterdir()]  # exact case, also on macOS
    cached = sorted(p.name for p in (wd.root / "glossary_parts").iterdir())
    assert cached == ["001.json", "002.json", "003.json"]
    assert json.loads((wd.root / "glossary_parts" / "001.json").read_text(encoding="utf-8")) == parts[0]
    assert wd.glossary_text().startswith("terms:")

    rerun = GlossaryModel()
    glossary.build(wd, call=rerun, log=lambda m: None)
    assert rerun.calls == [merge]  # cached parts are reused and merged the same way


def test_glossary_json_retry_keeps_system_prompt_and_model(tmp_path: Path) -> None:
    wd = glossary_book(tmp_path)
    fake = GlossaryModel(bad_first=True)
    glossary.build(wd, call=fake, log=lambda m: None)
    assert fake.calls[0] == fake.calls[1]


# llm: the CLIs --------------------------------------------------------------------
def completed(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> NS:
    """subprocess.run in llm records argv and kwargs; `which` finds every CLI under /opt/bin."""
    state = NS(result=completed(), cmd=None, kwargs=None, last_message=None)

    def fake(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:  # noqa: ANN401 - subprocess kwargs
        state.cmd, state.kwargs = cmd, kwargs
        if state.last_message is not None:
            Path(cmd[cmd.index("-o") + 1]).write_text(state.last_message, encoding="utf-8")
        result: subprocess.CompletedProcess[str] = state.result
        return result

    monkeypatch.setattr("konyvhang.llm.subprocess.run", fake)
    monkeypatch.setattr("konyvhang.llm.shutil.which", lambda name: f"/opt/bin/{name}")
    return state


CLAUDE_OK = json.dumps({"subtype": "success", "result": "Szia", "usage": {}})


def test_claude_cli_argv_without_tools_or_settings(run: NS) -> None:
    run.result = completed(CLAUDE_OK)
    llm.caller("claude")("the prompt", "the system", "opus")
    assert run.cmd == [
        "/opt/bin/claude", "-p", "--output-format", "json", "--system-prompt", "the system",
        "--tools", "", "--no-session-persistence", "--setting-sources", "", "--model", "opus",
    ]  # fmt: skip
    llm.caller("claude")("the prompt", "the system", None)
    assert run.cmd == [
        "/opt/bin/claude", "-p", "--output-format", "json", "--system-prompt", "the system",
        "--tools", "", "--no-session-persistence", "--setting-sources", "",
    ]  # fmt: skip


def test_cli_falls_back_to_the_bare_command_name(run: NS, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("konyvhang.llm.shutil.which", lambda name: None)
    run.result = completed(CLAUDE_OK)
    llm.caller("claude")("p", "s", None)
    assert run.cmd[0] == "claude"
    run.result = completed()
    run.last_message = "Szia"
    llm.caller("codex")("p", "s", None)
    assert run.cmd[0] == "codex"


def test_cli_calls_are_bounded_and_decoded_as_utf8(run: NS) -> None:
    run.result = completed(CLAUDE_OK)
    llm.caller("claude")("the prompt", "s", None)
    assert run.kwargs == {
        "input": "the prompt", "capture_output": True, "text": True, "encoding": "utf-8",
        "timeout": llm.TIMEOUT, "check": False,
    }  # fmt: skip


def test_claude_success_without_result_or_usage_is_empty_and_free(run: NS) -> None:
    run.result = completed(json.dumps({"subtype": "success"}))
    assert llm.caller("claude")("p", "s", None) == llm.Result("", {"input_tokens": 0, "output_tokens": 0})


def test_claude_is_error_flag_wins_over_a_success_subtype(run: NS) -> None:
    run.result = completed(json.dumps({"is_error": True, "subtype": "success", "result": "API Error: overloaded"}))
    with pytest.raises(llm.LLMError, match="overloaded") as caught:
        llm.caller("claude")("p", "s", None)
    assert not isinstance(caught.value, llm.UsageLimitError)


@pytest.mark.parametrize(
    ("result", "error", "text"),
    [
        (completed("not json", "You've hit your usage limit · resets 5pm"), llm.UsageLimitError, "resets 5pm"),
        (completed("not json", "segfault", 1), llm.LLMError, "segfault"),
        (completed(json.dumps({"is_error": True, "result": "Usage limit reached"})), llm.UsageLimitError, "reached"),
        (completed(json.dumps({"subtype": "error", "result": "boom"})), llm.LLMError, "boom"),
    ],
)
def test_claude_errors_keep_the_cli_message(
    run: NS, result: subprocess.CompletedProcess[str], error: type[llm.LLMError], text: str
) -> None:
    run.result = result
    with pytest.raises(error, match=text):
        llm.caller("claude")("p", "s", None)


def test_codex_cli_argv_runs_read_only_in_an_empty_folder(run: NS) -> None:
    run.last_message = "Szia"
    llm.caller("codex")("p", "s", "gpt-x")
    workdir = run.cmd[run.cmd.index("-C") + 1]
    last = run.cmd[run.cmd.index("-o") + 1]
    assert Path(last).parent == Path(workdir)
    assert run.cmd == [
        "/opt/bin/codex", "exec", "--ephemeral", "--skip-git-repo-check", "--ignore-user-config",
        "--ignore-rules", "--sandbox", "read-only", "-C", workdir, "--json", "-o", last, "-m", "gpt-x", "-",
    ]  # fmt: skip
    llm.caller("codex")("p", "s", None)
    assert run.cmd[-3:-1] == ["-o", run.cmd[run.cmd.index("-o") + 1]] and "-m" not in run.cmd


def codex_events(*items: object) -> str:
    return "\n".join(json.dumps(e) for e in items) + "\n"


def test_codex_answer_survives_error_events_of_a_successful_turn(run: NS) -> None:
    run.last_message = "Szia"
    run.result = completed(codex_events({"type": "error", "message": "reconnecting"}))
    assert llm.caller("codex")("p", "s", None).text == "Szia"


def test_codex_error_messages_are_joined(run: NS) -> None:
    run.result = completed(
        codex_events({"type": "error", "message": "first"}, {"type": "error", "message": "second"}), code=1
    )
    with pytest.raises(llm.LLMError, match="first second"):
        llm.caller("codex")("p", "s", None)


def test_codex_error_event_without_message_falls_back_to_stderr(run: NS) -> None:
    run.result = completed(codex_events({"type": "turn.failed"}), stderr="stderr says why", code=1)
    with pytest.raises(llm.LLMError, match="stderr says why"):
        llm.caller("codex")("p", "s", None)


def test_codex_is_checked_for_installation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("konyvhang.llm.shutil.which", lambda name: "/opt/bin/claude" if name == "claude" else None)
    llm.check_ready("claude")
    with pytest.raises(llm.LLMError, match="codex"):
        llm.check_ready("codex")


def test_openrouter_needs_a_model() -> None:
    with pytest.raises(llm.LLMError, match="openrouter"):
        llm.default_model("openrouter", None)


# llm: the APIs ----------------------------------------------------------------------
class Stream:
    def __init__(self, message: NS) -> None:
        self.message = message

    def __enter__(self) -> NS:
        return NS(get_final_message=lambda: self.message)

    def __exit__(self, *exc: object) -> None:
        return None


class RecordingAnthropic:
    """A fake anthropic.Anthropic that records the client options and the request."""

    client: ClassVar[dict[str, Any]] = {}
    params: ClassVar[dict[str, Any]] = {}

    def __init__(self, **kwargs: Any) -> None:  # noqa: ANN401 - SDK client options
        RecordingAnthropic.client = kwargs
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **params: Any) -> Stream:  # noqa: ANN401 - SDK request
        RecordingAnthropic.params = params
        return Stream(
            NS(
                stop_reason="end_turn",
                content=[NS(type="text", text="Szia "), NS(type="thinking"), NS(type="text", text="világ")],
                usage=NS(input_tokens=10, cache_read_input_tokens=None, cache_creation_input_tokens=4, output_tokens=7),
            )
        )


@pytest.fixture
def fake_anthropic(monkeypatch: pytest.MonkeyPatch) -> type[RecordingAnthropic]:
    monkeypatch.setattr(anthropic, "Anthropic", RecordingAnthropic)
    return RecordingAnthropic


def test_anthropic_request_shape(fake_anthropic: type[RecordingAnthropic]) -> None:
    result = llm.caller("anthropic")("the prompt", "the system", "claude-opus-4-8")
    assert fake_anthropic.client == {"timeout": llm.TIMEOUT, "max_retries": 5}
    assert fake_anthropic.params == {
        "model": "claude-opus-4-8",
        "max_tokens": llm.MAX_TOKENS,
        "system": "the system",
        "messages": [{"role": "user", "content": "the prompt"}],
        "output_config": {"effort": "medium"},
    }
    assert result.text == "Szia világ"
    assert result.usage == {"input_tokens": 14, "output_tokens": 7}


def test_anthropic_fallback_models_add_only_the_fallback_keys(fake_anthropic: type[RecordingAnthropic]) -> None:
    llm.caller("anthropic")("the prompt", "the system", "claude-opus-5-5")
    assert fake_anthropic.params == {
        "model": "claude-opus-5-5",
        "max_tokens": llm.MAX_TOKENS,
        "system": "the system",
        "messages": [{"role": "user", "content": "the prompt"}],
        "output_config": {"effort": "medium"},
        "betas": ["server-side-fallback-2026-07-01"],
        "fallbacks": "default",
    }


@pytest.mark.parametrize(("stop_reason", "text"), [("refusal", "elutasította"), ("max_tokens", "hosszkorlát")])
def test_anthropic_incomplete_answers_say_why(monkeypatch: pytest.MonkeyPatch, stop_reason: str, text: str) -> None:
    def client(**kwargs: object) -> NS:
        message = NS(stop_reason=stop_reason, content=[], usage=None)
        return NS(beta=NS(messages=NS(stream=lambda **params: Stream(message))))

    monkeypatch.setattr(anthropic, "Anthropic", client)
    with pytest.raises(llm.LLMError, match=text):
        llm.caller("anthropic")("p", "s", "claude-opus-5-5")


def openai_client(captured: dict[str, Any], chunks: list[NS] | None = None) -> Callable[..., NS]:
    def create(**params: Any) -> Iterator[NS]:  # noqa: ANN401 - SDK request
        captured["request"] = params
        return iter(
            chunks
            if chunks is not None
            else [NS(usage=None, choices=[NS(delta=NS(content="Szia"), finish_reason="stop")])]
        )

    def client(**kwargs: Any) -> NS:  # noqa: ANN401 - SDK client options
        captured["client"] = kwargs
        return NS(chat=NS(completions=NS(create=create)))

    return client


@pytest.mark.parametrize(
    ("provider", "key_env", "base_url", "extra_body"),
    [
        ("openai", "OPENAI_API_KEY", None, None),
        ("openrouter", "OPENROUTER_API_KEY", llm.OPENROUTER_URL, {"usage": {"include": True}}),
    ],
)
def test_openai_compatible_request_shape(
    monkeypatch: pytest.MonkeyPatch, provider: str, key_env: str, base_url: str | None, extra_body: object
) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv(key_env, f"key-for-{provider}")
    monkeypatch.setattr(openai, "OpenAI", openai_client(captured))
    llm.caller(provider)("the prompt", "the system", "vendor/model")
    assert captured["client"] == {
        "api_key": f"key-for-{provider}", "base_url": base_url, "timeout": llm.TIMEOUT, "max_retries": 5,
    }  # fmt: skip
    assert captured["request"] == {
        "model": "vendor/model",
        "stream": True,
        "stream_options": {"include_usage": True},
        "messages": [{"role": "system", "content": "the system"}, {"role": "user", "content": "the prompt"}],
        "extra_body": extra_body,
    }


def test_openai_stream_without_usage_counts_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [NS(usage=None, choices=[NS(delta=NS(content="Szia"), finish_reason="stop")])]
    monkeypatch.setattr(openai, "OpenAI", openai_client({}, chunks))
    assert llm.caller("openai")("p", "s", "m").usage == {"input_tokens": 0, "output_tokens": 0}


def test_openai_usage_with_null_counts_is_zero(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [NS(usage=NS(prompt_tokens=None, completion_tokens=None, model_extra=None), choices=[])]
    monkeypatch.setattr(openai, "OpenAI", openai_client({}, chunks))
    assert llm.caller("openai")("p", "s", "m").usage == {"input_tokens": 0, "output_tokens": 0}


def response(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=httpx2.Request("POST", "https://api.example/v1"))


def raising_openai(error: Exception) -> Callable[..., NS]:
    def create(**params: object) -> None:
        raise error

    return lambda **kw: NS(chat=NS(completions=NS(create=create)))


@pytest.mark.parametrize(
    ("error", "expected", "text"),
    [
        (
            openai.RateLimitError("rate limited, try at 5pm", response=response(429), body=None),
            llm.UsageLimitError,
            "5pm",
        ),
        (openai.APIStatusError("insufficient_quota", response=response(400), body=None), llm.UsageLimitError, "quota"),
        (openai.APIStatusError("Out of credits", response=response(402), body=None), llm.UsageLimitError, "credits"),
        (openai.APIStatusError("bad gateway", response=response(502), body=None), llm.LLMError, "bad gateway"),
        (
            openai.APIConnectionError(message="no route", request=httpx2.Request("POST", "https://x")),
            llm.LLMError,
            "no route",
        ),
    ],
)
def test_openai_errors_keep_their_message_and_quota_stops_on_any_status(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: type[llm.LLMError], text: str
) -> None:
    monkeypatch.setattr(openai, "OpenAI", raising_openai(error))
    with pytest.raises(expected, match=text) as caught:
        llm.caller("openai")("p", "s", "m")
    assert isinstance(caught.value, llm.UsageLimitError) == (expected is llm.UsageLimitError)
