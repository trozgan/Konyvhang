@AGENTS.md

## Claude Code specifics

- `.claude/settings.json` allows the gate commands without prompts and runs `ruff format` + `ruff check --fix` after every edit of a `.py` file (`.claude/hooks/ruff_fix.py`); remaining lint errors come back as context – fix them before moving on.
- `/check` runs all gates; `/smoke [provider] [model]` translates the sample book with a real model.
- For SDK work on the Anthropic backend in `llm.py`, load the `claude-api` skill first; model IDs and parameters change often.
