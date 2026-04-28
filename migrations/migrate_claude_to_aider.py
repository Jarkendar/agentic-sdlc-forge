#!/usr/bin/env python3
"""
Migrate Claude Code configuration to Aider.

Scope:
  - Project level: ./.claude/ + ./CLAUDE.md  ->  ./CONVENTIONS.md + ./.aider.conf.yml
  - User global:   ~/.claude/                ->  ~/.aider.conf.yml + ~/.config/aider/

What maps cleanly:
  - CLAUDE.md             -> CONVENTIONS.md (included via `read:` in aider config)
  - settings.json (model) -> .aider.conf.yml `model:`

What does NOT map (no Aider equivalent) -> archived to docs/claude-migration/:
  - commands/  (slash commands)
  - agents/    (sub-agents)
  - skills/    (skills with SKILL.md)
  - hooks      (lifecycle hooks from settings.json)
  - permissions rules from settings.json (Aider has no equivalent)

Usage:
  migrate_claude_to_aider.py --project PATH [--home PATH] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional


# ----- Report ----------------------------------------------------------------

@dataclass
class MigrationReport:
    """Collects every action so we can write a human-readable summary at the end."""
    mapped: list[str] = field(default_factory=list)           # clean mappings
    archived: list[str] = field(default_factory=list)         # moved to docs/
    manual_action: list[str] = field(default_factory=list)    # user must handle
    skipped: list[str] = field(default_factory=list)          # source missing
    warnings: list[str] = field(default_factory=list)

    def render(self) -> str:
        out: list[str] = []
        out.append("# Claude Code -> Aider Migration Report\n")
        out.append("Generated automatically. Review before committing.\n")

        def section(title: str, items: list[str]) -> None:
            out.append(f"\n## {title}\n")
            if not items:
                out.append("_(none)_\n")
            else:
                for item in items:
                    out.append(f"- {item}")
                out.append("")  # trailing blank line

        section("Mapped cleanly", self.mapped)
        section("Archived to docs/claude-migration/ (no Aider equivalent)", self.archived)
        section("Needs manual action", self.manual_action)
        section("Skipped (source not found)", self.skipped)
        section("Warnings", self.warnings)

        out.append("\n## Feature compatibility matrix\n")
        out.append("| Claude Code feature | Aider equivalent | Status |")
        out.append("|---|---|---|")
        out.append("| `CLAUDE.md` | `CONVENTIONS.md` via `read:` | full |")
        out.append("| `settings.json` → `model` | `.aider.conf.yml` `model:` | full |")
        out.append("| `settings.json` → `permissions` | *none* | **manual** — Aider uses `--yes` / per-command confirmation |")
        out.append("| `settings.json` → `hooks` | *none* | **manual** — use git hooks or shell wrappers |")
        out.append("| `commands/*.md` | *none* | **manual** — rewrite as shell aliases or prompt snippets |")
        out.append("| `agents/*.md` | *none* | **manual** — single-agent model; fold into CONVENTIONS.md if needed |")
        out.append("| `skills/*/SKILL.md` | *none* | **manual** — include directly via `/read-only` or paste into prompts |")
        out.append("| `projects/*.jsonl` (session history) | *none* | skipped — not portable |")
        return "\n".join(out)


# ----- Core migration --------------------------------------------------------

class Migrator:
    def __init__(self, *, dry_run: bool = False, verbose: bool = True) -> None:
        self.dry_run = dry_run
        self.verbose = verbose
        self.report = MigrationReport()

    # --- filesystem helpers ---

    def _log(self, msg: str) -> None:
        if self.verbose:
            prefix = "[dry-run] " if self.dry_run else ""
            print(f"{prefix}{msg}", file=sys.stderr)

    def _write_text(self, path: Path, content: str) -> None:
        self._log(f"write  {path}")
        if self.dry_run:
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _copy_tree(self, src: Path, dst: Path) -> None:
        self._log(f"copy   {src}  ->  {dst}")
        if self.dry_run:
            return
        dst.parent.mkdir(parents=True, exist_ok=True)
        if src.is_dir():
            # copytree fails if dst exists; merge manually
            for item in src.rglob("*"):
                rel = item.relative_to(src)
                target = dst / rel
                if item.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                else:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(item, target)
        else:
            shutil.copy2(src, dst)

    # --- mapping steps ---

    def _resolve_claude_md(self, root: Path, scope_label: str) -> Optional[Path]:
        """
        Claude Code reads CLAUDE.md from two places:
          - <root>/CLAUDE.md   (what CC actually loads for projects)
          - <root>/.claude/CLAUDE.md (some setups put it here)
        Prefer the first; fall back to the second; warn if both exist.
        """
        primary = root / "CLAUDE.md"
        secondary = root / ".claude" / "CLAUDE.md"
        if primary.exists() and secondary.exists():
            self.report.warnings.append(
                f"{scope_label}: both `CLAUDE.md` and `.claude/CLAUDE.md` exist; "
                f"using `{primary}` and archiving the other."
            )
            return primary
        if primary.exists():
            return primary
        if secondary.exists():
            return secondary
        return None

    def _extract_model(self, settings: dict) -> Optional[str]:
        # CC stores it under "model" at the top level in current schemas
        model = settings.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()
        return None

    def _build_aider_conf(self, *, model: Optional[str], conventions_path: Optional[str]) -> str:
        """
        Build a minimal .aider.conf.yml. We intentionally write YAML by hand
        (no PyYAML dependency) since the surface is tiny.
        """
        lines: list[str] = [
            "# Generated by migrate_claude_to_aider.py",
            "# See https://aider.chat/docs/config/aider_conf.html for all options.",
            "",
        ]
        if model:
            # CC model names don't always match Aider's expected identifiers;
            # leave as-is and flag in report so the user can adjust.
            lines.append(f"model: {model}")
        if conventions_path:
            lines.append("read:")
            lines.append(f"  - {conventions_path}")
        lines.append("")
        return "\n".join(lines)

    # --- scope handlers ---

    def migrate_project(self, project_root: Path) -> None:
        scope = "project"
        claude_dir = project_root / ".claude"
        has_anything = (project_root / "CLAUDE.md").exists() or claude_dir.exists()
        if not has_anything:
            self.report.skipped.append(f"{scope}: no Claude Code config found at `{project_root}`")
            return

        archive_dir = project_root / "docs" / "claude-migration"

        # 1. CLAUDE.md -> CONVENTIONS.md
        claude_md = self._resolve_claude_md(project_root, scope)
        conventions_written = False
        if claude_md is not None:
            conventions_path = project_root / "CONVENTIONS.md"
            header = (
                "<!-- Migrated from Claude Code.\n"
                f"     Source: {claude_md.relative_to(project_root)} -->\n\n"
            )
            self._write_text(conventions_path, header + claude_md.read_text(encoding="utf-8"))
            self.report.mapped.append(
                f"{scope}: `{claude_md.relative_to(project_root)}` -> `CONVENTIONS.md`"
            )
            conventions_written = True
            # Also archive the secondary if both existed
            secondary = project_root / ".claude" / "CLAUDE.md"
            if claude_md != secondary and secondary.exists():
                self._copy_tree(secondary, archive_dir / "CLAUDE.secondary.md")
                self.report.archived.append(
                    f"{scope}: `.claude/CLAUDE.md` -> `docs/claude-migration/CLAUDE.secondary.md`"
                )

        # 2. settings.json
        model: Optional[str] = None
        settings_file = claude_dir / "settings.json"
        local_settings_file = claude_dir / "settings.local.json"
        for sf in (settings_file, local_settings_file):
            if not sf.exists():
                continue
            try:
                data = json.loads(sf.read_text(encoding="utf-8"))
            except json.JSONDecodeError as e:
                self.report.warnings.append(f"{scope}: cannot parse `{sf.name}` ({e}); archiving raw.")
                self._copy_tree(sf, archive_dir / sf.name)
                continue

            m = self._extract_model(data)
            if m and not model:  # settings.local.json wins only if main had no model
                model = m

            # Always archive the raw file for reference (permissions, hooks, env, etc.)
            self._copy_tree(sf, archive_dir / sf.name)
            self.report.archived.append(f"{scope}: `.claude/{sf.name}` -> `docs/claude-migration/{sf.name}`")

            if "permissions" in data:
                self.report.manual_action.append(
                    f"{scope}: `permissions` rules from `{sf.name}` have no Aider equivalent — "
                    "Aider confirms tool calls interactively or via `--yes` / `--no-auto-commits`."
                )
            if "hooks" in data:
                self.report.manual_action.append(
                    f"{scope}: `hooks` from `{sf.name}` must be reimplemented as git hooks, "
                    "Aider `--lint-cmd` / `--test-cmd`, or shell wrappers."
                )

        # 3. .aider.conf.yml
        conf_content = self._build_aider_conf(
            model=model,
            conventions_path="CONVENTIONS.md" if conventions_written else None,
        )
        self._write_text(project_root / ".aider.conf.yml", conf_content)
        self.report.mapped.append(f"{scope}: `.aider.conf.yml` written (model={model or 'default'})")

        if model:
            self.report.warnings.append(
                f"{scope}: model `{model}` copied verbatim. "
                "Verify it matches an Aider-supported identifier (see `aider --list-models`)."
            )

        # 4. Archive unmappable directories
        for sub in ("commands", "agents", "skills", "hooks"):
            src = claude_dir / sub
            if src.exists() and any(src.iterdir()):
                self._copy_tree(src, archive_dir / sub)
                self.report.archived.append(f"{scope}: `.claude/{sub}/` -> `docs/claude-migration/{sub}/`")
                self.report.manual_action.append(
                    f"{scope}: `{sub}` has no Aider equivalent — review `docs/claude-migration/{sub}/` "
                    "and decide per-item (see compatibility matrix below)."
                )

        # 5. .aiderignore scaffold (include .claude-archive if we kept raw copy)
        aiderignore = project_root / ".aiderignore"
        if not aiderignore.exists():
            self._write_text(aiderignore, ".claude/\n")
            self.report.mapped.append(f"{scope}: `.aiderignore` created (excludes `.claude/`)")

    def migrate_home(self, home_root: Path) -> None:
        scope = "user-global"
        claude_dir = home_root / ".claude"
        if not claude_dir.exists():
            self.report.skipped.append(f"{scope}: no `~/.claude/` at `{home_root}`")
            return

        archive_dir = home_root / ".config" / "aider" / "claude-migration"

        # 1. Global CLAUDE.md -> ~/.aider.conventions.md
        global_md = claude_dir / "CLAUDE.md"
        conventions_target: Optional[Path] = None
        if global_md.exists():
            conventions_target = home_root / ".aider.conventions.md"
            header = "<!-- Migrated from ~/.claude/CLAUDE.md -->\n\n"
            self._write_text(conventions_target, header + global_md.read_text(encoding="utf-8"))
            self.report.mapped.append(f"{scope}: `~/.claude/CLAUDE.md` -> `~/.aider.conventions.md`")

        # 2. Global settings.json
        model: Optional[str] = None
        settings_file = claude_dir / "settings.json"
        if settings_file.exists():
            try:
                data = json.loads(settings_file.read_text(encoding="utf-8"))
                model = self._extract_model(data)
            except json.JSONDecodeError as e:
                self.report.warnings.append(f"{scope}: cannot parse global `settings.json` ({e}).")
            self._copy_tree(settings_file, archive_dir / "settings.json")
            self.report.archived.append(
                f"{scope}: `~/.claude/settings.json` -> `~/.config/aider/claude-migration/settings.json`"
            )

        # 3. ~/.aider.conf.yml
        #    Use absolute path for read: since aider resolves relative paths from CWD.
        conf = self._build_aider_conf(
            model=model,
            conventions_path=str(conventions_target) if conventions_target else None,
        )
        self._write_text(home_root / ".aider.conf.yml", conf)
        self.report.mapped.append(f"{scope}: `~/.aider.conf.yml` written (model={model or 'default'})")

        # 4. Archive unmappable
        for sub in ("commands", "agents", "skills", "hooks"):
            src = claude_dir / sub
            if src.exists() and any(src.iterdir()):
                self._copy_tree(src, archive_dir / sub)
                self.report.archived.append(
                    f"{scope}: `~/.claude/{sub}/` -> `~/.config/aider/claude-migration/{sub}/`"
                )
                self.report.manual_action.append(
                    f"{scope}: global `{sub}` archived — Aider has no equivalent."
                )

        # 5. projects/ (session history) - explicitly skip, not portable
        projects_dir = claude_dir / "projects"
        if projects_dir.exists() and any(projects_dir.iterdir()):
            self.report.skipped.append(
                f"{scope}: `~/.claude/projects/` (session history JSONL) not migrated — "
                "Aider stores history per-project in `.aider.chat.history.md`."
            )

    # --- entrypoint ---

    def write_report(self, target: Path) -> None:
        self._write_text(target, self.report.render())


def main() -> int:
    parser = argparse.ArgumentParser(description="Migrate Claude Code -> Aider")
    parser.add_argument("--project", type=Path, required=True,
                        help="Project root (containing CLAUDE.md and/or .claude/)")
    parser.add_argument("--home", type=Path, default=None,
                        help="Home directory containing .claude/ (default: $HOME, pass empty to skip)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Print actions without writing")
    parser.add_argument("--report", type=Path, default=None,
                        help="Where to write MIGRATION_REPORT.md (default: <project>/docs/claude-migration/MIGRATION_REPORT.md)")
    parser.add_argument("--skip-home", action="store_true",
                        help="Skip user-global migration")
    args = parser.parse_args()

    project_root = args.project.expanduser().resolve()
    if not project_root.exists():
        print(f"error: project root does not exist: {project_root}", file=sys.stderr)
        return 2

    m = Migrator(dry_run=args.dry_run)
    m.migrate_project(project_root)

    if not args.skip_home:
        home_root = (args.home or Path.home()).expanduser().resolve()
        m.migrate_home(home_root)

    report_path = args.report or (project_root / "docs" / "claude-migration" / "MIGRATION_REPORT.md")
    m.write_report(report_path)

    # Also print a short summary to stdout
    r = m.report
    print("Migration summary:")
    print(f"  mapped:        {len(r.mapped)}")
    print(f"  archived:      {len(r.archived)}")
    print(f"  manual action: {len(r.manual_action)}")
    print(f"  skipped:       {len(r.skipped)}")
    print(f"  warnings:      {len(r.warnings)}")
    print(f"  report:        {report_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
