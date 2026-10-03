# Könyvhang – guide for AI agents

Könyvhang translates EPUB books into Hungarian with a language model and reads them
aloud locally (Higgs TTS 3 on MLX) into a chaptered `.m4b`. Python 3.12+, managed with
`uv`. User-facing text (CLI output, prompts, README) is Hungarian; code, comments,
identifiers and commit messages are English.

## Commands

```
uv sync                         # dev setup (add --group tts on Apple Silicon for audio)
uv run ruff check .             # lint (fix: --fix)
uv run ruff format .            # format
uv run mypy                     # types
uv run pytest --cov             # tests; fails below 100% line+branch coverage
uv run konyvhang --help         # the CLI
```

`/check` runs every gate in order; `/smoke` runs the sample book end to end with a real
model. CI (`.github/workflows/ci.yml`) runs lint, format, mypy and the tests on Linux,
macOS and Windows with Python 3.12 and 3.13; the `CI OK` job is the required check on `main`.

## Definition of done

A change is done when all four gates pass locally, the new behaviour has tests, and the
README is updated if the user-visible behaviour changed. Coverage stays at 100%: add tests
for new lines instead of lowering the threshold or adding `# pragma: no cover` to code
that can be tested. Commits follow Conventional Commits (`feat(llm): …`, `fix: …`).

## Architecture

Data flow: EPUB → segments → chunks → model → validated translation → EPUB / audio.

| Module | Responsibility |
|---|---|
| `epub.py` | EPUB as a ZIP: container → OPF → spine; tolerant XHTML parsing (HTML entities); byte-preserving rewrite with `mimetype` first |
| `segment.py` | Finds translatable blocks (innermost block elements with letters); turns inline markup into numbered `<tag n="k">` for the model and back; `semantics()` reads `epub:type`/`role` |
| `workdir.py` | The per-book work folder (`work/<id>/`): `state.json`, `chunks/*.json`, `glossary.yaml`, atomic JSON writes, chunking (≤ 8000 words, bibliography/index left out) |
| `llm.py` | One text-in/text-out call behind a provider name: `claude` and `codex` CLIs (subscriptions), `anthropic`, `openai`, `openrouter` APIs; maps quota/credit exhaustion to `UsageLimitError` |
| `validate.py` | Parses `<seg id>` responses and checks every segment and inline tag came back |
| `glossary.py` | Book-wide glossary: extract per 40k-word slice (cached in `glossary_parts/`), then merge with Hungarian forms |
| `translate.py` | The resumable translation loop: retry once with the error, then halve the chunk; TOC/title labels at the end |
| `build.py` | Writes translations back into the original XHTML trees and assembles the Hungarian EPUB |
| `audio.py` | Audiobook: chapters from translated chunks (front matter skipped by landmarks, footnotes inlined), numbers spelled out by an LLM (`speech.json`), batched TTS + Whisper check, per-chapter `.m4a`, `.m4b` joined without re-encoding |
| `cli.py` | `konyvhang run` (whole pipeline, translation and audio in parallel) and the single-step commands |
| `prompts/*.md` | System prompts, in Hungarian; the model must return only `<seg>` elements |

## Invariants that are easy to break

- **Segment identity is positional.** A segment is `(file, idx)` in `find_segments()` order of the *original* XHTML. Never change `find_segments`, `BLOCK_TAGS` or `inline_elements` without considering existing work folders: their chunks would map to the wrong blocks.
- **Inline numbering must follow `inline_elements()` order.** `to_markup` counts while walking; never key elements by `id()` (lxml proxies are recreated and ids get reused).
- **Every file read/write passes `encoding="utf-8"`.** Windows defaults to cp1252; ruff rule `PLW1514` enforces this.
- **Writes that other processes read are atomic** (`write_json`, `to_aac`, the `.m4b`): write a temp file, then `replace`. The audio follower reads chunks while translation writes them.
- **Model output is untrusted.** Always go through `validate.parse_and_check` / `llm.parse_json`; never trust that a JSON or XML answer is complete.
- **Resumability.** Every unit of work (chunk, glossary slice, audio piece keyed by the SHA-1 of its spoken text, chapter manifest) is saved as soon as it is done, and reruns skip finished units. Keep it that way for new steps.
- **Quota exhaustion stops, it does not crash.** Raise `llm.UsageLimitError`; the loops catch it, save progress and tell the user to rerun.

## Tests

- No network, no real CLIs, no real models in tests: fake callers (see `FakeClaude` in `tests/test_pipeline.py`), monkeypatched `subprocess`, fake SDK clients (`tests/test_llm.py`), fake `mlx_audio` modules.
- `tests/conftest.py` builds a small EPUB fixture; `ffmpeg` must be on `PATH` (the m4b tests use it).
- Tests must also pass on Windows: `encoding="utf-8"`, `pathlib`, no POSIX-only paths.

## Never

- Never commit books, translations or audio (`books/`, `work/`, `voices/`, `bench/`, `*.epub` except `samples/`) – they are copyrighted or large.
- Never write API keys into files; they come from the environment (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `OPENROUTER_API_KEY`).
- Never push to `main` directly or force-push; open a pull request and let CI pass.
