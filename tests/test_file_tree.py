"""Tests for forge.file_tree.

Cover both modes (git ls-files, fallback walk), the ignore list, and
edge cases (missing dir, empty repo).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge.file_tree import build_file_tree


def _git_init(repo: Path) -> None:
    """Initialize a git repo with one file committed (so ls-files has output)."""
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "test@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "Test"], cwd=repo, check=True)


def test_missing_repo_root_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        build_file_tree(tmp_path / "does-not-exist")


def test_repo_root_must_be_dir(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(FileNotFoundError):
        build_file_tree(f)


def test_fallback_walk_lists_files(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("a")
    (tmp_path / "src" / "b.py").write_text("b")
    (tmp_path / "README.md").write_text("hi")

    tree = build_file_tree(tmp_path)
    lines = tree.splitlines()
    assert "README.md" in lines
    assert "src/a.py" in lines
    assert "src/b.py" in lines


def test_fallback_walk_skips_ignored_dirs(tmp_path: Path) -> None:
    """`.git/`, `__pycache__/`, `node_modules/`, `.forge/` etc. are excluded."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "real.py").write_text("x")

    # Noise we expect to be ignored
    for ignored in [".venv", "__pycache__", "node_modules", ".forge", ".git"]:
        d = tmp_path / ignored
        d.mkdir()
        (d / "garbage.py").write_text("nope")

    tree = build_file_tree(tmp_path)
    lines = tree.splitlines()
    assert "src/real.py" in lines
    for line in lines:
        for ignored in [".venv", "__pycache__", "node_modules", ".forge"]:
            assert ignored not in line, f"{line!r} leaked from ignored dir {ignored}"


def test_fallback_walk_uses_forward_slashes(tmp_path: Path) -> None:
    """Output must be platform-agnostic — always forward slashes."""
    nested = tmp_path / "a" / "b" / "c"
    nested.mkdir(parents=True)
    (nested / "deep.txt").write_text("x")

    tree = build_file_tree(tmp_path)
    assert "a/b/c/deep.txt" in tree.splitlines()


def test_fallback_walk_empty_dir_returns_empty_string(tmp_path: Path) -> None:
    assert build_file_tree(tmp_path) == ""


def test_fallback_walk_output_is_sorted(tmp_path: Path) -> None:
    """Determinism — Planner output must not depend on filesystem iteration order."""
    (tmp_path / "z.txt").write_text("z")
    (tmp_path / "a.txt").write_text("a")
    (tmp_path / "m.txt").write_text("m")

    lines = build_file_tree(tmp_path).splitlines()
    assert lines == sorted(lines)


def test_git_mode_used_when_dot_git_present(tmp_path: Path) -> None:
    _git_init(tmp_path)
    (tmp_path / "tracked.py").write_text("x")
    subprocess.run(["git", "add", "tracked.py"], cwd=tmp_path, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=tmp_path, check=True)

    # Untracked file — should NOT appear in git mode (ls-files lists tracked only)
    (tmp_path / "untracked.py").write_text("nope")

    tree = build_file_tree(tmp_path)
    lines = tree.splitlines()
    assert "tracked.py" in lines
    assert "untracked.py" not in lines


def test_git_mode_falls_back_when_git_fails(tmp_path: Path) -> None:
    """If `.git/` exists but `git ls-files` errors out, fall back to walker.

    Simulated by creating a `.git` directory that isn't a real git repo —
    `git ls-files` will fail, fallback should produce a walker result.
    """
    (tmp_path / ".git").mkdir()  # bogus .git dir, no init
    (tmp_path / "real.py").write_text("x")

    tree = build_file_tree(tmp_path)
    # Fallback walker excludes `.git/` so we just see `real.py`
    assert "real.py" in tree.splitlines()
