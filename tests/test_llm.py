"""The API providers with fake SDK clients: request shape and response handling."""

from collections.abc import Callable, Iterator
from types import SimpleNamespace as NS
from typing import Any, ClassVar, Literal

import anthropic
import openai
import pytest

from konyvhang import llm


class FakeAnthropic:
    last: ClassVar[dict[str, object]] = {}

    def __init__(self, stop_reason: str = "end_turn", **kwargs: object) -> None:
        self.stop_reason = stop_reason
        self.beta = NS(messages=NS(stream=self.stream))

    def stream(self, **params: object) -> "Stream":
        FakeAnthropic.last = params
        message = NS(
            stop_reason=self.stop_reason,
            content=[NS(type="thinking", thinking=""), NS(type="text", text='<seg id="1">Szia</seg>')],
            usage=NS(input_tokens=10, cache_read_input_tokens=5, cache_creation_input_tokens=None, output_tokens=7),
        )

        return Stream(message)


class Stream:
    def __init__(self, message: NS) -> None:
        self.message = message

    def __enter__(self) -> NS:
        return NS(get_final_message=lambda: self.message)

    def __exit__(self, *exc: object) -> Literal[False]:
        return False


def test_anthropic_streams_with_fallback_and_counts_usage(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: FakeAnthropic())
    result = llm.caller("anthropic")("prompt", "system", "claude-opus-5-5")
    assert result.text == '<seg id="1">Szia</seg>'
    assert result.usage == {"input_tokens": 15, "output_tokens": 7}
    assert FakeAnthropic.last["system"] == "system"
    assert FakeAnthropic.last["fallbacks"] == "default"
    assert FakeAnthropic.last["betas"] == ["server-side-fallback-2026-07-01"]

    llm.caller("anthropic")("prompt", "system", "claude-opus-4-8")
    assert "fallbacks" not in FakeAnthropic.last


@pytest.mark.parametrize("stop_reason", ["max_tokens", "refusal"])
def test_anthropic_incomplete_answers_are_errors(monkeypatch: pytest.MonkeyPatch, stop_reason: str) -> None:
    monkeypatch.setattr(anthropic, "Anthropic", lambda **kw: FakeAnthropic(stop_reason))
    with pytest.raises(llm.LLMError):
        llm.caller("anthropic")("prompt", "system", "claude-opus-5-5")


def fake_openai(captured: dict[str, object], finish: str = "stop") -> Callable[..., NS]:
    def create(**params: object) -> Iterator[NS]:
        captured.update(params)
        return iter(
            [
                NS(usage=None, choices=[NS(delta=NS(content="Szi"), finish_reason=None)]),
                NS(usage=None, choices=[NS(delta=NS(content="a"), finish_reason=finish)]),
                NS(usage=NS(prompt_tokens=20, completion_tokens=3, model_extra={"cost": 0.0012}), choices=[]),
            ]
        )

    def client(**kw: object) -> NS:
        captured["client"] = kw
        return NS(chat=NS(completions=NS(create=create)))

    return client


def test_openrouter_uses_its_endpoint_and_joins_the_stream(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: dict[str, Any] = {}
    monkeypatch.setenv("OPENROUTER_API_KEY", "k")
    monkeypatch.setattr(openai, "OpenAI", fake_openai(captured))
    result = llm.caller("openrouter")("prompt", "system", "vendor/model")
    assert result.text == "Szia"
    assert result.usage == {"input_tokens": 20, "output_tokens": 3, "cost_usd": 0.0012}
    assert captured["extra_body"] == {"usage": {"include": True}}
    assert captured["client"]["base_url"] == llm.OPENROUTER_URL and captured["client"]["api_key"] == "k"
    assert captured["messages"][0] == {"role": "system", "content": "system"}
    assert captured["model"] == "vendor/model"


def test_openai_truncated_answer_is_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(openai, "OpenAI", fake_openai({}, finish="length"))
    with pytest.raises(llm.LLMError, match="hosszkorlát"):
        llm.caller("openai")("prompt", "system", "some-model")
