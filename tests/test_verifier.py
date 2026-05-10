"""Tests for forge.agents.verifier.

Coverage:
- All-green path: no LLM call.
- Single command failure → LLM classifies → severity returned.
- Flaky path: first run fails, LLM returns flaky/not_run → re-run → second
  classification with second_run_outcome.
- Critical / warning / lint-not-flaky behaviors.
- Subprocess timeout treated as a runtime failure feeding the LLM.
- Hard contract checks (passed↔severity, task_id echo, command patching).
- Empty commands list short-circuits to severity='none'.
- Persona with wrong output_schema raises VerifierError.

Real subprocesses are never spawned — `_FakeCommandRunner` injects scripted
results.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from pydantic import BaseModel

from forge.agents.verifier import (
    CommandResult,
    VerifierError,
    run_verifier,
)
from forge.event_log import EventLog
from forge.llm.base import LLMClient, LLMResponse
from forge.personas import Persona
from forge.schemas import (
    ExecutionResult,
    Failure,
    Task,
    TestReport,
    VerificationCommand,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------
#
# Helpers are duplicated across test files (rather than shared via conftest
# or a tests/__init__.py module) per the project rule that cross-module test
# imports cause pytest collection failures. Keep this block in sync with
# similar blocks in test_planner / test_executor when fixing bugs in either.


class _FakeCommandRunner:
    """Returns scripted CommandResults in declaration order."""

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


class _ScriptedLLM(LLMClient):
    """Returns a pre-baked TestReport on each call. Test inspects `calls`."""

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
        report = self._reports.pop(0)
        return LLMResponse(
            content=report,
            tokens_in=42,
            tokens_out=84,
            cost_usd=0.001,
            duration_ms=120,
            model="fake-model",
            provider="fake",
            finish_reason="end_turn",
            retried_validation=False,
        )


def _persona(
    output_schema: type[BaseModel] | None = TestReport,
    body: str | None = None,
) -> Persona:
    """Build a synthetic Verifier persona without touching disk."""
    if body is None:
        body = (
            "task={{task_id}} cmd={{command}} exit={{exit_code}} "
            "stdout={{stdout}} stderr={{stderr}} "
            "files={{touched_files}} second={{second_run_outcome}}"
        )
    return Persona(
        name="verifier",
        output_schema=output_schema,
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
        body=body,
        source_path=Path("verifier.md"),
    )


def _task(task_id: str = "t1") -> Task:
    return Task(id=task_id, goal="g", files=[Path("src/foo.py")])


def _execution(files: list[str] | None = None) -> ExecutionResult:
    paths = [Path(f) for f in (files if files is not None else ["src/foo.py"])]
    return ExecutionResult(task_id="t1", status="success", files_changed=paths)


def _cmd(
    name: str,
    command: str = "true",
    stage: str = "verify_test",
    timeout: int = 60,
) -> VerificationCommand:
    return VerificationCommand(
        name=name,
        command=command,
        stage=stage,  # type: ignore[arg-type]
        timeout_seconds=timeout,
    )


def _result(
    exit_code: int = 0,
    stdout: str = "",
    stderr: str = "",
    timed_out: bool = False,
) -> CommandResult:
    return CommandResult(
        exit_code=exit_code,
        stdout=stdout,
        stderr=stderr,
        duration_ms=10,
        timed_out=timed_out,
    )


def _report(
    severity: str,
    *,
    task_id: str = "t1",
    command: str = "pytest",
    category: str = "test",
    message: str | None = "boom",
) -> TestReport:
    if severity == "none":
        return TestReport(task_id=task_id, passed=True, failures=[], severity="none")
    return TestReport(
        task_id=task_id,
        passed=False,
        failures=[
            Failure(
                stage="verify_test",
                command=command,
                exit_code=1,
                stdout_excerpt="...",
                stderr_excerpt="...",
                category=category,  # type: ignore[arg-type]
                message=message,
            )
        ],
        severity=severity,  # type: ignore[arg-type]
    )


def _open_log(tmp_path: Path) -> Callable[[], EventLog]:
    def _opener() -> EventLog:
        return EventLog(tmp_path / "events.jsonl")
    return _opener


# ---------------------------------------------------------------------------
# All-green path
# ---------------------------------------------------------------------------


def test_all_green_skips_llm_entirely(tmp_path: Path) -> None:
    """When every command exits 0, the LLM is never called and severity='none'."""
    runner = _FakeCommandRunner([_result(0), _result(0)])
    llm = _ScriptedLLM([])  # zero reports — calling LLM would explode

    with EventLog(tmp_path / "events.jsonl") as log:
        report = run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[_cmd("ruff", stage="verify_lint"), _cmd("pytest")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )

    assert report.passed is True
    assert report.severity == "none"
    assert report.failures == []
    assert llm.calls == []
    # Both commands ran in order.
    assert [c[0].name for c in runner.calls] == ["ruff", "pytest"]


def test_empty_commands_returns_none_severity(tmp_path: Path) -> None:
    """Empty config: short-circuit, no subprocess, no LLM, warning event."""
    runner = _FakeCommandRunner([])
    llm = _ScriptedLLM([])

    with EventLog(tmp_path / "events.jsonl") as log:
        report = run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )

    assert report.severity == "none"
    assert llm.calls == []
    phases = [e.phase for e in EventLog.read(tmp_path / "events.jsonl")]
    assert "empty_commands_warning" in phases


# ---------------------------------------------------------------------------
# Failure path with classification
# ---------------------------------------------------------------------------


def test_first_failing_command_short_circuits_rest(tmp_path: Path) -> None:
    """If lint fails, pytest must NOT run."""
    runner = _FakeCommandRunner([_result(1, stderr="lint error")])
    llm = _ScriptedLLM([_report("critical", category="lint", command="ruff check src")])

    with EventLog(tmp_path / "events.jsonl") as log:
        report = run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[
                _cmd("ruff", command="ruff check src", stage="verify_lint"),
                _cmd("pytest", command="pytest -q"),
            ],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )

    assert report.severity == "critical"
    # Only the lint command ran; pytest was skipped.
    assert [c[0].name for c in runner.calls] == ["ruff"]
    assert len(llm.calls) == 1


def test_critical_failure_returned_directly(tmp_path: Path) -> None:
    runner = _FakeCommandRunner([_result(1, stderr="AssertionError: 1==2")])
    llm = _ScriptedLLM([_report("critical")])

    with EventLog(tmp_path / "events.jsonl") as log:
        report = run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[_cmd("pytest", command="pytest")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )

    assert report.severity == "critical"
    assert report.passed is False
    # No re-run on critical.
    assert len(runner.calls) == 1


def test_warning_does_not_trigger_rerun(tmp_path: Path) -> None:
    runner = _FakeCommandRunner([_result(1)])
    llm = _ScriptedLLM([_report("warning", category="lint")])

    with EventLog(tmp_path / "events.jsonl") as log:
        report = run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[_cmd("pytest", command="pytest")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )

    assert report.severity == "warning"
    assert len(runner.calls) == 1


# ---------------------------------------------------------------------------
# Flaky path: re-run on second_run_outcome=not_run
# ---------------------------------------------------------------------------


def test_flaky_first_then_pass_resolves_to_flaky(tmp_path: Path) -> None:
    """LLM says flaky+not_run → runner re-runs → second pass → final flaky."""
    runner = _FakeCommandRunner([
        _result(1, stderr="connection reset"),
        _result(0),  # second run passes
    ])
    llm = _ScriptedLLM([
        _report("flaky", category="test"),  # first call
        # Second call: persona expects severity reflecting both runs;
        # the persona body in real life decides — for this fake we
        # just script the desired final classification.
        _report("flaky", category="test"),
    ])

    with EventLog(tmp_path / "events.jsonl") as log:
        report = run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[_cmd("pytest", command="pytest")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )

    assert report.severity == "flaky"
    assert len(runner.calls) == 2
    assert len(llm.calls) == 2
    # Inspect the second call's prompt: must carry second_run_outcome=passed
    second_system = llm.calls[1]["system"]
    assert isinstance(second_system, str)
    assert "second=passed" in second_system


def test_flaky_first_then_fail_can_become_critical(tmp_path: Path) -> None:
    """If second run also fails, the LLM is allowed to escalate to critical."""
    runner = _FakeCommandRunner([_result(1), _result(1)])
    llm = _ScriptedLLM([
        _report("flaky", category="test"),       # first triage
        _report("critical", category="test"),    # second classification, deterministic fail
    ])

    with EventLog(tmp_path / "events.jsonl") as log:
        report = run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[_cmd("pytest", command="pytest")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )

    assert report.severity == "critical"
    assert len(runner.calls) == 2


def test_flaky_label_with_lint_category_does_not_rerun(tmp_path: Path) -> None:
    """Persona rule: lint failures are deterministic, never flaky.

    Even if a buggy LLM returns severity=flaky for a lint failure, the
    runtime guard in `_is_flake_eligible` prevents the re-run, since
    lint is excluded by category. The first verdict is final.
    """
    runner = _FakeCommandRunner([_result(1)])  # only one call expected
    llm = _ScriptedLLM([_report("flaky", category="lint")])

    with EventLog(tmp_path / "events.jsonl") as log:
        report = run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[_cmd("ruff", command="ruff check", stage="verify_lint")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )

    assert report.severity == "flaky"
    assert len(runner.calls) == 1  # NO rerun
    assert len(llm.calls) == 1


# ---------------------------------------------------------------------------
# Timeout
# ---------------------------------------------------------------------------


def test_timeout_is_passed_to_llm_as_runtime_failure(tmp_path: Path) -> None:
    """Timed-out subprocess: exit_code=-1, timed_out=True → still goes through LLM."""
    runner = _FakeCommandRunner([
        _result(exit_code=-1, stderr="timed out after 60s", timed_out=True)
    ])
    llm = _ScriptedLLM([_report("critical", category="runtime")])

    with EventLog(tmp_path / "events.jsonl") as log:
        report = run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[_cmd("pytest", command="pytest")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )

    assert report.severity == "critical"
    # The LLM saw the synthetic exit code and the timeout text.
    first_system = llm.calls[0]["system"]
    assert isinstance(first_system, str)
    assert "exit=-1" in first_system


# ---------------------------------------------------------------------------
# Contract checks
# ---------------------------------------------------------------------------


def test_persona_with_wrong_schema_raises(tmp_path: Path) -> None:
    """Verifier persona must declare output_schema=TestReport."""
    runner = _FakeCommandRunner([_result(0)])
    llm = _ScriptedLLM([])

    with EventLog(tmp_path / "events.jsonl") as log, pytest.raises(VerifierError):
        run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[_cmd("pytest")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(output_schema=None),  # wrong
            llm=llm,
            event_log=log,
            command_runner=runner,
        )


def test_task_id_mismatch_raises(tmp_path: Path) -> None:
    runner = _FakeCommandRunner([_result(1)])
    bad = TestReport(
        task_id="WRONG_ID",
        passed=False,
        failures=[
            Failure(stage="verify_test", command="pytest", exit_code=1, category="test")
        ],
        severity="critical",
    )
    llm = _ScriptedLLM([bad])

    with EventLog(tmp_path / "events.jsonl") as log, pytest.raises(VerifierError, match="task_id"):
        run_verifier(
            task=_task("t1"),
            execution_result=_execution(),
            commands=[_cmd("pytest", command="pytest")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )


def test_passed_severity_inconsistency_raises(tmp_path: Path) -> None:
    """passed=True with severity!=none must be rejected."""
    runner = _FakeCommandRunner([_result(1)])
    bad = TestReport(
        task_id="t1",
        passed=True,
        failures=[
            Failure(stage="verify_test", command="pytest", exit_code=1, category="test")
        ],
        severity="critical",
    )
    llm = _ScriptedLLM([bad])

    with EventLog(tmp_path / "events.jsonl") as log, pytest.raises(VerifierError, match="passed"):
        run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[_cmd("pytest", command="pytest")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )


def test_command_string_is_patched_through_to_failure(tmp_path: Path) -> None:
    """LLMs sometimes paraphrase the command. Runtime patches it back."""
    runner = _FakeCommandRunner([_result(1)])
    bad = TestReport(
        task_id="t1",
        passed=False,
        failures=[
            Failure(
                stage="verify_test",
                command="pytest",  # missing the -q suffix
                exit_code=1,
                category="test",
                message="x",
            )
        ],
        severity="critical",
    )
    llm = _ScriptedLLM([bad])

    with EventLog(tmp_path / "events.jsonl") as log:
        report = run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[_cmd("pytest", command="pytest -q")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )

    assert report.failures[0].command == "pytest -q"


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------


def test_command_complete_event_carries_full_output(tmp_path: Path) -> None:
    """Stage 6: full stdout/stderr lives on disk; tail goes to the LLM only."""
    big_stdout = "x" * 5000
    runner = _FakeCommandRunner([_result(1, stdout=big_stdout)])
    llm = _ScriptedLLM([_report("critical")])

    with EventLog(tmp_path / "events.jsonl") as log:
        run_verifier(
            task=_task(),
            execution_result=_execution(),
            commands=[_cmd("pytest", command="pytest")],
            repo_root=tmp_path,
            run_id="run-1",
            persona=_persona(),
            llm=llm,
            event_log=log,
            command_runner=runner,
        )

    events = list(EventLog.read(tmp_path / "events.jsonl"))
    cmd_events = [e for e in events if e.phase == "command_complete"]
    assert cmd_events
    assert cmd_events[0].payload["stdout"] == big_stdout  # full, not truncated
