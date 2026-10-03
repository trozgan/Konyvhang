"""The command line, with the model, the translation and the audio replaced by fakes."""

import argparse
import io
import runpy
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, ClassVar

import pytest

from konyvhang import build, cli, glossary, llm, translate
from konyvhang.workdir import WorkDir

Calls = dict[str, Any]  # what the `calls` fixture recorded


@pytest.fixture(autouse=True)
def offline(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Work folders under tmp_path; every provider counts as installed and logged in."""
    monkeypatch.setenv("KONYVHANG_WORK", str(tmp_path / "work"))
    monkeypatch.setattr(llm, "check_ready", lambda provider: None)


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> Calls:
    """Record glossary, translation and build calls instead of running them."""
    seen: Calls = {"glossary": 0, "translate": [], "build": []}

    def fake_glossary(wd: WorkDir) -> None:
        seen["glossary"] += 1
        wd.save_glossary({"terms": []})

    def fake_translate(
        wd: WorkDir, log: Callable[[str], None] = print, max_chunks: int | None = None, done: bool = True
    ) -> bool:
        log("translating")
        seen["translate"].append(max_chunks)
        result: bool = seen.get("translate_result", True)
        return result

    def fake_build(wd: WorkDir, partial: bool = False, log: Callable[[str], None] = print) -> None:
        log("building")
        seen["build"].append(partial)

    monkeypatch.setattr(glossary, "build", fake_glossary)
    monkeypatch.setattr(translate, "run", fake_translate)
    monkeypatch.setattr(build, "build", fake_build)
    return seen


def run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["konyvhang", *argv])
    cli.main()


def book(tmp_path: Path) -> WorkDir:
    return WorkDir(tmp_path / "work" / "book")


def run_args(epub: Path, **overrides: object) -> argparse.Namespace:
    args: dict[str, object] = dict(
        epub=str(epub), profile="fiction", provider=None, model=None, id=None, voice="voices/narrator",
        skip=[], no_audio=True, references=False, yes=True,
    )  # fmt: skip
    return argparse.Namespace(**{**args, **overrides})


# helpers ------------------------------------------------------------------------
def test_slug_and_work_root(monkeypatch: pytest.MonkeyPatch) -> None:
    assert cli.slug("Difficult Conversations - Stone!") == "difficult-conversations-stone"
    assert cli.slug("!!!") == "konyv"
    monkeypatch.delenv("KONYVHANG_WORK")
    assert str(cli.work_root()) == "work"


def test_open_book_needs_a_prepared_book() -> None:
    with pytest.raises(SystemExit, match="Nincs ilyen könyv"):
        cli.open_book("missing")


def test_provider_problems_stop_with_their_message(monkeypatch: pytest.MonkeyPatch) -> None:
    def not_ready(provider: str) -> None:
        raise llm.LLMError("Hiányzik a(z) OPENAI_API_KEY környezeti változó.")

    monkeypatch.setattr(llm, "check_ready", not_ready)
    with pytest.raises(SystemExit, match="OPENAI_API_KEY"):
        cli.check_provider("openai")


def test_choose_model_needs_a_model_for_openai() -> None:
    assert cli.choose_model("claude", None) == "opus"
    with pytest.raises(SystemExit, match="--model"):
        cli.choose_model("openai", None)


# prepare ------------------------------------------------------------------------
def test_prepare_creates_the_book_and_refuses_to_overwrite(
    epub_file: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    kwargs: dict[str, Any] = dict(profile="fiction", provider="claude", model=None, book_id=None, force=False)
    wd = cli.prepare(epub_file, **kwargs)
    assert wd.book_id == "book"
    assert wd.load_state()["model"] == "opus"
    assert "angolul marad" in capsys.readouterr().out

    with pytest.raises(SystemExit, match="Már létezik"):
        cli.prepare(epub_file, **kwargs)

    (wd.root / "leftover.txt").write_text("x", encoding="utf-8")
    cli.prepare(epub_file, **{**kwargs, "force": True, "references": True})
    assert not (wd.root / "leftover.txt").exists()
    assert "angolul marad" not in capsys.readouterr().out


def test_prepare_command_with_and_without_glossary(
    epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    run_cli(monkeypatch, "prepare", str(epub_file), "--profile", "fiction", "--provider", "claude", "--no-glossary")
    assert calls["glossary"] == 0
    run_cli(monkeypatch, "prepare", str(epub_file), "--profile", "nonfiction", "--provider", "codex", "--force")
    assert calls["glossary"] == 1
    assert book(tmp_path).load_state()["provider"] == "codex"
    assert "Nézd át" in capsys.readouterr().out


def test_glossary_command_stops_cleanly_when_the_quota_runs_out(
    epub_file: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    run_cli(monkeypatch, "prepare", str(epub_file), "--profile", "fiction", "--provider", "claude", "--no-glossary")

    def limit(wd: WorkDir) -> None:
        raise llm.UsageLimitError("resets 5pm")

    monkeypatch.setattr(glossary, "build", limit)
    with pytest.raises(SystemExit, match="Elfogyott a keret"):
        run_cli(monkeypatch, "glossary", "book")


@pytest.mark.parametrize("error", [llm.LLMError("no key"), ValueError("bad JSON twice")])
def test_glossary_command_stops_cleanly_on_other_model_errors(
    epub_file: Path, monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    run_cli(monkeypatch, "prepare", str(epub_file), "--profile", "fiction", "--provider", "claude", "--no-glossary")

    def fail(wd: WorkDir) -> None:
        raise error

    monkeypatch.setattr(glossary, "build", fail)
    with pytest.raises(SystemExit, match="nem készült el"):
        run_cli(monkeypatch, "glossary", "book")


def test_prepare_of_a_broken_epub_leaves_no_prepared_book(tmp_path: Path) -> None:
    broken = tmp_path / "broken.epub"
    broken.write_bytes(b"not a zip")
    with pytest.raises(zipfile.BadZipFile):
        cli.prepare(broken, profile="fiction", provider="claude", model=None, book_id=None, force=False)
    assert not WorkDir(tmp_path / "work" / "broken").exists()


# run ----------------------------------------------------------------------------
def test_run_needs_a_profile_for_a_new_book(epub_file: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(SystemExit, match="--profile"):
        cli.cmd_run(run_args(epub_file, profile=None))


def test_run_new_book_without_audio(
    epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    run_cli(monkeypatch, "run", str(epub_file), "--profile", "fiction", "--yes", "--no-audio")
    out = capsys.readouterr().out
    assert calls["glossary"] == 1 and calls["translate"] == [None] and calls["build"] == [False]
    assert "[fordítás] translating" in out and "[fordítás] building" in out
    assert "Kész." in out


def test_run_waits_for_glossary_review(epub_file: Path, monkeypatch: pytest.MonkeyPatch, calls: Calls) -> None:
    prompts: list[str] = []
    monkeypatch.setattr("builtins.input", prompts.append)
    cli.cmd_run(run_args(epub_file, yes=False))
    assert prompts and "Enter" in prompts[0]


def test_run_existing_book_switches_or_keeps_provider(
    epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    cli.cmd_run(run_args(epub_file))
    wd = book(tmp_path)

    cli.cmd_run(run_args(epub_file, provider="codex"))
    assert wd.load_state()["provider"] == "codex" and wd.load_state()["model"] is None
    assert "Szolgáltató: codex, modell: alapértelmezett" in capsys.readouterr().out

    cli.cmd_run(run_args(epub_file, model="gpt-6-sol"))  # only a new model: the provider stays
    assert wd.load_state()["provider"] == "codex" and wd.load_state()["model"] == "gpt-6-sol"

    checked: list[str] = []
    monkeypatch.setattr(llm, "check_ready", checked.append)
    cli.cmd_run(run_args(epub_file))  # nothing to switch: only checks the saved provider
    assert checked == ["codex"]


def test_run_stopped_translation_does_not_build(
    epub_file: Path, monkeypatch: pytest.MonkeyPatch, calls: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    calls["translate_result"] = False
    cli.cmd_run(run_args(epub_file))
    assert calls["build"] == []
    assert "megállt" in capsys.readouterr().out


def test_run_skips_audio_without_the_tts_group(
    epub_file: Path, monkeypatch: pytest.MonkeyPatch, calls: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr("konyvhang.cli.importlib.util.find_spec", lambda name: None)
    cli.cmd_run(run_args(epub_file, no_audio=False))
    assert "tts csoport" in capsys.readouterr().out


class FakePopen:
    """Stands in for the audio child process."""

    instances: ClassVar[list["FakePopen"]] = []
    code = 0

    def __init__(self, cmd: list[str], **kwargs: Any) -> None:  # noqa: ANN401 - subprocess.Popen keyword arguments
        self.cmd, self.kwargs = cmd, kwargs
        self.returncode = FakePopen.code
        self.stdout = io.StringIO("Fetching 3 files\n[1/2] kész\n\n")
        FakePopen.instances.append(self)

    def wait(self) -> int:
        return self.returncode


@pytest.mark.parametrize("case", [(0, True), (0, False), (3, True)])
def test_run_with_audio_follows_the_translation(
    epub_file: Path,
    monkeypatch: pytest.MonkeyPatch,
    calls: Calls,
    capsys: pytest.CaptureFixture[str],
    case: tuple[int, bool],
) -> None:
    code, done = case  # audio exit code, translation finished
    FakePopen.code, FakePopen.instances = code, []
    calls["translate_result"] = done
    monkeypatch.setattr("konyvhang.cli.importlib.util.find_spec", lambda name: NS())
    monkeypatch.setattr("konyvhang.cli.subprocess.Popen", FakePopen)
    args = run_args(epub_file, no_audio=False, skip=["02_Praise"])
    if code:
        with pytest.raises(SystemExit, match="kilépési kód 3"):
            cli.cmd_run(args)
    else:
        cli.cmd_run(args)
    proc = FakePopen.instances[0]
    assert proc.cmd[-2:] == ["--skip", "02_Praise"] and "--follow" in proc.cmd
    assert proc.kwargs["env"]["PYTHONIOENCODING"] == "utf-8"
    out = capsys.readouterr().out
    assert ("még befejezi" in out) == (not done)
    assert "[hang] [1/2] kész" in out  # the child's output is printed in full before the exit


def test_run_with_audio_and_no_skips(epub_file: Path, monkeypatch: pytest.MonkeyPatch, calls: Calls) -> None:
    FakePopen.code, FakePopen.instances = 0, []
    monkeypatch.setattr("konyvhang.cli.importlib.util.find_spec", lambda name: NS())
    monkeypatch.setattr("konyvhang.cli.subprocess.Popen", FakePopen)
    cli.cmd_run(run_args(epub_file, no_audio=False))
    assert "--skip" not in FakePopen.instances[0].cmd


def test_prefix_lines_drops_library_noise(capsys: pytest.CaptureFixture[str]) -> None:
    cli.prefix_lines(
        ["UserWarning: x\n", "Fetching 6 files: 100%\n", "kernel = np.where(\n", "\n", "kész\n"], "[hang] "
    )
    assert capsys.readouterr().out == "[hang] kész\n"


# other commands ------------------------------------------------------------------
@pytest.fixture
def prepared(epub_file: Path, monkeypatch: pytest.MonkeyPatch, calls: Calls) -> Calls:
    run_cli(monkeypatch, "prepare", str(epub_file), "--profile", "fiction", "--provider", "claude", "--no-glossary")
    return calls


def test_translate_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepared: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    run_cli(monkeypatch, "translate", "book", "--max-chunks", "2")
    out = capsys.readouterr().out
    assert prepared["translate"] == [2]
    assert "nincs szójegyzék" in out and "Minden darab kész" in out

    book(tmp_path).save_glossary({"terms": []})
    prepared["translate_result"] = False
    run_cli(monkeypatch, "translate", "book")
    out = capsys.readouterr().out
    assert "nincs szójegyzék" not in out and "Minden darab kész" not in out


def test_build_command(monkeypatch: pytest.MonkeyPatch, prepared: Calls) -> None:
    run_cli(monkeypatch, "build", "book", "--partial")
    assert prepared["build"] == [True]


def test_audio_command_runs_or_follows(monkeypatch: pytest.MonkeyPatch, prepared: Calls) -> None:
    from konyvhang import audio

    seen: list[tuple[str, str, list[str]]] = []
    monkeypatch.setattr(audio, "run", lambda wd, voice, skip: seen.append(("run", voice.name, skip)))
    monkeypatch.setattr(audio, "follow", lambda wd, voice, skip: seen.append(("follow", voice.name, skip)))
    run_cli(monkeypatch, "audio", "book", "--voice", "voices/anna", "--skip", "x")
    run_cli(monkeypatch, "audio", "book", "--follow")
    assert seen == [("run", "anna", ["x"]), ("follow", "narrator", [])]


def test_status_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, prepared: Calls, capsys: pytest.CaptureFixture[str]
) -> None:
    run_cli(monkeypatch, "status", "book")
    out = capsys.readouterr().out
    assert "szolgáltató: claude, modell: opus" in out
    assert "Szójegyzék: nincs" in out and "Lefordítva: 0/1 darab" in out and "$" not in out

    wd = book(tmp_path)
    state = wd.load_state()
    state.update(model=None, usage={"calls": 3, "input_tokens": 1200, "output_tokens": 800, "cost_usd": 0.0123})
    wd.save_state(state)
    wd.save_glossary({"terms": []})
    run_cli(monkeypatch, "status", "book")
    out = capsys.readouterr().out
    assert "modell: alapértelmezett" in out and "Szójegyzék: van" in out
    assert "3 hívás, 1,200 bemeneti és 800 kimeneti token, $0.0123" in out


def test_main_works_with_streams_that_cannot_be_reconfigured(
    epub_file: Path, monkeypatch: pytest.MonkeyPatch, prepared: Calls
) -> None:
    stdout = io.StringIO()
    monkeypatch.setattr(sys, "stdout", stdout)
    monkeypatch.setattr(sys, "stderr", io.StringIO())
    run_cli(monkeypatch, "status", "book")
    assert "Lefordítva" in stdout.getvalue()


def test_module_entry_point(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["konyvhang", "--help"])
    with pytest.raises(SystemExit) as exit_info:
        runpy.run_module("konyvhang", run_name="__main__")
    assert exit_info.value.code == 0
    assert "konyvhang" in capsys.readouterr().out


def test_rerunning_an_existing_book_from_the_command_line_keeps_its_provider(
    epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: Calls
) -> None:
    run_cli(monkeypatch, "prepare", str(epub_file), "--profile", "fiction", "--provider", "codex", "--no-glossary")
    run_cli(monkeypatch, "run", str(epub_file), "--no-audio", "--yes")
    state = book(tmp_path).load_state()
    assert state["provider"] == "codex"
    assert state["model"] is None


def test_prepare_without_provider_uses_claude(
    epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, calls: Calls
) -> None:
    run_cli(monkeypatch, "prepare", str(epub_file), "--profile", "fiction", "--no-glossary")
    assert book(tmp_path).load_state()["provider"] == "claude"
