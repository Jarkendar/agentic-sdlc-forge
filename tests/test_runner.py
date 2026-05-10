"""Tests for forge.runner — per-task fix-loop driver.

Coverage:
- Pass on first attempt → status=success, attempts=1.
- Critical first, pass second → status=success, attempts=2.
- Critical on every attempt → status=escalated, attempts=cap, human_needed event.
- Executor failed → status=failed, no Verifier call, attempts=1.
- Executor no_changes → status=failed, no Verifier call.
- previous_failure flows from TestReport into the next Executor invocation.
- Warning / flaky severities count as success (non-blocking).
- build_failure_summary output shape.

Helpers are duplicated from test_executor / test_verifier per the project
rule against cross-module test imports. Keep these in sync if the helper
shapes change.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from pydantic import BaseModel

from forge.agents.verifier import CommandResult, run_verifier  # noqa: F401
from forge.aider_runner import AiderInvocation, AiderResult
from forge.event_log import EventLog
from forge.git_ops import ensure_run_branch
from forge.llm.base import LLMClient, LLMResponse
from forge.personas import Persona
from forge.runner import build_failure_summary, run_task_with_fix_loop
from forge.schemas import (
    Failure,
    Task,
    TestReport,
    VerificationCommand,
)

# ---------------------------------------------------------------------------
# Repo + git infrastructure (duplicated from test_executor)
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    return repo


# ---------------------------------------------------------------------------
# AiderRunner fake (duplicated from test_executor)
# ---------------------------------------------------------------------------


@dataclass
class _Edit:
    relpath: str
    content: str


@dataclass
class _ScriptedAiderResponse:
    edits: list[_Edit] = field(default_factory=list)
    exit_code: int = 0
    stdout: str = "ok"
    stderr: str = ""
    duration_ms: int = 100
    timed_out: bool = False
    skip_commit: bool = False


class FakeAiderRunner:
    """Same fake as test_executor: actually edits + commits to the repo."""

    def __init__(self, responses: list[_ScriptedAiderResponse]) -> None:
        self._responses = list(responses)
        self.invocations: list[AiderInvocation] = []

    def run(
        self,
        invocation: AiderInvocation,
        *,
        raise_on_timeout: bool = False,
    ) -> AiderResult:
        self.invocations.append(invocation)
        if not self._responses:
            raise AssertionError("FakeAiderRunner ran out of scripted responses")
        scripted = self._responses.pop(0)

        for edit in scripted.edits:
            target = invocation.cwd / edit.relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.content, encoding="utf-8")

        if scripted.edits and not scripted.skip_commit:
            subprocess.run(
                ["git", "add", "-A"], cwd=invocation.cwd, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", f"aider: edit {len(scripted.edits)} file(s)"],
                cwd=invocation.cwd, check=True, capture_output=True,
            )

        return AiderResult(
            exit_code=scripted.exit_code,
            stdout=scripted.stdout,
            stderr=scripted.stderr,
            duration_ms=scripted.duration_ms,
            timed_out=scripted.timed_out,
        )


# ---------------------------------------------------------------------------
# CommandRunner fake (duplicated from test_verifier)
# ---------------------------------------------------------------------------


class _FakeCommandRunner:
    def __init__(self, results: list[CommandResult]) -> None:
        self._results = list(results)
        self.calls: list[tuple[VerificationCommand, Path]] = []

    def run(self, command: VerificationCommand, cwd: Path) -> CommandResult:
        self.calls.append((command, cwd))
        if not self._results:
            raise AssertionError(
                f"FakeCommandRunner ran out of scripted results "
                f"(unexpected call for '{command.name}')"
            )
        return self._results.pop(0)


# ---------------------------------------------------------------------------
# LLM fake (duplicated from test_verifier)
# ---------------------------------------------------------------------------


class _ScriptedLLM(LLMClient):
    """Returns pre-baked TestReports in order."""

    provider = "fake"

    def __init__(self, reports: list[TestReport]) -> None:
        self._reports = list(reports)
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        self.calls.append({"system": system, "user": user, "schema": schema})
        if not self._reports:
            raise AssertionError("ScriptedLLM ran out of scripted reports")
        return LLMResponse(
            content=self._reports.pop(0),
            tokens_in=42,
            tokens_out=84,
            cost_usd=0.001,
            duration_ms=120,
            model="fake",
            provider="fake",
            finish_reason="end_turn",
            retried_validation=False,
        )


# ---------------------------------------------------------------------------
# Persona builder (duplicated from test_verifier)
# ---------------------------------------------------------------------------


def _verifier_persona() -> Persona:
    return Persona(
        name="verifier",
        output_schema=TestReport,
        required_vars=(
            "task_id",
            "command",
            "exit_code",
            "stdout",
            "stderr",
            "touched_files",
            "second_run_outcome",
        ),
        references=(),
        body=(
            "task={{task_id}} cmd={{command}} exit={{exit_code}} "
            "stdout={{stdout}} stderr={{stderr}} "
            "files={{touched_files}} second={{second_run_outcome}}"
        ),
        source_path=Path("verifier.md"),
    )


# ---------------------------------------------------------------------------
# Task / report / cmd helpers
# ---------------------------------------------------------------------------


def _task() -> Task:
    return Task(id="task-001", goal="Add foo.", files=[Path("src/foo.py")])


def _cmd(name: str = "pytest", command: str = "pytest -q") -> VerificationCommand:
    return VerificationCommand(
        name=name, command=command, stage="verify_test", timeout_seconds=60
    )


def _result_ok() -> CommandResult:
    return CommandResult(exit_code=0, stdout="", stderr="", duration_ms=10, timed_out=False)


def _result_fail(stderr: str = "AssertionError") -> CommandResult:
    return CommandResult(exit_code=1, stdout="", stderr=stderr, duration_ms=10, timed_out=False)


def _critical_report(message: str = "test_foo failed") -> TestReport:
    return TestReport(
        task_id="task-001",
        passed=False,
        failures=[
            Failure(
                stage="verify_test",
                command="pytest -q",
                exit_code=1,
                stdout_excerpt="",
                stderr_excerpt="AssertionError: 1 == 2",
                category="test",
                message=message,
            )
        ],
        severity="critical",
    )


def _none_report() -> TestReport:
    return TestReport(task_id="task-001", passed=True, failures=[], severity="none")


def _setup(tmp_path: Path) -> Path:
    repo = _make_repo(tmp_path)
    ensure_run_branch(repo, "run-1")
    return repo


def _drive(
    *,
    tmp_path: Path,
    aider_responses: list[_ScriptedAiderResponse],
    cmd_results: list[CommandResult],
    reports: list[TestReport],
    max_retries: int = 3,
    commands: list[VerificationCommand] | None = None,
):
    """Wire everything together. Returns (RunReport, log_path, aider, cmd_runner, llm)."""
    repo = _setup(tmp_path)
    log_path = tmp_path / "events.jsonl"
    aider = FakeAiderRunner(aider_responses)
    cmd_runner = _FakeCommandRunner(cmd_results)
    llm = _ScriptedLLM(reports)

    # Inject the fake CommandRunner via monkey-patching the verifier's
    # default. Easier than wiring an extra arg through run_task_with_fix_loop
    # for tests only — and keeps the production code path clean.
    import forge.agents.verifier as verifier_module

    real_runner_cls = verifier_module._RealCommandRunner
    verifier_module._RealCommandRunner = lambda: cmd_runner  # type: ignore[assignment,misc]

    try:
        with EventLog(log_path) as event_log:
            report = run_task_with_fix_loop(
                task=_task(),
                run_id="run-1",
                repo_root=repo,
                aider=aider,
                event_log=event_log,
                verifier_llm=llm,
                verifier_persona=_verifier_persona(),
                verification_commands=commands if commands is not None else [_cmd()],
                max_retries_per_task=max_retries,
            )
    finally:
        verifier_module._RealCommandRunner = real_runner_cls  # type: ignore[assignment]

    return report, log_path, aider, cmd_runner, llm


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_pass_on_first_attempt(tmp_path: Path) -> None:
    report, _, aider, cmd_runner, llm = _drive(
        tmp_path=tmp_path,
        aider_responses=[_ScriptedAiderResponse(edits=[_Edit("src/foo.py", "x\n")])],
        cmd_results=[_result_ok()],
        reports=[],  # all green → no LLM call expected
    )

    assert report.status == "success"
    assert report.attempts == 1
    assert report.escalation_reason is None
    assert report.final_test_report is not None
    assert report.final_test_report.severity == "none"
    assert len(aider.invocations) == 1
    assert len(cmd_runner.calls) == 1
    assert llm.calls == []


# ---------------------------------------------------------------------------
# Fix-loop iteration: critical → fix → pass
# ---------------------------------------------------------------------------


def test_critical_then_pass_resolves_in_two_attempts(tmp_path: Path) -> None:
    report, log_path, aider, _, _ = _drive(
        tmp_path=tmp_path,
        aider_responses=[
            _ScriptedAiderResponse(edits=[_Edit("src/foo.py", "broken\n")]),
            _ScriptedAiderResponse(edits=[_Edit("src/foo.py", "fixed\n")]),
        ],
        cmd_results=[_result_fail(), _result_ok()],
        reports=[_critical_report()],  # only one LLM call: second run is all-green
    )

    assert report.status == "success"
    assert report.attempts == 2
    assert len(aider.invocations) == 2

    # Second Aider invocation must carry the previous_failure summary.
    second_message = aider.invocations[1].message
    assert "Previous attempt failed" in second_message
    assert "test_foo failed" in second_message  # message from _critical_report

    # fix_loop_iteration event present
    phases = [e.phase for e in EventLog.read(log_path)]
    assert "fix_loop_iteration" in phases
    assert "task_success" in phases


def test_previous_failure_includes_command_and_stderr(tmp_path: Path) -> None:
    """Deterministic summary builder feeds command + stderr tail to next attempt."""
    report, _, aider, _, _ = _drive(
        tmp_path=tmp_path,
        aider_responses=[
            _ScriptedAiderResponse(edits=[_Edit("src/foo.py", "v1\n")]),
            _ScriptedAiderResponse(edits=[_Edit("src/foo.py", "v2\n")]),
        ],
        cmd_results=[_result_fail(), _result_ok()],
        reports=[_critical_report()],
    )

    assert report.status == "success"
    second_message = aider.invocations[1].message
    assert "pytest -q" in second_message  # command echoed
    assert "AssertionError" in second_message  # stderr_excerpt echoed


# ---------------------------------------------------------------------------
# Escalation: cap exhausted
# ---------------------------------------------------------------------------


def test_critical_on_every_attempt_escalates(tmp_path: Path) -> None:
    report, log_path, aider, _, llm = _drive(
        tmp_path=tmp_path,
        aider_responses=[
            _ScriptedAiderResponse(edits=[_Edit("src/foo.py", "v1\n")]),
            _ScriptedAiderResponse(edits=[_Edit("src/foo.py", "v2\n")]),
            _ScriptedAiderResponse(edits=[_Edit("src/foo.py", "v3\n")]),
        ],
        cmd_results=[_result_fail(), _result_fail(), _result_fail()],
        reports=[_critical_report(), _critical_report(), _critical_report()],
        max_retries=3,
    )

    assert report.status == "escalated"
    assert report.attempts == 3
    assert report.escalation_reason is not None
    assert "max_retries_per_task=3" in report.escalation_reason
    assert report.final_test_report is not None
    assert report.final_test_report.severity == "critical"

    phases = [e.phase for e in EventLog.read(log_path)]
    assert "human_needed" in phases
    assert phases.count("fix_loop_iteration") == 2  # between the 3 attempts


def test_max_retries_one_is_oneshot(tmp_path: Path) -> None:
    """max_retries_per_task=1 means one attempt, no fix loop iterations."""
    report, log_path, aider, _, _ = _drive(
        tmp_path=tmp_path,
        aider_responses=[_ScriptedAiderResponse(edits=[_Edit("src/foo.py", "v1\n")])],
        cmd_results=[_result_fail()],
        reports=[_critical_report()],
        max_retries=1,
    )

    assert report.status == "escalated"
    assert report.attempts == 1
    assert len(aider.invocations) == 1
    phases = [e.phase for e in EventLog.read(log_path)]
    assert "fix_loop_iteration" not in phases  # no iteration before escalation


def test_max_retries_zero_raises(tmp_path: Path) -> None:
    repo = _setup(tmp_path)
    log_path = tmp_path / "events.jsonl"
    aider = FakeAiderRunner([])
    llm = _ScriptedLLM([])

    with EventLog(log_path) as log, pytest.raises(ValueError, match="must be ≥1"):
        run_task_with_fix_loop(
            task=_task(),
            run_id="run-1",
            repo_root=repo,
            aider=aider,
            event_log=log,
            verifier_llm=llm,
            verifier_persona=_verifier_persona(),
            verification_commands=[_cmd()],
            max_retries_per_task=0,
        )


# ---------------------------------------------------------------------------
# Executor failures: no Verifier
# ---------------------------------------------------------------------------


def test_executor_no_changes_short_circuits(tmp_path: Path) -> None:
    """Aider made no edits → no commits → status='no_changes' → runner escalates
    without calling the Verifier."""
    report, log_path, _, cmd_runner, llm = _drive(
        tmp_path=tmp_path,
        # No edits = no commits on task branch = no_changes status
        aider_responses=[_ScriptedAiderResponse(edits=[], exit_code=0)],
        cmd_results=[],   # verifier not called
        reports=[],
    )

    assert report.status == "failed"
    assert report.attempts == 1
    assert report.final_test_report is None
    assert report.escalation_reason is not None
    assert "no_changes" in report.escalation_reason
    assert cmd_runner.calls == []
    assert llm.calls == []
    phases = [e.phase for e in EventLog.read(log_path)]
    assert "task_failed" in phases


def test_executor_failed_short_circuits(tmp_path: Path) -> None:
    """Aider non-zero exit → ExecutionResult.status='failed' → runner stops.

    NOTE: Our FakeAider commits on `edits` regardless of `exit_code`; with
    a non-zero exit AND edits, the executor still classifies as 'failed'
    (per executor.py: exit_code != 0 wins). We test that path here.
    """
    report, _, _, cmd_runner, llm = _drive(
        tmp_path=tmp_path,
        aider_responses=[
            _ScriptedAiderResponse(
                edits=[_Edit("src/foo.py", "x\n")],
                exit_code=2,
                stderr="aider crashed",
            )
        ],
        cmd_results=[],
        reports=[],
    )

    assert report.status == "failed"
    assert report.attempts == 1
    assert report.final_test_report is None
    assert cmd_runner.calls == []
    assert llm.calls == []


# ---------------------------------------------------------------------------
# Non-blocking severities
# ---------------------------------------------------------------------------


def test_warning_counts_as_success(tmp_path: Path) -> None:
    """Per persona: warnings are logged but non-blocking. Runner: success."""
    warning_report = TestReport(
        task_id="task-001",
        passed=False,
        failures=[
            Failure(
                stage="verify_lint",
                command="pytest -q",
                exit_code=1,
                category="lint",
                message="unrelated lint warning",
            )
        ],
        severity="warning",
    )

    report, _, aider, _, _ = _drive(
        tmp_path=tmp_path,
        aider_responses=[_ScriptedAiderResponse(edits=[_Edit("src/foo.py", "x\n")])],
        cmd_results=[_result_fail()],
        reports=[warning_report],
    )

    assert report.status == "success"
    assert report.attempts == 1
    assert len(aider.invocations) == 1
    assert report.final_test_report is not None
    assert report.final_test_report.severity == "warning"


def test_flaky_counts_as_success(tmp_path: Path) -> None:
    """Verifier already re-ran; final flaky is non-blocking at runner level."""
    flaky = _critical_report()
    flaky = flaky.model_copy(update={"severity": "flaky"})
    flaky_pass = TestReport(
        task_id="task-001",
        passed=False,
        failures=[
            Failure(
                stage="verify_test",
                command="pytest -q",
                exit_code=1,
                category="test",
                message="flaky test",
            )
        ],
        severity="flaky",
    )

    report, _, aider, cmd_runner, _ = _drive(
        tmp_path=tmp_path,
        aider_responses=[_ScriptedAiderResponse(edits=[_Edit("src/foo.py", "x\n")])],
        # Verifier sees fail, then pass on re-run (because it's testing flakiness)
        cmd_results=[_result_fail(), _result_ok()],
        # First LLM call: returns flaky+not_run → triggers re-run.
        # Second LLM call: returns flaky_pass with the second run outcome.
        reports=[flaky_pass, flaky_pass],
    )

    assert report.status == "success"
    assert report.attempts == 1
    assert len(aider.invocations) == 1
    # Two command invocations: original + flake check rerun
    assert len(cmd_runner.calls) == 2


# ---------------------------------------------------------------------------
# build_failure_summary
# ---------------------------------------------------------------------------


def test_build_failure_summary_basic_shape() -> None:
    report = TestReport(
        task_id="t1",
        passed=False,
        failures=[
            Failure(
                stage="verify_test",
                command="pytest tests/ -q",
                exit_code=1,
                stdout_excerpt="some stdout",
                stderr_excerpt="AssertionError: 1 == 2",
                category="test",
                message="test_login failed",
            )
        ],
        severity="critical",
    )

    summary = build_failure_summary(report)
    assert "test_login failed" in summary
    assert "pytest tests/ -q" in summary
    assert "exit code 1" in summary
    assert "AssertionError" in summary


def test_build_failure_summary_multiple_failures_notes_count() -> None:
    failures = [
        Failure(
            stage="verify_test",
            command="pytest",
            exit_code=1,
            category="test",
            message=f"failure {i}",
        )
        for i in range(3)
    ]
    report = TestReport(task_id="t1", passed=False, failures=failures, severity="critical")
    summary = build_failure_summary(report)
    assert "failure 0" in summary
    assert "plus 2 more" in summary


def test_build_failure_summary_handles_empty_failures() -> None:
    """Defensive path: shouldn't happen via runner, but the helper survives."""
    empty = TestReport(task_id="t1", passed=False, failures=[], severity="critical")
    # The model itself should reject this via the verifier contract, but
    # if a caller bypasses Verifier (direct invocation in tests), the
    # helper still returns a sensible string instead of crashing.
    summary = build_failure_summary(empty)
    assert "critical" in summary
