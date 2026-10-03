"""One `claude -p` call: no tools, no settings, no session, JSON result."""

import json
import re
import subprocess
from dataclasses import dataclass, field

LIMIT_PATTERN = re.compile(r"usage limit|rate limit|limit reached|hit your limit|resets? at", re.I)


class ClaudeError(Exception):
    pass


class UsageLimitError(ClaudeError):
    """The subscription quota is used up; the run should stop and resume later."""


@dataclass
class Result:
    text: str
    usage: dict = field(default_factory=dict)


def call(prompt: str, system: str, model: str, timeout: int = 1800) -> Result:
    cmd = [
        "claude", "-p",
        "--model", model,
        "--output-format", "json",
        "--system-prompt", system,
        "--tools", "",
        "--no-session-persistence",
        "--setting-sources", "",
    ]
    try:
        proc = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise ClaudeError(f"A claude hívás {timeout} másodperc után sem fejeződött be.") from None

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError:
        message = (proc.stderr or proc.stdout).strip()
        if LIMIT_PATTERN.search(message):
            raise UsageLimitError(message)
        raise ClaudeError(f"Értelmezhetetlen claude kimenet (kilépési kód {proc.returncode}): {message[:500]}")

    text = data.get("result") or ""
    if data.get("is_error") or data.get("subtype") != "success":
        if LIMIT_PATTERN.search(text) or data.get("api_error_status") == 429:
            raise UsageLimitError(text)
        raise ClaudeError(f"A claude hibát jelzett: {text[:500]}")
    usage = data.get("usage") or {}
    return Result(
        text=text,
        usage={
            "input_tokens": usage.get("input_tokens", 0) + usage.get("cache_read_input_tokens", 0)
            + usage.get("cache_creation_input_tokens", 0),
            "output_tokens": usage.get("output_tokens", 0),
            "cost_usd": data.get("total_cost_usd", 0.0),
        },
    )


def parse_json(text: str, opener: str):
    """The first complete JSON value starting with `opener` ("[" or "{"); text around it is ignored."""
    return json.JSONDecoder().raw_decode(text[text.index(opener):])[0]
