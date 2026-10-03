"""PostToolUse hook: format and lint-fix a Python file right after Claude edits it.

Reads the hook payload (JSON) on stdin. Non-Python files are ignored. Remaining lint
errors are reported back to Claude as context so it can fix them in the same turn.
"""

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def ruff(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["uv", "run", "--quiet", "ruff", *args], cwd=ROOT, capture_output=True, text=True, encoding="utf-8", check=False
    )


def main() -> None:
    payload = json.load(sys.stdin)
    path = (payload.get("tool_input") or {}).get("file_path") or ""
    if not path.endswith(".py"):
        return
    ruff(["format", path])
    lint = ruff(["check", "--fix", path])
    if lint.returncode != 0:
        output = {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"ruff talált javítandót ebben: {path}\n{lint.stdout.strip()}",
            }
        }
        print(json.dumps(output, ensure_ascii=False))


if __name__ == "__main__":
    main()
