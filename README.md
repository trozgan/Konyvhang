# Könyvhang

EPUB-könyvek fordítása magyarra a `claude -p` headless móddal, a Claude-előfizetés
keretéből, és hangoskönyv készítése belőlük helyben, a Higgs TTS 3 modellel.

A lefordított könyvet csak saját használatra készítsd el, ne terjeszd tovább.

## Telepítés

```
uv sync
```

Kell hozzá a bejelentkezett `claude` parancs. Ha az `epubcheck` telepítve van
(`brew install epubcheck`), a `build` lefuttatja a kész könyvön.

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
- A számokat egyszer Claude-dal betűvel kiíratja (`speech.json`).
- 8-as kötegekben generál (M4 Pro-n kb. négyszer gyorsabb a valós időnél), és minden darabot Whisperrel visszaellenőriz; a gyenge darabokat egyenként újragenerálja, ami így sem jó, az a `report.json`-ba kerül.
- Kimenet: fejezetenként `.m4a`, a végén borítós, fejezetjeles `.m4b` a `work/<könyv>/out/` mappában.

## Tesztek

```
uv run pytest
```

## Licencek

- A kód MIT-licencű, lásd a [LICENSE](LICENSE) fájlt.
- A modelleknek saját licencük van, ezeket mindenki maga tölti le: a Higgs TTS 3
  ([bosonai/higgs-audio-v3-tts-4b](https://huggingface.co/bosonai/higgs-audio-v3-tts-4b))
  kutatási és nem kereskedelmi licencű, a Whisper
  ([mlx-community/whisper-large-v3-turbo-asr-fp16](https://huggingface.co/mlx-community/whisper-large-v3-turbo-asr-fp16)) MIT.
- A fordításhoz a saját Claude-előfizetésed kell, a `claude` parancssori eszközzel bejelentkezve.
- A lefordított könyvet és a hangoskönyvet csak saját használatra készítsd el, ne terjeszd tovább.
