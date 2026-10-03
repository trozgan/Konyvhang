# Hozzájárulás

Köszönöm, hogy segítesz. A hibajelentés, az ötlet és a pull request is jól jön.

## Hibajelentés és ötlet

Nyiss issue-t a sablonnal. **Ne csatolj könyvet, fordítást vagy hangfájlt**: ezek
jogvédettek, és a nyilvános issue-ból bárki letöltheti őket. Ha a hiba csak egy adott
könyvnél jön elő, írd le a szerkezetét (melyik fájl, milyen elem), vagy készíts egy kicsi
EPUB-ot, amely előidézi.

Biztonsági hibát ne issue-ban jelents, hanem a [SECURITY.md](SECURITY.md) szerint.

## Pull request

1. Nagyobb változtatás előtt nyiss issue-t, hogy egyeztessünk az irányról.
2. Forkold a repót, és dolgozz külön ágon. A `main` ágra közvetlenül nem lehet pusholni.
3. Telepítsd a fejlesztői környezetet és a commit előtti ellenőrzéseket:

   ```
   uv sync
   uv run pre-commit install
   ```

4. A PR akkor kész, ha minden ellenőrzés átmegy:

   ```
   uv run ruff check .
   uv run ruff format --check .
   uv run mypy
   uv run pytest --cov      # 100% sor- és áglefedettség alatt elbukik
   ```

   Az új viselkedéshez teszt kell. Ha a felhasználó által látható viselkedés változik,
   frissítsd a README-t is.
5. A commitüzenet a [Conventional Commits](https://www.conventionalcommits.org/) formát
   követi, angolul: `feat(llm): add retry for rate limits`, `fix(audio): …`, `docs: …`.

A kód felépítését, a könnyen elrontható szabályokat (például hogy a szegmensek azonosítása
pozíció szerinti) és a tesztelés szabályait az [AGENTS.md](AGENTS.md) írja le. Ez nemcsak
AI-ügynököknek szól, hanem minden fejlesztőnek.

A tesztek nem használnak hálózatot, valódi CLI-t vagy modellt, és Windowson is át kell
menniük. A felhasználónak szóló szöveg (CLI-kimenet, promptok, README) magyar; a kód, a
megjegyzések és a commitüzenetek angolok.

## Licenc

A hozzájárulásod a projekt [MIT-licence](LICENSE) alá kerül.
