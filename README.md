# Könyvhang

EPUB-könyvek fordítása magyarra nagy nyelvi modellel, és hangoskönyv készítése belőlük
helyben, a Higgs TTS 3 modellel.

A lefordított könyvet csak saját használatra készítsd el, ne terjeszd tovább.

> *In English:* Könyvhang translates EPUB books into Hungarian with a language model
> (Claude, Codex, or any model via the Anthropic, OpenAI or OpenRouter APIs) and reads
> them aloud locally into a chaptered `.m4b` audiobook (Higgs TTS 3 on MLX, Apple Silicon).
> Runs are resumable and every model answer is validated. The docs are in Hungarian.

## Telepítés

```
git clone https://github.com/trozgan/Konyvhang.git
cd Konyvhang
uv sync
```

Kell hozzá a [`uv`](https://docs.astral.sh/uv/) és egy modellszolgáltató: előfizetéssel
a bejelentkezett `claude` vagy `codex` parancs, vagy egy API-kulcs (lásd lent). A fordítás
Linuxon, macOS-en és Windowson is fut; a hangoskönyvhöz Apple Silicon kell. Ha az
`epubcheck` telepítve van (`brew install epubcheck`), a `build` lefuttatja a kész könyvön.

## Gyors indulás

Előfizetés nélkül, OpenRouter API-kulccsal, csak fordítás:

```
export OPENROUTER_API_KEY=...
uv run konyvhang run könyv.epub --profile fiction --provider openrouter --model openai/gpt-6-sol --no-audio
```

A program elkészíti a szójegyzéket, és megáll, hogy átnézd (`work/<könyv>/glossary.yaml`).
Utána lefordítja a könyvet, a kész EPUB a `work/<könyv>/out/` mappába kerül. Ha bármikor
megszakad, ugyanezzel a paranccsal folytatható. Kipróbálni a mintakönyvvel lehet:
`samples/gift-of-the-magi.epub`.

## Modellszolgáltató

A fordítás ezek közül bármelyikkel mehet (`--provider`):

| Szolgáltató | Mit használ | Kell hozzá |
|---|---|---|
| `claude` (alapértelmezett) | Claude-előfizetés a Claude Code CLI-n át (`claude -p`) | telepített, bejelentkezett `claude` |
| `codex` | ChatGPT-előfizetés a Codex CLI-n át (`codex exec`) | telepített, bejelentkezett `codex` |
| `anthropic` | Anthropic API | `ANTHROPIC_API_KEY` |
| `openai` | OpenAI API | `OPENAI_API_KEY` és `--model` |
| `openrouter` | OpenRouter, bármelyik ott elérhető modell | `OPENROUTER_API_KEY` és `--model` |

A modell a `--model` kapcsolóval választható; alapértelmezés: `claude` → `opus`,
`anthropic` → `claude-opus-5-5`, `codex` → a Codex saját alapértelmezése. A választás a
könyvvel együtt elmentődik. Egy elkezdett könyvnél a `konyvhang run … --provider <más>`
átállítja a szolgáltatót, és onnan folytatja, például ha az egyik előfizetés kerete elfogyott.

## Melyik modellel fordíts?

Mérés a repóban lévő mintakönyvön ([`samples/gift-of-the-magi.epub`](samples/gift-of-the-magi.epub),
O. Henry: *The Gift of the Magi*, közkincs, 2062 szó, kb. 8 könyvoldal), szépirodalmi
profillal, a teljes folyamattal: szójegyzék, fordítás, tartalomjegyzék. Az árak az
OpenRouter által számlázott valódi költségek (2026. október), 10 oldalra és egy 300
oldalas könyvre vetítve. A minőség három különböző gyártó modelljének (Claude Opus,
GPT a Codexen át, Gemini 3.1 Pro) vak pontozásának átlaga: a bírálók betűjeleket láttak,
nem modellneveket, és pontosságot, gördülékenységet, stílust és egységességet pontoztak.

| Modell | Minőség (1–10) | Ár / 10 oldal | Ár / 300 oldal (becslés) | Idő (8 oldal) |
|---|---|---|---|---|
| `openai/gpt-6-astra` | 9.1 | $0.76 | ~$23 | 154 s |
| Codex, ChatGPT-előfizetéssel (alapértelmezett modell) | 9.1 | előfizetés | előfizetés | 359 s |
| `openai/gpt-6-sol` | 8.7 | $0.13 | ~$4.0 | 94 s |
| `google/gemini-3.8-flash` | 8.5 | $0.05 | ~$1.5 | 69 s |
| `anthropic/claude-opus-5.5` | 8.1 | $0.39 | ~$12 | 149 s |
| `qwen/qwen3.7-max` | 8.1 | $0.17 | ~$5.0 | 267 s |
| Claude Opus, Claude-előfizetéssel | 7.8 | előfizetés | előfizetés | 229 s |
| `openai/gpt-6-luna` | 7.6 | $0.008 | ~$0.25 | 78 s |
| `google/gemini-3.1-pro-preview` | 7.2 | $0.43 | ~$13 | 202 s |
| `x-ai/grok-4.7` | 7.2 | $0.38 | ~$11 | 666 s |
| `anthropic/claude-sonnet-5.5` | 6.6 | $0.22 | ~$6.7 | 128 s |
| `moonshotai/kimi-k3` | 6.1 | $0.60 | ~$18 | 470 s |
| `deepseek/deepseek-v4.1-flash` | 5.3 | $0.04 | ~$1.2 | 448 s |
| `deepseek/deepseek-v4-pro` | 5.2 | $0.06 | ~$1.8 | 505 s |

Mind a 14 modell hibátlan szerkezetű fordítást adott, újrapróbálás nélkül. Kézi
szúrópróbánál a DeepSeek 4.1 Flash és a Kimi K3 angol nagykötőjelet (—) használt a magyar
gondolatjel helyett, a Gemini 3.1 Pro pedig egy ritka kötőjelet (‒); a Kimi szövegében
elírás is volt.

**Javaslat:**

- **Ha van előfizetésed**, használd azt (`--provider claude` vagy `--provider codex`): nem
  kerül külön pénzbe, csak a keretből fogy, és a minősége a legjobbak között van.
- **API-ból a legjobb ár-érték arány:** `openai/gpt-6-sol` (kb. 4 dollár egy 300 oldalas
  könyvre), vagy ha az ár a fő szempont, `google/gemini-3.8-flash` (kb. 1,5 dollár).
- **A legjobb minőség:** `openai/gpt-6-astra`, de ez könyvenként 20 dollár fölött van.

**A mérés korlátai:** egyetlen, 8 oldalas szépirodalmi szövegen készült, és modellenként
egyszer futott. Ugyanaz a Claude Opus az API-n 8,1, az előfizetésen át 7,8 pontot kapott,
vagyis kb. fél pont eltérés a véletlen szórás. Szakkönyvnél, más szerzőnél más lehet a
sorrend. A 300 oldalas ár egyenes arányos becslés. A mérés megismételhető:

```
OPENROUTER_API_KEY=... uv run scripts/benchmark.py translate openrouter:openai/gpt-6-sol claude:opus
uv run scripts/benchmark.py judge     # vak pontozás: claude:opus és codex
uv run scripts/benchmark.py report    # a fenti táblázat
```

## Használat

Egy paranccsal, az elejétől a végéig:

```
uv run --group tts konyvhang run könyv.epub --profile nonfiction --skip <ami-nem-kell>
```

Ez elkészíti a szójegyzéket, megáll, amíg átnézed, aztán együtt futtatja a fordítást
és a felolvasást, végül összeállítja a magyar EPUB-ot és a fejezetjeles `.m4b`-t
(`work/<könyv>/out/`). Ha bármikor megáll (Ctrl+C, elfogyott keret), ugyanezzel a
paranccsal folytatható. A `--no-audio` csak fordít, a `--yes` nem áll meg a
szójegyzéknél. Az irodalomjegyzéket és a tárgymutatót (ha a kiadó `epub:type`-pal
jelöli őket) nem fordítja és nem olvassa fel; a `--references` kapcsolóval ezeket is
lefordítja.

Lépésenként:

```
uv run konyvhang prepare könyv.epub --profile fiction    # vagy: --profile nonfiction
# nézd át és javítsd: work/<könyv>/glossary.yaml
uv run konyvhang translate <könyv>                       # megszakítható, ugyanígy folytatható
uv run konyvhang build <könyv>                           # → work/<könyv>/out/<könyv>.hu.epub
uv run konyvhang status <könyv>
```

- `translate --max-chunks 1`: csak egy darabot fordít, próbának.
- `build --partial`: a még lefordítatlan részek angolul maradnak.
- `glossary <könyv>`: a szójegyzék újrakészítése.
- A munkamappák alapból a `./work` alatt vannak; a `KONYVHANG_WORK` környezeti változóval máshová tehetők.

## Hangoskönyv

A felolvasás helyben fut, a Higgs TTS 3 modellel (MLX, Apple Silicon). Telepítés: `uv sync --group tts`.

```
uv run --group tts scripts/tts_try.py --seeds 1 2 3 4         # hangpróbák
uv run --group tts scripts/tts_try.py --save-voice 4           # → voices/narrator
uv run --group tts konyvhang audio <könyv> --follow              # a fordítással együtt halad
```

- A borítót, a címoldalt, a tartalomjegyzéket és a kolofont a kiadói jelölések alapján kihagyja; ami nincs jelölve, azt a `--skip <fájlnév-részlet>` kapcsolóval lehet kihagyni.
- A lábjegyzeteket a hivatkozó bekezdés után olvassa fel.
- A számokat és a római számokat egyszer Claude-dal betűvel kiíratja (`speech.json`). Az oldalszám-hivatkozásokat, a hivatkozások évszámait, a kitöltendő vonalakat és a szakaszszámokat („4.5. Babanevelés”) nem olvassa fel.
- Az egymást követő, legfeljebb háromszavas, mondatvég nélküli sorokat (táblázatcellák, címkék) egy felsorolásként olvassa fel, mert külön a hang rosszul ejti ki őket.
- 8-as kötegekben generál (M4 Pro-n kb. négyszer gyorsabb a valós időnél), és minden darabot Whisperrel visszaellenőriz (a Whisper számjegyeit betűvel hasonlítja össze); a gyenge darabokat egyenként újragenerálja, ami így sem jó, az a `report.json`-ba kerül.
- A már legenerált hangdarabok megmaradnak: ha a felolvasási szabályok változnak, csak a még hiányzó részek készülnek az új szabályokkal.
- Kimenet: fejezetenként `.m4a`, a végén borítós, fejezetjeles `.m4b` a `work/<könyv>/out/` mappában.

## Fejlesztés

```
uv sync                         # fejlesztői környezet
uv run pre-commit install       # egyszer klónonként: commit előtt ruff, mypy és fájlellenőrzések
uv run ruff check .             # lint (bandit-biztonsági és típusannotációs szabályokkal)
uv run ruff format .            # formázás
uv run mypy                     # típusellenőrzés, --strict: src, scripts és tests
uv run pytest --cov             # tesztek, 100% sor- és áglefedettség alatt elbukik
```

A CI ugyanezeket futtatja Linuxon, macOS-en és Windowson, emellett `pip-audit`-tal
ellenőrzi a zárolt függőségeket, a CodeQL pedig biztonsági elemzést végez.

Hozzájárulás előtt olvasd el a [CONTRIBUTING.md](CONTRIBUTING.md) fájlt; biztonsági hibát a
[SECURITY.md](SECURITY.md) szerint jelents.

## Licencek

- A kód MIT-licencű, lásd a [LICENSE](LICENSE) fájlt.
- A modelleknek saját licencük van, ezeket mindenki maga tölti le: a Higgs TTS 3
  ([bosonai/higgs-audio-v3-tts-4b](https://huggingface.co/bosonai/higgs-audio-v3-tts-4b))
  kutatási és nem kereskedelmi licencű, a Whisper
  ([mlx-community/whisper-large-v3-turbo-asr-fp16](https://huggingface.co/mlx-community/whisper-large-v3-turbo-asr-fp16)) MIT.
- A fordításhoz a saját előfizetésed vagy API-kulcsod kell; a használat a választott szolgáltató feltételei szerint történik.
- A lefordított könyvet és a hangoskönyvet csak saját használatra készítsd el, ne terjeszd tovább.
