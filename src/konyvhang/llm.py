"""One text-in, text-out call to a language model, behind a provider name.

Providers:
  claude      Claude Code CLI (`claude -p`), uses a Claude subscription
  codex       Codex CLI (`codex exec`), uses a ChatGPT subscription
  anthropic   Anthropic API, ANTHROPIC_API_KEY
  openai      OpenAI API, OPENAI_API_KEY
  openrouter  OpenRouter, OPENROUTER_API_KEY

A used-up subscription or credit raises UsageLimitError: the run stops and resumes later.
Short rate limits on the APIs are retried by the SDKs.
"""

import json
import os
import re
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PROVIDERS = ("claude", "codex", "anthropic", "openai", "openrouter")
# None: the provider's own default (codex) or the user must name one (openai, openrouter).
DEFAULT_MODEL = {"claude": "opus", "codex": None, "anthropic": "claude-opus-5-5", "openai": None, "openrouter": None}
# For light work (spelling out numbers); None means the book's model.
LIGHT_MODEL = {"claude": "sonnet", "anthropic": "claude-sonnet-5-5"}
API_KEYS = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "openrouter": "OPENROUTER_API_KEY"}
OPENROUTER_URL = "https://openrouter.ai/api/v1"
TIMEOUT = 1800
MAX_TOKENS = 64000  # a chunk of about 8000 words comes back as roughly 20-40k tokens of Hungarian

LIMIT_PATTERN = re.compile(
    r"usage limit|rate limit|limit reached|hit your limit|resets? at|credit balance|insufficient_quota|quota", re.I
)


class LLMError(Exception):
    pass


class UsageLimitError(LLMError):
    """The subscription quota or the API credit is used up; stop and resume later."""


@dataclass
class Result:
    text: str
    usage: dict[str, Any] = field(default_factory=dict)


Caller = Callable[[str, str, str | None], Result]


def caller(provider: str) -> Caller:
    if provider not in PROVIDERS:
        raise LLMError(f"Ismeretlen szolgáltató: {provider}. Lehetőségek: {', '.join(PROVIDERS)}.")
    return {"claude": _claude, "codex": _codex, "anthropic": _anthropic, "openai": _openai, "openrouter": _openrouter}[
        provider
    ]


def default_model(provider: str, model: str | None) -> str | None:
    model = model or DEFAULT_MODEL[provider]
    if model is None and provider in ("openai", "openrouter"):
        raise LLMError(f"A(z) {provider} szolgáltatóhoz meg kell adni a modellt a --model kapcsolóval.")
    return model


def check_ready(provider: str) -> None:
    """Fail early with a clear message when the CLI or the API key is missing."""
    if provider in ("claude", "codex") and not shutil.which(provider):
        raise LLMError(f"Nincs telepítve a `{provider}` parancs, vagy nincs a PATH-on.")
    if provider in API_KEYS and not os.environ.get(API_KEYS[provider]):
        raise LLMError(f"Hiányzik a(z) {API_KEYS[provider]} környezeti változó.")


def parse_json(text: str, opener: str) -> Any:  # noqa: ANN401 - whatever JSON value the text holds
    """The first complete JSON value starting with `opener` ("[" or "{"); text around it is ignored."""
    return json.JSONDecoder().raw_decode(text[text.index(opener) :])[0]


# subscriptions through the CLIs ----------------------------------------------
def _run(cmd: list[str], stdin: str) -> subprocess.CompletedProcess[str]:
    try:
        return subprocess.run(
            cmd, input=stdin, capture_output=True, text=True, encoding="utf-8", timeout=TIMEOUT, check=False
        )
    except subprocess.TimeoutExpired:
        raise LLMError(f"A hívás {TIMEOUT} másodperc után sem fejeződött be.") from None


def _claude(prompt: str, system: str, model: str | None) -> Result:
    cmd = [
        shutil.which("claude") or "claude",
        "-p",
        "--output-format",
        "json",
        "--system-prompt",
        system,
        "--tools",
        "",
        "--no-session-persistence",
        "--setting-sources",
        "",
    ]
    if model:
        cmd += ["--model", model]
    proc = _run(cmd, prompt)
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        message = (proc.stderr or proc.stdout).strip()
        if LIMIT_PATTERN.search(message):
            raise UsageLimitError(message) from None
        raise LLMError(f"Értelmezhetetlen claude kimenet (kilépési kód {proc.returncode}): {message[:500]}") from None

    text = data.get("result") or ""
    if data.get("is_error") or data.get("subtype") != "success":
        if LIMIT_PATTERN.search(text) or data.get("api_error_status") == 429:
            raise UsageLimitError(text)
        raise LLMError(f"A claude hibát jelzett: {text[:500]}")
    usage = data.get("usage") or {}
    return Result(
        text,
        {
            "input_tokens": usage.get("input_tokens", 0)
            + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    )


def _codex(prompt: str, system: str, model: str | None) -> Result:
    # Codex has no system prompt flag, so the instructions lead the prompt. It runs as an
    # agent: an empty read-only workspace and no user config keep it to answering.
    with tempfile.TemporaryDirectory() as workdir:
        last = Path(workdir) / "last.txt"
        cmd = [
            shutil.which("codex") or "codex",
            "exec",
            "--ephemeral",
            "--skip-git-repo-check",
            "--ignore-user-config",
            "--ignore-rules",
            "--sandbox",
            "read-only",
            "-C",
            workdir,
            "--json",
            "-o",
            str(last),
        ]
        if model:
            cmd += ["-m", model]
        stdin = f"{system}\n\nNe futtass parancsot és ne nyúlj fájlokhoz; csak a kért szöveget add vissza.\n\n{prompt}"
        proc = _run([*cmd, "-"], stdin)
        events = [json.loads(line) for line in proc.stdout.splitlines() if line.startswith("{")]
        errors = [
            e.get("message") or e.get("error", {}).get("message", "")
            for e in events
            if e.get("type") in ("error", "turn.failed")
        ]
        text = last.read_text(encoding="utf-8") if last.exists() else ""
    if proc.returncode != 0 or (errors and not text):
        message = " ".join(str(m) for m in errors) or proc.stderr.strip()
        if LIMIT_PATTERN.search(message):
            raise UsageLimitError(message)
        raise LLMError(f"A codex hibát jelzett (kilépési kód {proc.returncode}): {message[:500]}")
    usage = next((e.get("usage") for e in reversed(events) if e.get("type") == "turn.completed"), None) or {}
    return Result(
        text,
        {
            "input_tokens": usage.get("input_tokens", 0) + usage.get("cached_input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
        },
    )


# APIs ---------------------------------------------------------------------------
# Models that take the server-side refusal fallback; elsewhere the parameter is left out.
FALLBACK_MODELS = {"claude-opus-5-5", "claude-sonnet-5-5", "claude-fable-5-1"}


def _anthropic(prompt: str, system: str, model: str | None) -> Result:
    import anthropic

    client = anthropic.Anthropic(timeout=TIMEOUT, max_retries=5)
    params: dict[str, Any] = dict(
        model=model,
        max_tokens=MAX_TOKENS,
        system=system,
        messages=[{"role": "user", "content": prompt}],
        output_config={"effort": "medium"},
    )
    if model in FALLBACK_MODELS:  # a policy decline is re-run on a suitable model in the same call
        params.update(betas=["server-side-fallback-2026-07-01"], fallbacks="default")
    try:
        with client.beta.messages.stream(**params) as stream:  # streaming: long outputs, no HTTP timeout
            message = stream.get_final_message()
    except anthropic.BadRequestError as e:
        if "credit balance" in str(e).lower():
            raise UsageLimitError(str(e)) from None
        raise LLMError(str(e)) from None
    except anthropic.RateLimitError as e:  # still limited after the SDK's retries
        raise UsageLimitError(str(e)) from None
    except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as e:
        raise LLMError(f"Az API-kulcs nem érvényes vagy nincs jogosultsága: {e}") from None
    except anthropic.APIError as e:
        raise LLMError(str(e)) from None

    if message.stop_reason == "refusal":
        raise LLMError("A modell elutasította a kérést.")
    if message.stop_reason == "max_tokens":
        raise LLMError("A válasz elérte a hosszkorlátot, a darab túl nagy.")
    text = "".join(block.text for block in message.content if block.type == "text")
    usage = message.usage
    return Result(
        text,
        {
            "input_tokens": usage.input_tokens
            + (usage.cache_read_input_tokens or 0)
            + (usage.cache_creation_input_tokens or 0),
            "output_tokens": usage.output_tokens,
        },
    )


def _openai(prompt: str, system: str, model: str | None) -> Result:
    return _openai_compatible(prompt, system, model, base_url=None, key_env="OPENAI_API_KEY")


def _openrouter(prompt: str, system: str, model: str | None) -> Result:
    return _openai_compatible(prompt, system, model, base_url=OPENROUTER_URL, key_env="OPENROUTER_API_KEY")


def _openai_compatible(prompt: str, system: str, model: str | None, base_url: str | None, key_env: str) -> Result:
    import openai
    from openai.types.chat import ChatCompletionMessageParam

    client = openai.OpenAI(api_key=os.environ.get(key_env), base_url=base_url, timeout=TIMEOUT, max_retries=5)
    parts, usage, finish = [], None, None
    try:
        messages: list[ChatCompletionMessageParam] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        stream = client.chat.completions.create(
            model=model or "",
            stream=True,
            stream_options={"include_usage": True},
            messages=messages,
            # OpenRouter then reports the real price of the call in usage.cost
            extra_body={"usage": {"include": True}} if base_url == OPENROUTER_URL else None,
        )
        for chunk in stream:
            if chunk.usage:
                usage = chunk.usage
            for choice in chunk.choices:
                if choice.delta and choice.delta.content:
                    parts.append(choice.delta.content)
                finish = choice.finish_reason or finish
    except openai.RateLimitError as e:  # quota used up, or still limited after retries
        raise UsageLimitError(str(e)) from None
    except openai.APIStatusError as e:
        if e.status_code == 402 or LIMIT_PATTERN.search(str(e)):  # OpenRouter: out of credit
            raise UsageLimitError(str(e)) from None
        raise LLMError(str(e)) from None
    except openai.APIError as e:
        raise LLMError(str(e)) from None

    if finish == "length":
        raise LLMError("A válasz elérte a hosszkorlátot, a darab túl nagy.")
    counted = {
        "input_tokens": getattr(usage, "prompt_tokens", 0) or 0,
        "output_tokens": getattr(usage, "completion_tokens", 0) or 0,
    }
    cost = (getattr(usage, "model_extra", None) or {}).get("cost")
    if cost is not None:
        counted["cost_usd"] = float(cost)
    return Result("".join(parts), counted)
