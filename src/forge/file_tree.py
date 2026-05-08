"""File tree generation for Planner inputs.

The Planner needs a snapshot of the repo's file layout so its task IDs can
reference real paths. We feed it a newline-separated list of repo-relative
paths, simplest possible format the model can parse.

Two paths:

1. **Git mode** (preferred): `git ls-files` — respects `.gitignore`, doesn't
   walk into `.git/`, `node_modules/`, `__pycache__/`, etc. Free filtering.

2. **Filesystem fallback**: when `repo_root` is not a git repo. Walks
   `pathlib.rglob("*")` with a hardcoded ignore list. Less precise, but
   useful for greenfield projects (`forge plan` on day-one before
   `git init`) and for testing.

We do not normalize order beyond what `git ls-files` produces (already
sorted by git) and what `pathlib.rglob` produces (we sort it ourselves
for determinism — the Planner's output should not depend on filesystem
iteration order).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

# Filesystem-fallback ignore list. Must mirror what `git ls-files` would
# already exclude via standard `.gitignore` conventions, so the two modes
# produce comparable trees. Add to this list when you find that "comparable"
# is being violated by some new generated artifact.
_FALLBACK_IGNORE_DIRS: frozenset[str] = frozenset(
    {
        ".git",
        ".venv",
        "venv",
        "__pycache__",
        "node_modules",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "dist",
        "build",
        ".tox",
        ".idea",
        ".vscode",
        ".forge",  # don't leak run state into Planner inputs
    }
)


def build_file_tree(repo_root: Path) -> str:
    """Return a newline-separated list of repo-relative file paths.

    Args:
        repo_root: Directory the Planner should consider as the repo root.
            Must exist; we don't try to be helpful about missing dirs because
            the Planner's user_story is meaningless without a real repo to
            plan against.

    Returns:
        Newline-separated string of repo-relative paths, one per line.
        Empty string if the repo has no files (greenfield, pre-`git init`).

    Raises:
        FileNotFoundError: If `repo_root` doesn't exist or isn't a directory.
    """
    if not repo_root.exists():
        raise FileNotFoundError(f"repo_root does not exist: {repo_root}")
    if not repo_root.is_dir():
        raise FileNotFoundError(f"repo_root is not a directory: {repo_root}")

    paths = (
        _git_ls_files(repo_root)
        if (repo_root / ".git").exists()
        else _fallback_walk(repo_root)
    )

    return "\n".join(paths)


def _git_ls_files(repo_root: Path) -> list[str]:
    """Run `git ls-files` and return its output as a sorted list of paths.

    `git ls-files` already returns paths repo-relative and respects
    `.gitignore`. We don't pass any flags — the default lists tracked
    files only, which matches what the Planner should see (untracked
    junk in the working tree shouldn't shape task IDs).
    """
    result = subprocess.run(
        ["git", "ls-files"],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        # If `.git/` exists but git refuses to list (corrupt index, no
        # commits yet, missing git binary, etc.), fall back to the walker
        # rather than dying. The Planner can still work with a slightly
        # noisier tree.
        return _fallback_walk(repo_root)

    # `git ls-files` is already sorted, but we sort defensively in case
    # of git config quirks (e.g. core.precomposeunicode on macOS).
    return sorted(p for p in result.stdout.splitlines() if p)


def _fallback_walk(repo_root: Path) -> list[str]:
    """Walk repo_root with pathlib, skipping known noise dirs.

    Slower than `git ls-files` and less precise (no `.gitignore`), but
    works on greenfield projects.
    """
    found: list[str] = []
    for path in repo_root.rglob("*"):
        if not path.is_file():
            continue
        # Skip if any ancestor is in the ignore list. We check by name
        # rather than by path-prefix to catch nested cases like
        # `src/.venv/lib/...` without enumerating every prefix.
        rel = path.relative_to(repo_root)
        if any(part in _FALLBACK_IGNORE_DIRS for part in rel.parts):
            continue
        # Use forward slashes regardless of platform — the Planner prompt
        # is platform-agnostic and `git ls-files` always uses forward
        # slashes, so the two modes produce visually identical output.
        found.append(rel.as_posix())
    return sorted(found)
