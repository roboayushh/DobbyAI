"""Opt-in apply of a verified final patch back to the original source (interactive `harness run`)."""
from __future__ import annotations

import io
import os
import shutil
import subprocess
import zipfile
from pathlib import Path

import pytest
from rich.console import Console

from harness.cli_execution import offer_apply_to_original
from tests.support.harness_fixtures import git, make_repo, repo_integrity

RUN_ID = "run_0123456789abcdef"
FILES = {
    "src/__init__.py": "",
    "src/calc.py": "def add(a, b):\n    return a - b\n",
    "tests/test_calc.py": "from src.calc import add\n\n\ndef test_add():\n    assert add(2, 3) == 5\n",
}
FIXED = "def add(a, b):\n    return a + b\n"
BINARY = b"\x89PNG\r\n\x1a\n\x00\x01\x02binary\x00"


def git_bytes(repo: Path, *args: str) -> bytes:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_AUTHOR_NAME": "Fixture",
        "GIT_AUTHOR_EMAIL": "fixture@example.invalid",
        "GIT_COMMITTER_NAME": "Fixture",
        "GIT_COMMITTER_EMAIL": "fixture@example.invalid",
    }
    return subprocess.run(["git", *args], cwd=str(repo), check=True, capture_output=True, env=env).stdout


def make_patch(source: Path, tmp_path: Path) -> Path:
    """The harness's final-patch format (diff-tree --binary --full-index) from a scratch copy."""
    scratch = tmp_path / "scratch"
    shutil.copytree(source, scratch, ignore=shutil.ignore_patterns(".git"))
    git_bytes(scratch, "init", "-q", "-b", "main")
    git_bytes(scratch, "add", "-A")
    git_bytes(scratch, "commit", "-q", "-m", "baseline")
    (scratch / "src" / "calc.py").write_text(FIXED, encoding="utf-8")
    (scratch / "tests" / "test_more.py").write_text("def test_more():\n    assert True\n", encoding="utf-8")
    (scratch / "assets").mkdir()
    (scratch / "assets" / "logo.png").write_bytes(BINARY)
    git_bytes(scratch, "add", "-A")
    git_bytes(scratch, "commit", "-q", "-m", "candidate")
    patch = tmp_path / "artifacts" / "final-patch.diff"
    patch.parent.mkdir()
    patch.write_bytes(git_bytes(scratch, "diff-tree", "-p", "--binary", "--full-index", "--no-color",
                               "--no-renames", "-r", "HEAD~1", "HEAD"))
    return patch


def answer(value):
    def ask(prompt, **kwargs):
        ask.prompts.append((prompt, kwargs))
        return value

    ask.prompts = []
    return ask


def never(*args, **kwargs):
    raise AssertionError("must not ask")


def console() -> Console:
    return Console(file=io.StringIO(), width=1000, color_system=None, force_terminal=False)


def output(out: Console) -> str:
    return out.file.getvalue()


def untouched_state(repo: Path) -> dict:
    state = repo_integrity(repo)
    state.pop("index_sha256")  # `git status` may refresh stat data; staged content is in "status"
    return state


def test_yes_applies_to_working_tree_without_committing(tmp_path):
    original = make_repo(tmp_path, FILES, name="original")
    patch = make_patch(original, tmp_path)
    head, refs = git(original, "rev-parse", "HEAD"), git(original, "for-each-ref")
    ask, out = answer(True), console()

    assert offer_apply_to_original(out, "local_git", str(original), patch, RUN_ID, ask=ask) is True

    prompt, kwargs = ask.prompts[0]
    assert f"Apply these verified changes to the original repository {original}?" in prompt
    assert kwargs["default"] is False
    assert (original / "src" / "calc.py").read_text(encoding="utf-8") == FIXED
    assert (original / "assets" / "logo.png").read_bytes() == BINARY
    assert (original / "tests" / "test_more.py").is_file()
    # Working tree only: no commit, no ref change, nothing staged.
    assert git(original, "rev-parse", "HEAD") == head
    assert git(original, "for-each-ref") == refs
    assert git(original, "diff", "--cached", "--name-only") == ""
    assert git_bytes(original, "status", "--porcelain=v1", "--untracked-files=all").decode().splitlines() == [
        " M src/calc.py", "?? assets/logo.png", "?? tests/test_more.py",
    ]
    text = output(out)
    for path in ("src/calc.py", "tests/test_more.py", "assets/logo.png"):
        assert path in text
    assert f"Receipt: run {RUN_ID}" in text and "3 file(s)" in text
    assert "nothing was committed or pushed" in text


def test_patch_that_does_not_apply_leaves_original_untouched(tmp_path):
    original = make_repo(tmp_path, FILES, name="original")
    patch = make_patch(original, tmp_path)
    (original / "src" / "calc.py").write_text("def add(a, b):\n    return b - a - 0\n", encoding="utf-8")
    git(original, "commit", "-q", "-am", "diverged since the run")
    before = untouched_state(original)
    out = console()

    assert offer_apply_to_original(out, "local_git", str(original), patch, RUN_ID, ask=answer(True)) is False

    assert untouched_state(original) == before
    text = output(out)
    assert "does not apply cleanly" in text and "src/calc.py" in text
    assert "Nothing was changed." in text


def test_public_https_saves_patch_in_cwd(tmp_path, monkeypatch):
    patch = make_patch(make_repo(tmp_path, FILES, name="original"), tmp_path)
    cwd = tmp_path / "cwd"
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    out = console()

    applied = offer_apply_to_original(out, "public_https", "https://github.com/octo/widgets.git", patch, RUN_ID, ask=answer(True))

    assert applied is False  # a remote original is never changed
    saved = cwd / f"widgets-{RUN_ID}.patch"
    assert saved.read_bytes() == patch.read_bytes()
    assert f"git apply --binary {saved}" in output(out)


def test_local_zip_saves_patch_next_to_archive(tmp_path):
    patch = make_patch(make_repo(tmp_path, FILES, name="original"), tmp_path)
    archive = tmp_path / "upload" / "project.zip"
    archive.parent.mkdir()
    with zipfile.ZipFile(archive, "w") as handle:
        handle.writestr("src/calc.py", FILES["src/calc.py"])
    archive_bytes = archive.read_bytes()

    assert offer_apply_to_original(console(), "local_zip", str(archive), patch, RUN_ID, ask=answer(True)) is False

    assert (archive.parent / f"project-{RUN_ID}.patch").read_bytes() == patch.read_bytes()
    assert archive.read_bytes() == archive_bytes


def test_plain_folder_inside_another_repository_is_patched_in_place_only(tmp_path):
    parent = make_repo(tmp_path, {"README.md": "parent\n"}, name="parent")
    folder = parent / "vendored"
    for relative, content in FILES.items():
        (folder / relative).parent.mkdir(parents=True, exist_ok=True)
        (folder / relative).write_text(content, encoding="utf-8")
    patch = make_patch(folder, tmp_path)
    head = git(parent, "rev-parse", "HEAD")

    assert offer_apply_to_original(console(), "local_folder", str(folder), patch, RUN_ID, ask=answer(True)) is True

    assert (folder / "src" / "calc.py").read_text(encoding="utf-8") == FIXED
    assert (folder / "assets" / "logo.png").read_bytes() == BINARY
    assert (parent / "README.md").read_text(encoding="utf-8") == "parent\n"
    assert not (parent / "src").exists() and not (parent / "assets").exists()
    assert git(parent, "rev-parse", "HEAD") == head
    assert git(parent, "diff", "--cached", "--name-only") == ""


def _eof(prompt, **kwargs):
    raise EOFError


@pytest.mark.parametrize("ask", [answer(False), _eof], ids=["declined", "no-input"])
@pytest.mark.parametrize("kind", ["local_git", "public_https"])
def test_declining_changes_nothing(tmp_path, monkeypatch, ask, kind):
    original = make_repo(tmp_path, FILES, name="original")
    patch = make_patch(original, tmp_path)
    monkeypatch.chdir(tmp_path)
    before, listing = untouched_state(original), sorted(os.listdir(tmp_path))
    locator = str(original) if kind == "local_git" else "https://github.com/octo/widgets"

    assert offer_apply_to_original(console(), kind, locator, patch, RUN_ID, ask=ask) is False

    assert untouched_state(original) == before
    assert sorted(os.listdir(tmp_path)) == listing


def test_missing_original_or_empty_patch_is_refused_without_asking(tmp_path):
    original = make_repo(tmp_path, FILES, name="original")
    patch = make_patch(original, tmp_path)
    out = console()

    assert offer_apply_to_original(out, "local_git", str(tmp_path / "gone"), patch, RUN_ID, ask=never) is False
    assert "no longer exists. Nothing was changed." in output(out)
    empty = tmp_path / "empty.diff"
    empty.write_bytes(b"")
    assert offer_apply_to_original(console(), "local_git", str(original), empty, RUN_ID, ask=never) is False
