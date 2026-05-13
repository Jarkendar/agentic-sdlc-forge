"""Tests for cli.cmd_init.

Drives cmd_init via build_parser + the handler, with the architect's
LLM call stubbed via monkeypatching forge.cli.run_architect. The
interview is similarly bypassed for the interactive path.

Coverage:
- --no-interview path: scaffold runs, architecture.md is the template,
  exit 0
- second run: scaffold-side ScaffoldError → exit 1
- missing target dir → exit 1
- interview path: Interview is constructed and run, architect is called
  with the result, architecture.md is written, exit 0
- missing API key for interview path → exit 1 (no architect call)
- KeyboardInterrupt during interview → exit 130, scaffold artifacts remain
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from forge import cli
from forge.interview import InterviewResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run(argv: list[str]) -> int:
    """Run the CLI via build_parser + the registered handler. Returns exit code."""
    parser = cli.build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


# ---------------------------------------------------------------------------
# --no-interview path
# ---------------------------------------------------------------------------


def test_no_interview_scaffolds_and_exits_zero(tmp_path: Path, capsys: Any) -> None:
    """End-to-end: scaffold succeeds, template arch.md is in place, exit 0."""
    rc = _run(["init", "--target", str(tmp_path), "--no-interview"])
    assert rc == 0

    arch_path = tmp_path / ".forge" / "knowledge" / "architecture.md"
    assert arch_path.is_file()
    # Template stub should mention TODO
    content = arch_path.read_text(encoding="utf-8")
    assert "TODO" in content

    out = capsys.readouterr().out
    assert "Scaffolded" in out
    assert "Skipped interview" in out


def test_no_interview_does_not_call_architect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """--no-interview must NOT invoke the architect (no LLM call, no key needed)."""
    called: dict[str, bool] = {"value": False}

    def fail_if_called(*_a: object, **_k: object) -> str:
        called["value"] = True
        return ""

    monkeypatch.setattr(cli, "run_architect", fail_if_called)
    # Even with the env var blank we should succeed
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    rc = _run(["init", "--target", str(tmp_path), "--no-interview"])
    assert rc == 0
    assert called["value"] is False


def test_reinit_exit_1(tmp_path: Path, capsys: Any) -> None:
    """Second init in the same dir → ScaffoldError → exit 1."""
    rc1 = _run(["init", "--target", str(tmp_path), "--no-interview"])
    assert rc1 == 0
    rc2 = _run(["init", "--target", str(tmp_path), "--no-interview"])
    assert rc2 == 1
    err = capsys.readouterr().err
    assert "already exists" in err


def test_missing_target_dir_exit_1(tmp_path: Path, capsys: Any) -> None:
    """Target doesn't exist → exit 1 with a clear message."""
    rc = _run(["init", "--target", str(tmp_path / "nope"), "--no-interview"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "does not exist" in err


# ---------------------------------------------------------------------------
# Interview path (with architect + interview stubbed)
# ---------------------------------------------------------------------------


def test_interview_path_writes_architecture_md(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    """Happy path with interview: arch.md is written from architect's output."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-key-not-real")

    # Stub the AnthropicClient constructor — we won't actually call it,
    # but cmd_init imports it. Replace with a sentinel object so the
    # equality check elsewhere doesn't trip.
    class FakeClient:
        def __init__(self, model: str) -> None:
            self.model = model

    # The import happens inside cmd_init under `from forge.llm.anthropic_client
    # import AnthropicClient`. Patch the module symbol.
    import forge.llm.anthropic_client as ac_mod
    monkeypatch.setattr(ac_mod, "AnthropicClient", FakeClient)

    # Stub the Interview class so we don't need to script terminal input.
    fake_answers = {
        "1.1": "FakeProject",
        "1.2": "A test project.",
        "2.1": "Python",
        "3.1": "MVI",
    }

    class FakeInterview:
        def __init__(self, *_a: object, **_k: object) -> None: ...

        def run(self, *, repo: Path, default_project_name: str) -> InterviewResult:
            return InterviewResult(project_name="FakeProject", answers=fake_answers)

    monkeypatch.setattr(cli, "Interview", FakeInterview)

    # Stub run_architect — verify it gets the persona + answers.
    captured: dict[str, object] = {}

    def fake_run_architect(
        *,
        project_name: str,
        answers: dict[str, str],
        persona: Any,
        llm: Any,
        **_kw: object,
    ) -> str:
        captured["project_name"] = project_name
        captured["answers"] = answers
        captured["persona_name"] = persona.name
        captured["llm_type"] = type(llm).__name__
        return "# Architecture Map — FakeProject\n\n## 1. ... etc.\n"

    monkeypatch.setattr(cli, "run_architect", fake_run_architect)

    rc = _run(["init", "--target", str(tmp_path)])
    assert rc == 0

    # arch.md was written
    arch_path = tmp_path / ".forge" / "knowledge" / "architecture.md"
    assert arch_path.is_file()
    assert "FakeProject" in arch_path.read_text(encoding="utf-8")

    # The architect was called with the interview's results
    assert captured["project_name"] == "FakeProject"
    assert captured["answers"] == fake_answers
    assert captured["persona_name"] == "architect"
    assert captured["llm_type"] == "FakeClient"

    out = capsys.readouterr().out
    assert "Synthesizing" in out
    assert "Wrote" in out


def test_missing_api_key_exit_1_before_architect(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    """Interview runs, but no API key → exit 1 BEFORE architect call."""
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    # find_dotenv would walk up from cwd; pin to nothing
    import forge.cli as cli_mod
    # cmd_init imports dotenv inline; patch by also clearing any .env in tmp_path
    # tmp_path is pristine, so find_dotenv won't find one.
    monkeypatch.chdir(tmp_path)

    class FakeInterview:
        def __init__(self, *_a: object, **_k: object) -> None: ...

        def run(self, *, repo: Path, default_project_name: str) -> InterviewResult:
            return InterviewResult(project_name="P", answers={"1.1": "P"})

    monkeypatch.setattr(cli_mod, "Interview", FakeInterview)

    architect_called = {"v": False}

    def fail_arch(**_kw: object) -> str:
        architect_called["v"] = True
        return ""

    monkeypatch.setattr(cli_mod, "run_architect", fail_arch)

    rc = _run(["init", "--target", str(tmp_path)])
    assert rc == 1
    assert architect_called["v"] is False
    err = capsys.readouterr().err
    assert "ANTHROPIC_API_KEY" in err


def test_keyboard_interrupt_during_interview_exits_130(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    """Ctrl+C during interview → exit 130, scaffolded files remain."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    class AbortingInterview:
        def __init__(self, *_a: object, **_k: object) -> None: ...

        def run(self, *, repo: Path, default_project_name: str) -> InterviewResult:
            raise KeyboardInterrupt()

    monkeypatch.setattr(cli, "Interview", AbortingInterview)

    rc = _run(["init", "--target", str(tmp_path)])
    assert rc == 130

    # Scaffold artifacts still present
    assert (tmp_path / ".forge" / "personas" / "architect.md").is_file()
    assert (tmp_path / ".env.example").is_file()

    err = capsys.readouterr().err
    assert "Aborted" in err


def test_architect_error_exits_1(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: Any,
) -> None:
    """ArchitectError from synthesis → exit 1 with the error in stderr."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "k")

    # Bypass the real Anthropic client construction (we won't call it)
    import forge.llm.anthropic_client as ac_mod
    monkeypatch.setattr(ac_mod, "AnthropicClient", lambda model: object())

    class FakeInterview:
        def __init__(self, *_a: object, **_k: object) -> None: ...

        def run(self, *, repo: Path, default_project_name: str) -> InterviewResult:
            return InterviewResult(project_name="P", answers={"1.1": "P"})

    monkeypatch.setattr(cli, "Interview", FakeInterview)

    from forge.agents.architect import ArchitectError

    def boom(**_kw: object) -> str:
        raise ArchitectError("synthesis failed")

    monkeypatch.setattr(cli, "run_architect", boom)

    rc = _run(["init", "--target", str(tmp_path)])
    assert rc == 1
    err = capsys.readouterr().err
    assert "synthesis failed" in err
