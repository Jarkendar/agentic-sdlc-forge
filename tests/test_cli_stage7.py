"""CLI tests for `forge run` and `forge report` (Stage 7).

Scope:
- Parser accepts the new subcommands with correct arguments
- `forge run` argument-combination validation (mutually exclusive
  --resume vs user_story, neither given → error)

Deeper integration tests that exercise load_config / load_all_personas /
get_client live in the production repo's test_cli.py alongside the
existing plan/execute/verify suites. This file covers the Stage 7
parser additions and the two early-exit branches that don't touch
those loaders.
"""

from __future__ import annotations

import argparse
from pathlib import Path

import pytest

import forge.cli as cli


# ---------------------------------------------------------------------------
# Parser shape
# ---------------------------------------------------------------------------


def test_parser_accepts_run_with_user_story() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["run", "fix the parser"])
    assert args.user_story == "fix the parser"
    assert args.resume is None
    assert args.func is cli.cmd_run


def test_parser_accepts_run_with_resume() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["run", "--resume", "20260101-120000-abcdef"])
    assert args.user_story is None
    assert args.resume == "20260101-120000-abcdef"


def test_parser_accepts_report_command() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["report", "20260101-120000-abcdef"])
    assert args.run_id == "20260101-120000-abcdef"
    assert args.func is cli.cmd_report


def test_parser_run_supports_architecture_override() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["run", "story", "--architecture", "/tmp/arch.md"]
    )
    assert args.architecture == Path("/tmp/arch.md")


def test_parser_run_supports_config_override() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(
        ["run", "story", "--config", "/tmp/cfg.toml"]
    )
    assert args.config == Path("/tmp/cfg.toml")


def test_parser_report_supports_repo_override() -> None:
    parser = cli.build_parser()
    args = parser.parse_args(["report", "rid", "--repo", "/tmp/r"])
    assert args.repo == Path("/tmp/r")


# ---------------------------------------------------------------------------
# Argument-combination validation
# (Only exercises the upfront branches before config loaders are called.)
# ---------------------------------------------------------------------------


def _build_run_args(
    repo: Path,
    *,
    user_story: str | None = None,
    resume: str | None = None,
) -> argparse.Namespace:
    return argparse.Namespace(
        user_story=user_story,
        resume=resume,
        config=None,
        repo=repo,
        architecture=None,
        func=cli.cmd_run,
    )


def test_cmd_run_without_story_or_resume_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _build_run_args(tmp_path)
    rc = cli.cmd_run(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "user_story" in err or "--resume" in err


def test_cmd_run_with_both_story_and_resume_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = _build_run_args(tmp_path, user_story="x", resume="some-run")
    rc = cli.cmd_run(args)
    assert rc == 1
    err = capsys.readouterr().err
    assert "mutually exclusive" in err.lower() or "either" in err.lower()
