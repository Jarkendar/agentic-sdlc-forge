"""Tests for forge.init_scaffold.

These exercise the filesystem-only side of `forge init` — no LLM,
no interview. Covers:

- happy path: scaffold creates the full tree in an empty dir
- --no-interview: architecture.md is the template, NOT absent
- interview path: architecture.md is absent (caller writes it later),
  but architecture.md.template is in place
- .gitignore: created if missing; appended if present without our marker
- idempotency: second init refuses
- target dir must exist and be a directory
"""

from __future__ import annotations

from pathlib import Path

import pytest

from forge.init_scaffold import (
    ScaffoldError,
    scaffold,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def target(tmp_path: Path) -> Path:
    """A fresh empty target dir for each test."""
    return tmp_path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_scaffold_creates_full_tree_no_interview(target: Path) -> None:
    """--no-interview path lays down everything including architecture.md."""
    result = scaffold(target, no_interview=True)

    # Top-level .forge/ exists with the expected files
    assert (target / ".forge" / "config.toml").is_file()
    assert (target / ".forge" / "config.example.toml").is_file()
    assert (target / ".forge" / "git_flow.md").is_file()
    assert (target / ".forge" / "architecture_map.md").is_file()

    # personas/ with all six personas (5 runtime + 1 architect)
    personas = target / ".forge" / "personas"
    for name in ("orchestrator", "planner", "executor", "verifier", "reporter", "architect"):
        assert (personas / f"{name}.md").is_file(), f"missing persona: {name}"

    # presets/
    presets = target / ".forge" / "presets"
    for name in ("python", "android", "kmp", "kotlin-gradle", "node-pnpm"):
        assert (presets / f"{name}.toml").is_file(), f"missing preset: {name}"

    # knowledge/ has BOTH template and architecture.md (latter because no-interview)
    knowledge = target / ".forge" / "knowledge"
    assert (knowledge / "architecture.md.template").is_file()
    assert (knowledge / "architecture.md").is_file()
    assert result.architecture_is_template is True

    # .env.example at project root
    assert (target / ".env.example").is_file()


def test_scaffold_interview_path_omits_architecture_md(target: Path) -> None:
    """Interview path leaves architecture.md ABSENT so caller can fill it.

    The template still gets dropped under knowledge/ as a reference, but
    the real architecture.md does NOT exist after scaffold() returns.
    The caller (cmd_init) is responsible for writing it.
    """
    result = scaffold(target, no_interview=False)

    assert (target / ".forge" / "knowledge" / "architecture.md.template").is_file()
    assert not (target / ".forge" / "knowledge" / "architecture.md").exists()
    assert result.architecture_is_template is False


def test_config_toml_is_copy_of_example(target: Path) -> None:
    """config.toml must equal config.example.toml byte-for-byte."""
    scaffold(target, no_interview=True)
    example = (target / ".forge" / "config.example.toml").read_bytes()
    real = (target / ".forge" / "config.toml").read_bytes()
    assert example == real


# ---------------------------------------------------------------------------
# .gitignore
# ---------------------------------------------------------------------------


def test_gitignore_created_when_absent(target: Path) -> None:
    """No .gitignore present → we create one with our block."""
    result = scaffold(target, no_interview=True)
    assert result.gitignore_created is True
    assert result.gitignore_changed is True
    content = (target / ".gitignore").read_text(encoding="utf-8")
    assert "# >>> forge init <<<" in content
    assert ".forge/runs/" in content
    assert ".env" in content


def test_gitignore_appended_when_present(target: Path) -> None:
    """Existing .gitignore is appended to, not overwritten."""
    existing = "node_modules/\n*.log\n"
    (target / ".gitignore").write_text(existing, encoding="utf-8")

    result = scaffold(target, no_interview=True)

    assert result.gitignore_created is False
    assert result.gitignore_changed is True
    content = (target / ".gitignore").read_text(encoding="utf-8")
    assert "node_modules/" in content  # preserved
    assert "*.log" in content  # preserved
    assert ".forge/runs/" in content  # added
    assert "# >>> forge init <<<" in content


def test_gitignore_marker_block_idempotent_in_principle(target: Path) -> None:
    """If our marker is already there, we don't append again.

    Currently unreachable in MVP (scaffold refuses re-init), but the
    helper itself is defensive. We test it directly via the lower-level
    function so the second-init invariant is documented in code.
    """
    from forge.init_scaffold import _update_gitignore

    path = target / ".gitignore"
    path.write_text("# >>> forge init <<<\n.forge/runs/\n# <<< forge init >>>\n", encoding="utf-8")
    before = path.read_text(encoding="utf-8")
    created, changed = _update_gitignore(path)
    assert created is False
    assert changed is False
    assert path.read_text(encoding="utf-8") == before


# ---------------------------------------------------------------------------
# Idempotency / re-init
# ---------------------------------------------------------------------------


def test_reinit_refused(target: Path) -> None:
    """Second scaffold raises ScaffoldError with a clear message."""
    scaffold(target, no_interview=True)
    with pytest.raises(ScaffoldError, match=r"already exists"):
        scaffold(target, no_interview=True)


def test_reinit_error_message_lists_files_to_edit(target: Path) -> None:
    """Error message must point at the files to edit instead of re-running."""
    scaffold(target, no_interview=True)
    with pytest.raises(ScaffoldError) as ei:
        scaffold(target, no_interview=True)
    msg = str(ei.value)
    assert "config.toml" in msg
    assert "architecture.md" in msg
    assert "personas/" in msg


# ---------------------------------------------------------------------------
# Target validation
# ---------------------------------------------------------------------------


def test_missing_target_raises(tmp_path: Path) -> None:
    bogus = tmp_path / "does-not-exist"
    with pytest.raises(ScaffoldError, match=r"does not exist"):
        scaffold(bogus, no_interview=True)


def test_target_is_file_not_dir(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("x")
    with pytest.raises(ScaffoldError, match=r"not a directory"):
        scaffold(f, no_interview=True)
