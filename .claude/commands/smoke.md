---
description: End-to-end smoke test on the sample book with a real model (costs a few cents or quota)
argument-hint: "[provider] [model]"
---

Translate `samples/gift-of-the-magi.epub` end to end in a throwaway work folder and check
the result. Provider and model default to `claude` and its default model; use `$ARGUMENTS`
when given (for example `codex` or `openrouter openai/gpt-6-sol`).

```
KONYVHANG_WORK=$(mktemp -d) uv run konyvhang run samples/gift-of-the-magi.epub \
  --id smoke --profile fiction --yes --no-audio --provider <provider> [--model <model>]
```

Then verify: the run printed no `hibás válasz` or traceback, `konyvhang status smoke`
shows 1/1 chunks, and the built EPUB opens as valid XML. Report time, tokens and cost.
