## Mit és miért

<!-- Egy-két mondat: mit változtat a PR, és miért. -->

## Ellenőrzés

- [ ] `uv run ruff check . && uv run ruff format --check .`
- [ ] `uv run mypy`
- [ ] `uv run pytest --cov` (100% sor- és áglefedettség)
- [ ] Ha a fordítást vagy a felolvasást érinti: kipróbálva a `samples/gift-of-the-magi.epub` könyvön
