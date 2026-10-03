"""What the command line passes down: flags into state.json and chunking, work folders into each step."""

import io
import subprocess
import sys
import zipfile
from collections.abc import Callable
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any

import pytest
from conftest import CH1, make_epub

from konyvhang import build, cli, glossary, llm, translate
from konyvhang.workdir import WorkDir

BIBLIOGRAPHY = (
    '<section epub:type="bibliography" xmlns:epub="http://www.idpf.org/2007/ops"><h2>References</h2>'
    "<p>Smith, J. (2020). A Book. Publisher.</p></section></body>"
)


@pytest.fixture
def book_with_references(tmp_path: Path) -> Path:
    path = make_epub(tmp_path / "Some Book.epub")
    with zipfile.ZipFile(path) as zf:
        files = {name: zf.read(name) for name in zf.namelist()}
    files["OEBPS/text/ch1.xhtml"] = CH1.replace("</body>", BIBLIOGRAPHY).encode()
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in files.items():
            zf.writestr(name, data)
    return path


class Steps:
    """Records which work folder each pipeline step and provider check received."""

    def __init__(self) -> None:
        self.checked: list[str] = []
        self.glossary: list[Path] = []
        self.translate: list[tuple[Path, int | None]] = []
        self.build: list[tuple[Path, bool]] = []


@pytest.fixture
def steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Steps:
    seen = Steps()
    monkeypatch.setenv("KONYVHANG_WORK", str(tmp_path / "work"))
    monkeypatch.setattr(llm, "check_ready", seen.checked.append)

    def fake_glossary(wd: WorkDir) -> None:
        seen.glossary.append(wd.root)
        wd.save_glossary({"terms": []})

    def fake_translate(wd: WorkDir, log: Callable[[str], None] = print, max_chunks: int | None = None) -> bool:
        seen.translate.append((wd.root, max_chunks))
        return True

    def fake_build(wd: WorkDir, partial: bool = False, log: Callable[[str], None] = print) -> Path:
        seen.build.append((wd.root, partial))
        return wd.out_dir

    monkeypatch.setattr(glossary, "build", fake_glossary)
    monkeypatch.setattr(translate, "run", fake_translate)
    monkeypatch.setattr(build, "build", fake_build)
    return seen


def run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> None:
    monkeypatch.setattr(sys, "argv", ["konyvhang", *argv])
    cli.main()


def chunk_texts(wd: WorkDir) -> str:
    return " ".join(s["src"] for c in wd.load_chunks() for s in c["segments"])


# prepare and run: flags end up in state.json and the chunks ---------------------------
@pytest.mark.parametrize("command", ["prepare", "run"])
def test_every_flag_reaches_the_prepared_book(
    command: str, book_with_references: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: Steps
) -> None:
    extra = ["--no-glossary"] if command == "prepare" else ["--no-audio", "--yes"]
    run_cli(
        monkeypatch, command, str(book_with_references), "--profile", "nonfiction", "--provider", "anthropic",
        "--model", "claude-test-1", "--id", "my-id", *extra, "--references",
    )  # fmt: skip
    wd = WorkDir(tmp_path / "work" / "my-id")
    assert wd.load_state() == {
        "profile": "nonfiction",
        "provider": "anthropic",
        "model": "claude-test-1",
        "source_name": "Some Book.epub",
    }
    assert "Smith" in chunk_texts(wd)
    assert steps.checked[0] == "anthropic"


@pytest.mark.parametrize("command", ["prepare", "run"])
def test_defaults_leave_references_out_and_name_the_folder_after_the_file(
    command: str, book_with_references: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: Steps
) -> None:
    extra = ["--no-glossary"] if command == "prepare" else ["--no-audio", "--yes"]
    run_cli(monkeypatch, command, str(book_with_references), "--profile", "fiction", *extra)
    wd = WorkDir(tmp_path / "work" / "some-book")
    assert wd.load_state()["provider"] == "claude" and wd.load_state()["model"] == "opus"
    assert "Smith" not in chunk_texts(wd) and "Tom" in chunk_texts(wd)


def test_run_passes_the_prepared_book_to_every_step(
    epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: Steps
) -> None:
    run_cli(monkeypatch, "run", str(epub_file), "--profile", "fiction", "--no-audio", "--yes", "--id", "x")
    root = tmp_path / "work" / "x"
    assert steps.glossary == [root]
    assert steps.translate == [(root, None)]
    assert steps.build == [(root, False)]


# old state.json files without a provider ----------------------------------------------
def prepared_without_provider(epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> WorkDir:
    run_cli(monkeypatch, "prepare", str(epub_file), "--profile", "fiction", "--provider", "codex", "--no-glossary")
    wd = WorkDir(tmp_path / "work" / "book")
    state = wd.load_state()
    del state["provider"]
    wd.save_state(state)
    return wd


def test_old_book_without_provider_is_checked_as_claude(
    epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: Steps
) -> None:
    prepared_without_provider(epub_file, tmp_path, monkeypatch)
    steps.checked.clear()
    run_cli(monkeypatch, "run", str(epub_file), "--no-audio", "--yes")
    assert steps.checked == ["claude"]


def test_old_book_switching_only_the_model_stays_on_claude(
    epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: Steps
) -> None:
    wd = prepared_without_provider(epub_file, tmp_path, monkeypatch)
    run_cli(monkeypatch, "run", str(epub_file), "--no-audio", "--yes", "--model", "sonnet")
    assert wd.load_state()["provider"] == "claude" and wd.load_state()["model"] == "sonnet"


def test_status_of_an_old_book_shows_claude_and_zero_usage(
    epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: Steps, capsys: pytest.CaptureFixture[str]
) -> None:
    prepared_without_provider(epub_file, tmp_path, monkeypatch)
    capsys.readouterr()
    run_cli(monkeypatch, "status", "book")
    out = capsys.readouterr().out
    assert "szolgáltató: claude," in out
    assert "Felhasználás: 0 hívás, 0 bemeneti és 0 kimeneti token\n" in out


def test_status_shows_the_saved_provider(
    epub_file: Path, monkeypatch: pytest.MonkeyPatch, steps: Steps, capsys: pytest.CaptureFixture[str]
) -> None:
    run_cli(monkeypatch, "prepare", str(epub_file), "--profile", "fiction", "--provider", "codex", "--no-glossary")
    run_cli(monkeypatch, "status", "book")
    assert "szolgáltató: codex, modell: alapértelmezett" in capsys.readouterr().out


# single-step commands get the right work folder ------------------------------------
@pytest.fixture
def prepared(epub_file: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: Steps) -> Path:
    run_cli(monkeypatch, "prepare", str(epub_file), "--profile", "fiction", "--no-glossary", "--id", "b")
    return tmp_path / "work" / "b"


def test_step_commands_pass_the_named_book(monkeypatch: pytest.MonkeyPatch, steps: Steps, prepared: Path) -> None:
    run_cli(monkeypatch, "glossary", "b")
    run_cli(monkeypatch, "translate", "b", "--max-chunks", "3")
    run_cli(monkeypatch, "build", "b")
    assert steps.glossary == [prepared]
    assert steps.translate == [(prepared, 3)]
    assert steps.build == [(prepared, False)]


@pytest.mark.parametrize("follow", [False, True])
def test_audio_command_passes_the_named_book(
    monkeypatch: pytest.MonkeyPatch, steps: Steps, prepared: Path, follow: bool
) -> None:
    from konyvhang import audio

    seen: list[tuple[str, Path, Path, list[str]]] = []
    for name in ("run", "follow"):
        monkeypatch.setattr(audio, name, lambda wd, voice, skip, name=name: seen.append((name, wd.root, voice, skip)))
    run_cli(monkeypatch, "audio", "b", *(["--follow"] if follow else []))
    assert seen == [("follow" if follow else "run", prepared, Path("voices/narrator"), [])]


# the audio follower child process ----------------------------------------------------
class RecordingPopen:
    def __init__(self, cmd: list[str], **kwargs: Any) -> None:  # noqa: ANN401 - subprocess.Popen keyword arguments
        self.cmd, self.kwargs = cmd, kwargs
        self.stdout = io.StringIO("")
        children.append(self)

    def wait(self) -> int:
        return 0


children: list[RecordingPopen] = []


def test_audio_follower_command_line_and_pipes(epub_file: Path, monkeypatch: pytest.MonkeyPatch, steps: Steps) -> None:
    children.clear()
    looked_up: list[str] = []

    def find_spec(name: str) -> NS:
        looked_up.append(name)
        return NS()

    monkeypatch.setattr("konyvhang.cli.importlib.util.find_spec", find_spec)
    monkeypatch.setattr("konyvhang.cli.subprocess.Popen", RecordingPopen)
    run_cli(monkeypatch, "run", str(epub_file), "--profile", "fiction", "--yes", "--voice", "voices/anna")

    assert looked_up == ["mlx_audio"]
    (child,) = children
    assert child.cmd == [sys.executable, "-u", "-m", "konyvhang", "audio", "book", "--follow", "--voice", "voices/anna"]
    env = child.kwargs.pop("env")
    assert env["PYTHONIOENCODING"] == "utf-8"
    assert child.kwargs == {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True, "encoding": "utf-8"}


# argument parsing ----------------------------------------------------------------------
class Console(io.StringIO):
    """A text stream that records how main() reconfigured it."""

    def __init__(self) -> None:
        super().__init__()
        self.options: dict[str, str] = {}

    def reconfigure(self, **options: str) -> None:
        self.options = options


def test_main_switches_both_console_streams_to_utf8(
    monkeypatch: pytest.MonkeyPatch, steps: Steps, prepared: Path
) -> None:
    out, err = Console(), Console()
    monkeypatch.setattr(sys, "stdout", out)
    monkeypatch.setattr(sys, "stderr", err)
    run_cli(monkeypatch, "status", "b")
    assert out.options == err.options == {"encoding": "utf-8", "errors": "replace"}


def test_help_names_the_program(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(sys, "argv", ["/somewhere/__main__.py", "--help"])
    with pytest.raises(SystemExit):
        cli.main()
    out = capsys.readouterr().out
    assert out.startswith("usage: konyvhang ")
    assert "EPUB-könyvek fordítása magyarra" in out


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["prepare", "b.epub"],
        ["prepare", "b.epub", "--profile", "poetry"],
        ["prepare", "b.epub", "--profile", "fiction", "--provider", "gemini"],
        ["run", "b.epub", "--profile", "poetry"],
        ["run", "b.epub", "--provider", "gemini"],
    ],
)
def test_invalid_command_lines_are_usage_errors(monkeypatch: pytest.MonkeyPatch, steps: Steps, argv: list[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        run_cli(monkeypatch, *argv)
    assert exit_info.value.code == 2


def test_slug_keeps_letters_that_strip_could_eat() -> None:
    assert cli.slug("Xenia X") == "xenia-x"
