"""Command line: run (everything), prepare, glossary, translate, build, audio, status."""

import argparse
import importlib.util
import os
import re
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from . import build, glossary, llm, translate
from .workdir import WorkDir


def work_root() -> Path:
    return Path(os.environ.get("KONYVHANG_WORK", "work"))


def slug(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "-", name).strip("-").lower() or "konyv"


def open_book(book_id: str) -> WorkDir:
    wd = WorkDir(work_root() / book_id)
    if not wd.exists():
        sys.exit(f"Nincs ilyen könyv: {wd.root}. Előbb futtasd a prepare parancsot.")
    return wd


def check_provider(provider: str) -> None:
    try:
        llm.check_ready(provider)
    except llm.LLMError as e:
        sys.exit(str(e))


def choose_model(provider: str, model: str | None) -> str | None:
    """The model for this provider, after checking that the provider can be reached at all."""
    check_provider(provider)
    try:
        return llm.default_model(provider, model)
    except llm.LLMError as e:
        sys.exit(str(e))


def prepare(src: Path, profile: str, provider: str, model: str | None, book_id: str | None, force: bool,
            references: bool = False) -> WorkDir:
    model = choose_model(provider, model)  # before anything is written
    wd = WorkDir(work_root() / (book_id or slug(src.stem)))
    if wd.exists() and not force:
        sys.exit(f"Már létezik: {wd.root}. A --force kapcsolóval újrakezdheted.")
    if wd.root.exists():
        shutil.rmtree(wd.root)
    wd.root.mkdir(parents=True)
    shutil.copyfile(src, wd.source)
    wd.save_state({"profile": profile, "provider": provider, "model": model, "source_name": src.name})
    count = wd.write_chunks(references)
    print(f"{wd.book_id}: {count} darab elkészült."
          + ("" if references else " Az irodalomjegyzék és a tárgymutató angolul marad."))
    return wd


def build_glossary(wd: WorkDir) -> None:
    try:
        glossary.build(wd)
    except llm.UsageLimitError as e:
        sys.exit(f"Elfogyott a keret. Folytatás később ugyanezzel a paranccsal.\n{e}")


def cmd_prepare(args) -> None:
    wd = prepare(Path(args.epub), args.profile, args.provider, args.model, args.id, args.force, args.references)
    if args.no_glossary:
        return
    build_glossary(wd)
    print(f"Nézd át és javítsd a szójegyzéket, mielőtt fordítasz: {wd.glossary_path}")


def cmd_run(args) -> None:
    """The whole pipeline in one command; rerunning it continues where it stopped."""
    src = Path(args.epub)
    wd = WorkDir(work_root() / (args.id or slug(src.stem)))
    if not wd.exists():
        if not args.profile:
            sys.exit("Új könyvnél meg kell adni a --profile kapcsolót (fiction vagy nonfiction).")
        wd = prepare(src, args.profile, args.provider or "claude", args.model, args.id, force=False,
                     references=args.references)
    elif args.provider or args.model:  # switch an existing book, e.g. when one subscription runs out
        state = wd.load_state()
        state["provider"] = args.provider or state.get("provider", "claude")
        state["model"] = choose_model(state["provider"], args.model)
        wd.save_state(state)
        print(f"Szolgáltató: {state['provider']}, modell: {state['model'] or 'alapértelmezett'}")
    else:
        check_provider(wd.load_state().get("provider", "claude"))

    if not wd.glossary_path.exists():
        build_glossary(wd)
        if not args.yes:
            print(f"\nNézd át és javítsd a szójegyzéket: {wd.glossary_path}")
            input("Ha kész vagy, nyomj Entert a fordítás indításához… ")

    audio_proc = None
    if args.no_audio:
        pass
    elif importlib.util.find_spec("mlx_audio") is None:
        print("A hangoskönyvhöz kell a tts csoport: uv run --group tts konyvhang run … (most kimarad)")
    else:
        cmd = [sys.executable, "-u", "-m", "konyvhang", "audio", wd.book_id, "--follow", "--voice", args.voice]
        if args.skip:
            cmd += ["--skip", *args.skip]
        audio_proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        threading.Thread(target=prefix_lines, args=(audio_proc.stdout, "[hang] "), daemon=True).start()

    done = translate.run(wd, log=lambda m: print(f"[fordítás] {m}", flush=True))
    if done:
        build.build(wd, log=lambda m: print(f"[fordítás] {m}", flush=True))
    else:
        print("[fordítás] A fordítás megállt; folytatás később ugyanezzel a paranccsal.")
    if audio_proc:
        if not done:
            print("[hang] A felolvasás a már lefordított részt még befejezi, aztán vár. Ctrl+C-vel leállítható.")
        if audio_proc.wait() != 0:
            sys.exit(f"[hang] A felolvasás hibával állt le (kilépési kód {audio_proc.returncode}). "
                     "Ugyanezzel a paranccsal folytatható.")
    print(f"Kész. Az eredmény itt van: {wd.out_dir}")


def prefix_lines(stream, prefix: str) -> None:
    for line in stream:
        line = line.rstrip()
        if line and not ignored_audio_line(line):
            print(prefix + line, flush=True)


def ignored_audio_line(line: str) -> bool:
    """Library noise from the TTS stack: warnings and download progress bars."""
    return bool(re.search(r"warn|Fetching \d+ files|kernel = np\.where", line, re.I))


def cmd_glossary(args) -> None:
    build_glossary(open_book(args.id))


def cmd_translate(args) -> None:
    wd = open_book(args.id)
    if not wd.glossary_path.exists():
        print("Figyelem: nincs szójegyzék, a fordítás enélkül indul.")
    if translate.run(wd, max_chunks=args.max_chunks):
        print(f"Minden darab kész. Összeállítás: konyvhang build {wd.book_id}")


def cmd_build(args) -> None:
    build.build(open_book(args.id), partial=args.partial)


def cmd_audio(args) -> None:
    from . import audio  # needs the tts dependency group

    (audio.follow if args.follow else audio.run)(open_book(args.id), Path(args.voice), args.skip)


def cmd_status(args) -> None:
    wd = open_book(args.id)
    state = wd.load_state()
    chunks = wd.load_chunks()
    done = sum(c["translation"] is not None for c in chunks)
    usage = state.get("usage", {})
    print(f"{wd.book_id} ({state['source_name']}), profil: {state['profile']}, "
          f"szolgáltató: {state.get('provider', 'claude')}, modell: {state['model'] or 'alapértelmezett'}")
    print(f"Szójegyzék: {'van' if wd.glossary_path.exists() else 'nincs'}")
    print(f"Lefordítva: {done}/{len(chunks)} darab")
    print(
        f"Felhasználás: {usage.get('calls', 0)} hívás, {usage.get('input_tokens', 0):,} bemeneti és "
        f"{usage.get('output_tokens', 0):,} kimeneti token"
        + (f", ${usage['cost_usd']:.4f}" if usage.get("cost_usd") else "")
    )


def main() -> None:
    parser = argparse.ArgumentParser(prog="konyvhang", description="EPUB-könyvek fordítása magyarra és hangoskönyv készítése belőlük.")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("run", help="minden egyben: szójegyzék, átnézés, fordítás és felolvasás együtt, EPUB, m4b")
    p.add_argument("epub")
    p.add_argument("--profile", choices=["fiction", "nonfiction"], help="új könyvnél kötelező")
    p.add_argument("--provider", choices=llm.PROVIDERS, default="claude", help="claude, codex (előfizetés) vagy anthropic, openai, openrouter (API-kulcs)")
    p.add_argument("--model", help="a modell neve (claude: opus, anthropic: claude-opus-5-5; openai és openrouter: kötelező)")
    p.add_argument("--id", help="a munkamappa neve (alapból a fájlnévből)")
    p.add_argument("--voice", default="voices/narrator", help="referenciahang mappája")
    p.add_argument("--skip", nargs="*", default=[], help="ezeket a fájlnév-részleteket nem olvassa fel")
    p.add_argument("--no-audio", action="store_true", help="hangoskönyv nélkül")
    p.add_argument("--references", action="store_true",
                   help="az irodalomjegyzéket és a tárgymutatót is lefordítja (alapból angolul maradnak)")
    p.add_argument("--yes", action="store_true", help="nem áll meg a szójegyzék átnézésére")
    p.set_defaults(func=cmd_run)

    p = sub.add_parser("prepare", help="darabolás és szójegyzék")
    p.add_argument("epub")
    p.add_argument("--profile", choices=["fiction", "nonfiction"], required=True)
    p.add_argument("--provider", choices=llm.PROVIDERS, help="claude, codex (előfizetés) vagy anthropic, openai, openrouter (API-kulcs)")
    p.add_argument("--model", help="a modell neve (claude: opus, anthropic: claude-opus-5-5; openai és openrouter: kötelező)")
    p.add_argument("--id", help="a munkamappa neve (alapból a fájlnévből)")
    p.add_argument("--no-glossary", action="store_true", help="szójegyzék nélkül")
    p.add_argument("--references", action="store_true",
                   help="az irodalomjegyzéket és a tárgymutatót is lefordítja (alapból angolul maradnak)")
    p.add_argument("--force", action="store_true", help="a meglévő munkamappa törlése")
    p.set_defaults(func=cmd_prepare)

    p = sub.add_parser("glossary", help="a szójegyzék (újra)készítése")
    p.add_argument("id")
    p.set_defaults(func=cmd_glossary)

    p = sub.add_parser("translate", help="fordítás, folytatható")
    p.add_argument("id")
    p.add_argument("--max-chunks", type=int, help="legfeljebb ennyi darab ebben a futásban")
    p.set_defaults(func=cmd_translate)

    p = sub.add_parser("build", help="a magyar EPUB összeállítása")
    p.add_argument("id")
    p.add_argument("--partial", action="store_true", help="a lefordítatlan részek angolul maradnak")
    p.set_defaults(func=cmd_build)

    p = sub.add_parser("audio", help="hangoskönyv a lefordított részből (uv run --group tts)")
    p.add_argument("id")
    p.add_argument("--voice", default="voices/narrator", help="referenciahang mappája (voice.wav + voice.txt)")
    p.add_argument("--skip", nargs="*", default=[], help="ezeket a fájlnév-részleteket nem olvassa fel")
    p.add_argument("--follow", action="store_true", help="a futó fordítással együtt halad, amíg kész a könyv")
    p.set_defaults(func=cmd_audio)

    p = sub.add_parser("status", help="haladás és felhasználás")
    p.add_argument("id")
    p.set_defaults(func=cmd_status)

    args = parser.parse_args()
    args.func(args)
