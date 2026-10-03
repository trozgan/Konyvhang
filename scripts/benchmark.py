"""Translate a sample book with several models and record time, tokens and price.

    uv run scripts/benchmark.py translate openrouter:anthropic/claude-opus-5.5 claude:opus ...
    uv run scripts/benchmark.py judge      # blind quality scores from two judges
    uv run scripts/benchmark.py report     # Markdown table for the README

--profile nonfiction (before the command) measures the non-fiction sample instead of the
fiction one. Every model runs the whole pipeline (glossary, translation, TOC labels) in its
own work folder under bench/<profile>/, in parallel. Results go to bench/<profile>/results.json.
"""

import argparse
import json
import os
import random
import re
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from konyvhang import llm  # noqa: E402
from konyvhang.segment import plain_text  # noqa: E402
from konyvhang.workdir import WorkDir  # noqa: E402

JUDGE_FORMAT = """Légy szigorú és következetes: 10 csak kiadható, kiváló fordítás, 5 érthető, de sok hibával.
Csak egy JSON-objektumot adj vissza, kulcsa a fordítás betűjele:
{{"A": {{{example}, "megjegyzes": "egy mondat"}}, ...}}"""

JUDGE_FICTION = """Irodalmi fordítások értékelője vagy. Egy angol novella részletét és annak több névtelen magyar
fordítását kapod. Mindegyik fordítást önállóan pontozd 1-től 10-ig, négy szempont szerint:

- pontosság: a jelentés hiánytalan és helyes, nincs kihagyás vagy félrefordítás;
- gördülékenység: természetes, magyaros, nem fordításízű szöveg;
- stílus: a szerző hangja, iróniája, szójátékai, a párbeszédek magyar írásmódja (gondolatjel);
- egységesség: nevek, megszólítás (tegezés/magázás), visszatérő kifejezések következetesek.
"""

JUDGE_NONFICTION = """Ismeretterjesztő szövegek fordításainak értékelője vagy. Egy angol tudományos előadás részletét
és annak több névtelen magyar fordítását kapod, lábjegyzetekkel együtt. Mindegyik fordítást önállóan
pontozd 1-től 10-ig, négy szempont szerint:

- pontosság: a jelentés hiánytalan és helyes; számok, mértékek, nevek, címek és hivatkozások nem
  változnak, nincs kihagyás vagy félrefordítás;
- gördülékenység: természetes, magyaros, nem fordításízű szöveg, amely megtartja az előadás élőszó-jellegét;
- szakszavak: a szakkifejezések a magyar szakirodalomban használt alakjukban szerepelnek, a latin
  tudományos nevek megmaradnak, a magyarázatok szakmailag helyesek;
- egységesség: ugyanaz a fogalom végig ugyanazzal a szóval szerepel, a hallgatóság megszólítása következetes.
"""


@dataclass(frozen=True)
class Profile:
    sample: Path
    judge_system: str
    criteria: tuple[str, ...]


PROFILES = {
    "fiction": Profile(
        ROOT / "samples" / "gift-of-the-magi.epub",
        JUDGE_FICTION,
        ("pontossag", "gordulekenyseg", "stilus", "egysegesseg"),
    ),
    "nonfiction": Profile(
        ROOT / "samples" / "on-a-piece-of-chalk.epub",
        JUDGE_NONFICTION,
        ("pontossag", "gordulekenyseg", "szakszavak", "egysegesseg"),
    ),
}
PROFILE = "fiction"  # set from --profile before any command runs


def bench() -> Path:
    return ROOT / "bench" / PROFILE


def judge_system() -> str:
    criteria = PROFILES[PROFILE].criteria
    example = ", ".join(f'"{key}": {score}' for key, score in zip(criteria, (9, 8, 8, 9), strict=True))
    return PROFILES[PROFILE].judge_system + "\n" + JUDGE_FORMAT.format(example=example)


def slug(spec: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", spec.lower()).strip("-")


def load() -> dict[str, Any]:
    results = bench() / "results.json"
    return json.loads(results.read_text(encoding="utf-8")) if results.exists() else {}


def save(results: dict[str, Any]) -> None:
    bench().mkdir(parents=True, exist_ok=True)
    (bench() / "results.json").write_text(json.dumps(results, ensure_ascii=False, indent=1), encoding="utf-8")


def translate_one(spec: str) -> dict[str, Any]:
    provider, _, model = spec.partition(":")
    book = slug(spec)
    log = bench() / f"{book}.log"
    cmd = [
        "uv",
        "run",
        "konyvhang",
        "run",
        str(PROFILES[PROFILE].sample),
        "--id",
        book,
        "--profile",
        PROFILE,
        "--provider",
        provider,
        "--yes",
        "--no-audio",
    ] + (["--model", model] if model else [])
    env = {**os.environ, "KONYVHANG_WORK": str(bench() / "work")}
    start = time.time()
    with log.open("w", encoding="utf-8") as out:
        code = subprocess.run(cmd, cwd=ROOT, env=env, stdout=out, stderr=subprocess.STDOUT, check=False).returncode
    seconds = time.time() - start
    wd = WorkDir(bench() / "work" / book)
    usage = wd.load_state().get("usage", {}) if wd.exists() else {}
    chunks = wd.load_chunks() if wd.exists() else []
    text = log.read_text(encoding="utf-8")
    return {
        "provider": provider,
        "model": model or "(alapértelmezett)",
        "book": book,
        "ok": code == 0 and all(c["translation"] for c in chunks),
        "seconds": round(seconds),
        "calls": usage.get("calls", 0),
        "input_tokens": usage.get("input_tokens", 0),
        "output_tokens": usage.get("output_tokens", 0),
        "cost_usd": usage.get("cost_usd"),
        "retries": text.count("hibás válasz"),
        "splits": text.count("kettévágás"),
    }


def cmd_translate(specs: list[str]) -> None:
    bench().mkdir(parents=True, exist_ok=True)
    results = load()
    with ThreadPoolExecutor(max_workers=len(specs)) as pool:
        for spec, result in zip(specs, pool.map(translate_one, specs), strict=True):
            results[spec] = {**results.get(spec, {}), **result}
            save(results)
            print(
                f"{spec}: {'kész' if result['ok'] else 'HIBA'} {result['seconds']} s, "
                f"${result['cost_usd'] or 0:.4f}, újrapróbálás {result['retries']}",
                flush=True,
            )


def translation_text(book: str) -> str:
    wd = WorkDir(bench() / "work" / book)
    return "\n\n".join(plain_text(m) for c in wd.load_chunks() for m in c["translation"])


def cmd_judge(judges: list[str]) -> None:
    """Blind: the judge sees letters, not model names, in a shuffled order."""
    results = load()
    done = [s for s, r in results.items() if r.get("ok")]
    source = "\n\n".join(
        plain_text(s["src"])
        for c in WorkDir(bench() / "work" / results[done[0]]["book"]).load_chunks()
        for s in c["segments"]
    )
    for judge in judges:
        provider, _, model = judge.partition(":")
        order = done[:]
        random.Random(judge).shuffle(order)  # noqa: S311 - a repeatable blind order, not a secret
        letters = {chr(65 + i): spec for i, spec in enumerate(order)}
        prompt = f"<eredeti>\n{source}\n</eredeti>\n\n" + "\n\n".join(
            f'<forditas betu="{letter}">\n{translation_text(results[spec]["book"])}\n</forditas>'
            for letter, spec in letters.items()
        )
        print(f"Bírálat: {judge} ({len(letters)} fordítás)", flush=True)
        answer = llm.parse_json(llm.caller(provider)(prompt, judge_system(), model or None).text, "{")
        for letter, spec in letters.items():
            scores = answer.get(letter, {})
            results[spec].setdefault("judges", {})[judge] = scores
        save(results)


PAGE_WORDS = 250  # a typical printed page
SUBSCRIPTIONS = {"claude": "Claude-előfizetés", "codex": "ChatGPT-előfizetés"}


def cmd_report() -> None:
    results = load()
    words = sum(
        len(plain_text(s["src"]).split())
        for c in WorkDir(bench() / "work" / next(iter(results.values()))["book"]).load_chunks()
        for s in c["segments"]
    )
    pages = words / PAGE_WORDS
    keys = PROFILES[PROFILE].criteria
    rows = []
    for spec, r in results.items():
        judged = [j for j in r.get("judges", {}).values() if j]
        score = sum(sum(j.get(k, 0) for k in keys) / 4 for j in judged) / len(judged) if judged else 0
        rows.append((score, spec, r, len(judged)))
    rows.sort(key=lambda row: -row[0])
    print(f"Mérés: {words} szó (kb. {pages:.0f} oldal), {rows[0][3]} bíráló átlaga.\n")
    print("| Modell | Minőség (1–10) | Ár / 10 oldal | Ár / 300 oldal (becslés) | Idő |")
    print("|---|---|---|---|---|")
    for score, spec, r, _ in rows:
        provider, _, model = spec.partition(":")
        name = f"`{model}`" + (f" ({SUBSCRIPTIONS[provider]})" if provider in SUBSCRIPTIONS else "")
        if provider in SUBSCRIPTIONS:
            price = book = "előfizetés"
        else:
            per_page = (r.get("cost_usd") or 0) / pages
            price, book = f"${per_page * 10:.3f}", f"${per_page * 300:.2f}"
        label = name if model else f"`{provider}` alapértelmezett modell"
        print(f"| {label} | {score:.1f} | {price} | {book} | {r['seconds']} s |")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Modellek összemérése a mintakönyvön.")
    parser.add_argument("--profile", choices=sorted(PROFILES), default="fiction")
    parser.add_argument("command", nargs="?", choices=["translate", "judge", "report"], default="report")
    parser.add_argument("specs", nargs="*", help="szolgáltató:modell, például openrouter:openai/gpt-6-sol")
    args = parser.parse_args()
    PROFILE = args.profile
    {
        "translate": lambda: cmd_translate(args.specs),
        "judge": lambda: cmd_judge(args.specs or ["claude:opus", "codex:"]),
        "report": cmd_report,
    }[args.command]()
