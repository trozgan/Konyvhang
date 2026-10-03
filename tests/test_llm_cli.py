"""The subscription CLIs (claude, codex) with a fake subprocess, and the API error mapping."""

import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace as NS

import anthropic
import httpx2
import openai
import pytest

from konyvhang import llm


def completed(stdout: str = "", stderr: str = "", code: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr=stderr)


@pytest.fixture
def run(monkeypatch: pytest.MonkeyPatch) -> NS:
    """Replace subprocess.run in llm; `run.result` is what the fake returns, `run.cmd` what it got."""
    state = NS(result=completed(), cmd=None, stdin=None, last_message=None)

    def fake(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        state.cmd, state.stdin = cmd, kwargs["input"]
        if state.last_message is not None and "-o" in cmd:
            Path(cmd[cmd.index("-o") + 1]).write_text(state.last_message, encoding="utf-8")
        if isinstance(state.result, Exception):
            raise state.result
        result: subprocess.CompletedProcess[str] = state.result
        return result

    monkeypatch.setattr("konyvhang.llm.subprocess.run", fake)
    monkeypatch.setattr("konyvhang.llm.shutil.which", lambda name: None)
    return state


# readiness ------------------------------------------------------------------
def test_check_ready(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("konyvhang.llm.shutil.which", lambda name: None)
    with pytest.raises(llm.LLMError, match="claude"):
        llm.check_ready("claude")
    monkeypatch.setattr("konyvhang.llm.shutil.which", lambda name: f"/usr/bin/{name}")
    llm.check_ready("codex")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")
    llm.check_ready("anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY")
    with pytest.raises(llm.LLMError, match="ANTHROPIC_API_KEY"):
        llm.check_ready("anthropic")


def test_timeout_becomes_llm_error(run: NS) -> None:
    run.result = subprocess.TimeoutExpired("claude", 1)
    with pytest.raises(llm.LLMError, match="másodperc"):
        llm.caller("claude")("p", "s", None)


# claude -p ---------------------------------------------------------------------
def test_claude_success_counts_cached_tokens(run: NS) -> None:
    usage = {"input_tokens": 5, "cache_read_input_tokens": 3, "cache_creation_input_tokens": 2, "output_tokens": 7}
    run.result = completed(json.dumps({"subtype": "success", "result": "Szia", "usage": usage}))
    result = llm.caller("claude")("prompt", "system", "opus")
    assert result.text == "Szia" and result.usage == {"input_tokens": 10, "output_tokens": 7}
    assert run.cmd[run.cmd.index("--model") + 1] == "opus" and run.stdin == "prompt"
    llm.caller("claude")("prompt", "system", None)
    assert "--model" not in run.cmd


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (completed("not json", "You've hit your usage limit"), llm.UsageLimitError),
        (completed("not json", "", 1), llm.LLMError),
        (completed(json.dumps({"is_error": True, "result": "Usage limit reached"})), llm.UsageLimitError),
        (completed(json.dumps({"subtype": "error", "api_error_status": 429})), llm.UsageLimitError),
        (completed(json.dumps({"subtype": "error", "result": "boom"})), llm.LLMError),
    ],
)
def test_claude_errors(run: NS, result: subprocess.CompletedProcess[str], error: type[llm.LLMError]) -> None:
    run.result = result
    with pytest.raises(error) as caught:
        llm.caller("claude")("p", "s", None)
    if error is llm.LLMError:
        assert not isinstance(caught.value, llm.UsageLimitError)


# codex exec ------------------------------------------------------------------
def events(*items: object) -> str:
    return "\n".join(json.dumps(e) for e in items) + "\nnot an event\n"


def test_codex_success_reads_last_message_and_usage(run: NS) -> None:
    run.last_message = "Szia"
    usage = {"input_tokens": 20, "cached_input_tokens": 5, "output_tokens": 3}
    run.result = completed(events({"type": "turn.started"}, {"type": "turn.completed", "usage": usage}))
    result = llm.caller("codex")("prompt", "system", "gpt-x")
    assert result.text == "Szia" and result.usage == {"input_tokens": 25, "output_tokens": 3}
    assert run.cmd[run.cmd.index("-m") + 1] == "gpt-x" and run.cmd[-1] == "-"
    assert run.stdin.startswith("system") and run.stdin.endswith("prompt")
    run.result = completed(events({"type": "turn.started"}))
    assert llm.caller("codex")("p", "s", None).usage == {"input_tokens": 0, "output_tokens": 0}
    assert "-m" not in run.cmd


@pytest.mark.parametrize(
    ("result", "error"),
    [
        (completed(events({"type": "error", "message": "You've hit your usage limit"}), code=1), llm.UsageLimitError),
        (completed(events({"type": "turn.failed", "error": {"message": "bad model"}})), llm.LLMError),
        (completed("", "crashed", 2), llm.LLMError),
    ],
)
def test_codex_errors(run: NS, result: subprocess.CompletedProcess[str], error: type[llm.LLMError]) -> None:
    run.result = result
    with pytest.raises(error, match=r"usage limit|bad model|crashed"):
        llm.caller("codex")("p", "s", None)


# API error mapping ---------------------------------------------------------------
def response(status: int) -> httpx2.Response:
    return httpx2.Response(status, request=httpx2.Request("POST", "https://api.example/v1"))


def raising_anthropic(error: Exception) -> Callable[..., object]:
    class Client:
        def __init__(self, **kwargs: object) -> None:
            self.beta = NS(messages=NS(stream=self.stream))

        def stream(self, **params: object) -> None:
            raise error

    return Client


@pytest.mark.parametrize(
    ("error", "expected", "match"),
    [
        (
            anthropic.BadRequestError("Your credit balance is too low", response=response(400), body=None),
            llm.UsageLimitError,
            "credit",
        ),
        (anthropic.BadRequestError("bad input", response=response(400), body=None), llm.LLMError, "bad input"),
        (anthropic.RateLimitError("slow down", response=response(429), body=None), llm.UsageLimitError, "slow"),
        (anthropic.AuthenticationError("no key", response=response(401), body=None), llm.LLMError, "API-kulcs"),
        (anthropic.APIConnectionError(request=httpx2.Request("POST", "https://x")), llm.LLMError, "Connection"),
    ],
)
def test_anthropic_error_mapping(
    monkeypatch: pytest.MonkeyPatch, error: Exception, expected: type[llm.LLMError], match: str
) -> None:
    monkeypatch.setattr(anthropic, "Anthropic", raising_anthropic(error))
    with pytest.raises(expected, match=match) as caught:
        llm.caller("anthropic")("p", "s", "claude-opus-5-5")
    if expected is llm.LLMError:
        assert not isinstance(caught.value, llm.UsageLimitError)


def raising_openai(error: Exception) -> Callable[..., NS]:
    def create(**params: object) -> None:
        raise error

    return lambda **kw: NS(chat=NS(completions=NS(create=create)))


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (openai.RateLimitError("insufficient_quota", response=response(429), body=None), llm.UsageLimitError),
        (openai.APIStatusError("Payment required", response=response(402), body=None), llm.UsageLimitError),
        (openai.APIStatusError("server error", response=response(500), body=None), llm.LLMError),
        (openai.APIConnectionError(request=httpx2.Request("POST", "https://x")), llm.LLMError),
    ],
)
def test_openai_error_mapping(monkeypatch: pytest.MonkeyPatch, error: Exception, expected: type[llm.LLMError]) -> None:
    monkeypatch.setattr(openai, "OpenAI", raising_openai(error))
    with pytest.raises(expected) as caught:
        llm.caller("openai")("p", "s", "m")
    if expected is llm.LLMError:
        assert not isinstance(caught.value, llm.UsageLimitError)


def test_openai_stream_without_content_or_cost(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = [
        NS(usage=None, choices=[NS(delta=None, finish_reason=None)]),
        NS(usage=None, choices=[NS(delta=NS(content=""), finish_reason="stop")]),
        NS(usage=NS(prompt_tokens=4, completion_tokens=0), choices=[]),
    ]
    monkeypatch.setattr(openai, "OpenAI", lambda **kw: NS(chat=NS(completions=NS(create=lambda **p: iter(chunks)))))
    result = llm.caller("openai")("p", "s", "m")
    assert result.text == "" and result.usage == {"input_tokens": 4, "output_tokens": 0}
