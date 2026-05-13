"""Scaffold logic for `forge init`.

Stage 8. Single responsibility: take a target directory and lay down
the `.forge/` structure plus `.env.example`, append to `.gitignore`,
and refuse if `.forge/` already exists (MVP — re-init not supported,
deferred to Stage 9 via `--force`).

The interview and the architect call live elsewhere (`forge.interview`,
`forge.agents.architect`). This module only handles the filesystem
layout. Keeping the concerns split lets us test scaffolding without
any LLM mocking, and lets `forge init --no-interview` skip everything
LLM-related cleanly.

Templates are bundled as package data under `src/forge/templates/`.
We read them via `importlib.resources` so it works equally well from
a wheel install and from an editable checkout.
"""

from __future__ import annotations

from dataclasses import dataclass
from importlib import resources
from pathlib import Path

# ---------------------------------------------------------------------------
# Constants — kept in sync with cli.DEFAULT_* by way of being the source of
# truth here. cli.py imports nothing from this module yet, but if it ever
# does, paths line up.
# ---------------------------------------------------------------------------

#: Directory inside the package containing template files.
_TEMPLATES_ANCHOR = "forge.templates"

#: Marker block we append to .gitignore so we can recognize it on re-runs
#: (not used in MVP since re-init is forbidden, but cheap to put down now).
_GITIGNORE_MARKER_START = "# >>> forge init <<<"
_GITIGNORE_MARKER_END = "# <<< forge init >>>"

#: Lines added between the markers. Each entry comes with a one-line comment
#: above it so a curious developer reading .gitignore later understands why.
_GITIGNORE_ENTRIES = [
    "# Forge per-run state (event logs, RunState snapshots, reports)",
    ".forge/runs/",
    "",
    "# API keys and other secrets (use .env.example as a template)",
    ".env",
]


class ScaffoldError(Exception):
    """Raised when the scaffold cannot proceed safely.

    Examples:
        - .forge/ already exists in the target (idempotency guard)
        - target path is not a directory
        - permission denied while writing
    """


@dataclass(frozen=True)
class ScaffoldResult:
    """Summary of what `scaffold()` did, for the CLI to print back.

    `gitignore_changed` is False either because the file already had
    our markers (defence-in-depth, currently unreachable in MVP) or
    because the file did not exist and we created a fresh one with
    our entries — in the latter case `gitignore_created` is True.
    """

    forge_dir: Path
    env_example_path: Path
    gitignore_path: Path
    gitignore_created: bool
    gitignore_changed: bool
    architecture_path: Path
    architecture_is_template: bool


def scaffold(
    target: Path,
    *,
    no_interview: bool,
) -> ScaffoldResult:
    """Lay down the .forge/ scaffold in `target`.

    Args:
        target: Directory to scaffold into. Must exist and be a
            directory; created files go directly under it. We do NOT
            create the target itself — if the user passes a non-existent
            path we fail loudly. (Passing a path that doesn't exist is
            almost always a typo or a misunderstanding.)
        no_interview: If True, copy `architecture.md.template` directly
            to `.forge/knowledge/architecture.md` so the user can fill
            it in by hand. If False, leave `architecture.md` ABSENT —
            the caller (cmd_init) is responsible for running the
            interview + architect agent and writing the file itself.

    Returns:
        ScaffoldResult describing every file/dir touched.

    Raises:
        ScaffoldError: If `target` is missing or not a directory, or
            if `.forge/` already exists (MVP idempotency policy).
    """
    target = target.resolve()
    if not target.exists():
        raise ScaffoldError(
            f"Target directory does not exist: {target}. "
            f"Create it first (e.g. `mkdir -p {target}`) and rerun."
        )
    if not target.is_dir():
        raise ScaffoldError(
            f"Target path is not a directory: {target}."
        )

    forge_dir = target / ".forge"
    if forge_dir.exists():
        raise ScaffoldError(
            f".forge/ already exists at {forge_dir}.\n"
            f"Re-initialization is not supported in MVP.\n"
            f"To modify configuration, edit files directly:\n"
            f"  - {forge_dir}/config.toml          (model assignments, limits)\n"
            f"  - {forge_dir}/knowledge/architecture.md  (architecture map)\n"
            f"  - {forge_dir}/personas/*.md         (agent prompts)\n"
            f"  - {forge_dir}/git_flow.md           (git rules)"
        )

    # ---- Copy the .forge/ tree from package templates ----
    _copy_template_tree(_TEMPLATES_ANCHOR + ".forge_dir", forge_dir)

    # config.example.toml -> config.toml (so `forge plan` works out of the box)
    example_cfg = forge_dir / "config.example.toml"
    if example_cfg.exists():
        (forge_dir / "config.toml").write_bytes(example_cfg.read_bytes())

    # Decide what to do about architecture.md.
    #
    # We always copy the template down as architecture.md.template (so
    # users can reset to a known-good shape later by hand). Whether the
    # *real* architecture.md materializes here depends on the flag:
    #
    #   --no-interview   -> copy the template to architecture.md so
    #                       `forge plan` has SOMETHING to read. User
    #                       must fill the TODOs before a real run.
    #
    #   (interview path) -> do NOT create architecture.md here. The
    #                       caller will write the architect's output to
    #                       that path after the interview completes.
    knowledge_dir = forge_dir / "knowledge"
    knowledge_dir.mkdir(exist_ok=True)
    arch_path = knowledge_dir / "architecture.md"
    template_path = knowledge_dir / "architecture.md.template"

    if no_interview and template_path.exists():
        arch_path.write_bytes(template_path.read_bytes())
        arch_is_template = True
    else:
        arch_is_template = False  # caller will fill it in

    # ---- .env.example at the project root ----
    env_example_src = resources.files(_TEMPLATES_ANCHOR).joinpath("env.example")
    env_example_dst = target / ".env.example"
    env_example_dst.write_bytes(env_example_src.read_bytes())

    # ---- .gitignore: append our block, or create one with just our block ----
    gitignore_path = target / ".gitignore"
    gitignore_created, gitignore_changed = _update_gitignore(gitignore_path)

    return ScaffoldResult(
        forge_dir=forge_dir,
        env_example_path=env_example_dst,
        gitignore_path=gitignore_path,
        gitignore_created=gitignore_created,
        gitignore_changed=gitignore_changed,
        architecture_path=arch_path,
        architecture_is_template=arch_is_template,
    )


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _copy_template_tree(anchor: str, dest: Path) -> None:
    """Recursively copy the package-data tree at `anchor` into `dest`.

    `anchor` is a dotted module path (e.g. "forge.templates.forge_dir")
    that `importlib.resources.files()` resolves to a Traversable. We
    walk that tree and replicate it under `dest`.

    Why not shutil.copytree? Because the source is package data, which
    on a wheel install can live inside a zipimport — shutil walks
    real filesystem paths only. importlib.resources is the supported
    way to read package data portably (PEP 691, Python 3.9+).

    We skip the .template suffix files at copy time — they're kept in
    the tree for the --no-interview branch but should not pollute the
    scaffolded project at top-level. (The architecture.md.template
    file lives under knowledge/ and IS copied; the suffix is fine
    there as documentation of intent.)
    """
    dest.mkdir(parents=True, exist_ok=True)
    root = resources.files(anchor)
    for entry in root.iterdir():
        _copy_entry(entry, dest)


def _copy_entry(entry, dest: Path) -> None:
    """Recursive helper for `_copy_template_tree`.

    Traversable is duck-typed: `is_dir()`, `is_file()`, `iterdir()`,
    `name`, `read_bytes()`. We don't import the protocol class to
    keep imports small.
    """
    target = dest / entry.name
    if entry.is_dir():
        target.mkdir(exist_ok=True)
        for child in entry.iterdir():
            _copy_entry(child, target)
    else:
        target.write_bytes(entry.read_bytes())


def _update_gitignore(path: Path) -> tuple[bool, bool]:
    """Add the forge block to .gitignore, creating the file if absent.

    Returns (created, changed). `created` is True iff the file did not
    exist before. `changed` is True iff we appended/wrote our block —
    False only if the marker block is already present verbatim, which
    means a previous `forge init` ran here (or the user copied the
    block by hand).

    We don't try to merge with existing similar entries (e.g. the user
    already has `.env` listed somewhere else). The marker block is
    self-contained and harmless even if duplicates exist elsewhere —
    git uses the union.
    """
    block_lines = [_GITIGNORE_MARKER_START, *_GITIGNORE_ENTRIES, _GITIGNORE_MARKER_END]
    block = "\n".join(block_lines) + "\n"

    if not path.exists():
        path.write_text(block, encoding="utf-8")
        return True, True

    existing = path.read_text(encoding="utf-8")
    if _GITIGNORE_MARKER_START in existing:
        # Already initialized. Don't touch.
        return False, False

    # Append, separated by a blank line if the file doesn't already end with one.
    separator = "" if existing.endswith("\n\n") else ("\n" if existing.endswith("\n") else "\n\n")
    path.write_text(existing + separator + block, encoding="utf-8")
    return False, True
