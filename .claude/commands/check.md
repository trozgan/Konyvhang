---
description: Run every quality gate the CI runs (lint, format, types, tests with 100% coverage)
---

Run these in order and stop at the first failure; fix the cause, then rerun from that gate:

1. `uv run ruff check .`
2. `uv run ruff format --check .`
3. `uv run mypy`
4. `uv run pytest --cov`

Report each gate's result in one line. If coverage drops below 100%, add tests for the
missing lines (`--cov-report=term-missing` shows them); do not lower the threshold and do
not add `# pragma: no cover` to code that can be tested.
