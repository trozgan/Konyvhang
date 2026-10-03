"""Try Higgs TTS 3 on Hungarian text, before building the audiobook pipeline.

Without --ref, every seed gives a different voice: generate a few, listen,
and keep the best one as the reference voice with --save-voice.

    uv run --group tts scripts/tts_try.py --seeds 1 2 3 4
    uv run --group tts scripts/tts_try.py --save-voice 3 --name narrator
    uv run --group tts scripts/tts_try.py --ref voices/narrator --text-file valami.txt
"""

import argparse
import shutil
import time
from pathlib import Path
from typing import Any

MODEL = "bosonai/higgs-audio-v3-tts-4b"
OUT = Path("tts_try")
VOICES = Path("voices")

# Recommended sampling from the model card (voice cloning).
TEMPERATURE = 0.8
TOP_K = 50

SAMPLE = (
    "Huszonöt év telt el a könyv első megjelenése óta, és nagy örömünkre az olvasók "
    "továbbra is hasznosnak találják a tanácsainkat. Azóta sok minden megváltozott a világban. "
    "– Megveszi a hajamat? – kérdezte Della. "
    "– Hajat veszek – felelte a madame. – Vegye le a kalapját, hadd lám, mi van alatta. "
    "Ha neked is gondot okoznak a nehéz beszélgetések, üdv a klubban; ott leszünk, hogy fogadjunk."
)
VOICE_TEXT = (
    "Huszonöt év telt el a könyv első megjelenése óta, és nagy örömünkre az olvasók "
    "továbbra is hasznosnak találják a tanácsainkat."
)


def frames_for(text: str) -> int:
    """Generation cap: about 25 codec frames per second, generous speaking time per character."""
    return max(400, int(len(text) * 0.12 * 25))


def generate(model: Any, text: str, path: Path, seed: int | None = None, ref: Path | None = None) -> None:  # noqa: ANN401 - mlx_audio has no types
    from mlx_audio.audio_io import write as audio_write

    kwargs: dict[str, str] = {}
    if ref:
        kwargs["ref_audio"] = str(ref / "voice.wav")
        kwargs["ref_text"] = (ref / "voice.txt").read_text(encoding="utf-8").strip()
    start = time.perf_counter()
    result = next(
        model.generate(
            text=text,
            temperature=TEMPERATURE,
            top_k=TOP_K,
            seed=seed,
            max_new_tokens=frames_for(text),
            **kwargs,
        )
    )
    elapsed = time.perf_counter() - start
    audio_write(str(path), result.audio, result.sample_rate)
    duration = result.audio.shape[0] / result.sample_rate
    print(f"{path}: {duration:.1f} s hang, {elapsed:.1f} s alatt (RTF {elapsed / duration:.2f})")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--seeds", type=int, nargs="*", default=[], help="új hangok próbája, seedenként egy")
    parser.add_argument("--save-voice", type=int, help="ezt a seedet menti referenciahangnak")
    parser.add_argument("--name", default="narrator", help="a mentett hang neve")
    parser.add_argument("--ref", type=Path, help="referenciahang mappája (voice.wav + voice.txt)")
    parser.add_argument("--text", help="a felolvasandó szöveg")
    parser.add_argument("--text-file", type=Path)
    args = parser.parse_args()

    OUT.mkdir(exist_ok=True)
    text = args.text_file.read_text(encoding="utf-8") if args.text_file else (args.text or SAMPLE)

    from mlx_audio.tts import load

    t = time.perf_counter()
    model = load(MODEL)
    print(f"Modell betöltve: {time.perf_counter() - t:.1f} s")

    for seed in args.seeds:
        generate(model, VOICE_TEXT, OUT / f"voice_seed{seed}.wav", seed=seed)

    if args.save_voice is not None:
        src = OUT / f"voice_seed{args.save_voice}.wav"
        if not src.exists():
            generate(model, VOICE_TEXT, src, seed=args.save_voice)
        dest = VOICES / args.name
        dest.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dest / "voice.wav")
        (dest / "voice.txt").write_text(VOICE_TEXT + "\n", encoding="utf-8")
        print(f"Referenciahang mentve: {dest}")
        args.ref = dest

    if args.ref or args.text or args.text_file:
        name = args.ref.name if args.ref else "noref"
        generate(model, text, OUT / f"sample_{name}.wav", seed=0, ref=args.ref)


if __name__ == "__main__":
    main()
