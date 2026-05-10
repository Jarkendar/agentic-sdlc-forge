"""Verifier agent — runs configured commands, classifies failures via LLM.

Stage 6 boundary above the Executor. Given the ExecutionResult of a task
and a list of VerificationCommands, the Verifier:

    1. Runs each command in order; fast-passes when all are green.
    2. On the first failing command, asks the Verifier-LLM (with the
       persona's hard severity rules) to produce a TestReport.
    3. If the LLM returns severity="flaky" with second_run_outcome="not_run",
       re-runs that one command and asks the LLM again with the actual
       outcome — this is the "request a re-run before classifying"
       contract from .forge/personas/verifier.md.
    4. Returns the final TestReport. The runner.py fix-loop consumes it.

Design points:

- All-green → no LLM call. The persona is only summoned for failures.
  Saves tokens and removes a flake source on the happy path.
- The Verifier never decides whether to retry — that's the runner's job.
  This module only classifies.
- Subprocess timeout is treated as exit_code = -1 with a synthetic stderr
  line so the LLM has something to classify. We don't pre-categorize
  the timeout — the LLM does, since it might be a test hanging vs a
  build genuinely needing more time.
- shell=True is intentional: `./gradlew test --info` and friends carry
  their own arg quoting; trying to shlex these breaks more than it
  protects. Sandboxing is out of scope (same posture as Aider).
"""

from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from forge.event_log import EventLog
from forge.llm.base import LLMClient
from forge.personas import Persona
from forge.schemas import (
    ExecutionResult,
    Failure,
    Task,
    TestReport,
    VerificationCommand,
)

# Last N chars of stdout/stderr we keep on disk (events.jsonl) AND feed
# back to the LLM. Matches verifier.md "may be truncated to last ~2000
# chars per stream" so the persona's expectations align with reality.
_OUTPUT_TAIL_CHARS = 2000


class VerifierError(Exception):
    """Raised when the Verifier cannot run at all (misconfigured persona,
    bad output_schema). Subprocess failures are NOT errors — they're the
    point of running the Verifier."""


# ---------------------------------------------------------------------------
# Command runner abstraction — lets tests inject scripted outputs without
# spawning real subprocesses (subprocess + shell=True is hard to mock
# portably). Production uses `_RealCommandRunner`.
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CommandResult:
    """Outcome of one VerificationCommand invocation."""

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


class CommandRunner(Protocol):
    """Protocol for running a verification command. Tests inject fakes."""

    def run(self, command: VerificationCommand, cwd: Path) -> CommandResult:
        ...


class _RealCommandRunner:
    """Production CommandRunner — wraps subprocess.run."""

    def run(self, command: VerificationCommand, cwd: Path) -> CommandResult:
        t0 = time.monotonic()
        try:
            completed = subprocess.run(
                command.command,
                shell=True,  # noqa: S602 — see module docstring
                cwd=str(cwd),
                capture_output=True,
                text=True,
                timeout=command.timeout_seconds,
                check=False,
            )
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            return CommandResult(
                exit_code=completed.returncode,
                stdout=completed.stdout or "",
                stderr=completed.stderr or "",
                duration_ms=elapsed_ms,
                timed_out=False,
            )
        except subprocess.TimeoutExpired as e:
            elapsed_ms = int((time.monotonic() - t0) * 1000)
            # `e.stdout` / `e.stderr` may be bytes or None depending on
            # how far the process got. Normalize.
            stdout = _coerce_to_str(e.stdout)
            stderr = _coerce_to_str(e.stderr)
            stderr = stderr + (
                f"\n[forge] command timed out after {command.timeout_seconds}s\n"
            )
            # exit_code = -1 is a sentinel: the LLM rule says "runtime
            # crash → critical" and a killed-on-timeout process is the
            # closest analogue to a crash for verification commands.
            return CommandResult(
                exit_code=-1,
                stdout=stdout,
                stderr=stderr,
                duration_ms=elapsed_ms,
                timed_out=True,
            )


def _coerce_to_str(value: bytes | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_verifier(
    *,
    task: Task,
    execution_result: ExecutionResult,
    commands: list[VerificationCommand],
    repo_root: Path,
    run_id: str,
    persona: Persona,
    llm: LLMClient,
    event_log: EventLog,
    command_runner: CommandRunner | None = None,
) -> TestReport:
    """Run verification commands for one task and return the TestReport.

    Args:
        task: The just-executed Task. Only `id` is used directly; the
            full object is in scope for future hooks (e.g. per-task
            command overrides).
        execution_result: ExecutionResult from the Executor. Its
            `files_changed` becomes the `touched_files` input to the
            Verifier persona.
        commands: Ordered list of VerificationCommands to run.
        repo_root: Working directory for each subprocess.
        run_id: For event correlation.
        persona: Loaded Verifier persona. Must declare output_schema=TestReport.
        llm: Provider client bound to the verifier model.
        event_log: Open EventLog for this run.
        command_runner: Override for tests. Defaults to the subprocess
            implementation.

    Returns:
        A TestReport with severity in {"none","warning","flaky","critical"}.

    Raises:
        VerifierError: persona is misconfigured.
        LLMValidationError / LLMTransportError: bubble from the LLM layer.
    """
    if persona.output_schema is not TestReport:
        raise VerifierError(
            f"Verifier persona must declare output_schema=TestReport, "
            f"got {persona.output_schema!r}. Check {persona.source_path}."
        )

    runner: CommandRunner = command_runner or _RealCommandRunner()
    touched_files = [str(p) for p in execution_result.files_changed]

    event_log.log(
        agent="verifier",
        phase="start",
        run_id=run_id,
        payload={
            "task_id": task.id,
            "command_count": len(commands),
            "touched_files": touched_files,
        },
    )

    # Empty config: Stage 6 lets it through with a warning. Stage 7's
    # `forge run` is where this becomes an error. The runner that calls us
    # must treat severity="none" as success, so this stays consistent.
    if not commands:
        event_log.log(
            agent="verifier",
            phase="empty_commands_warning",
            run_id=run_id,
            payload={"task_id": task.id},
        )
        return TestReport(task_id=task.id, passed=True, failures=[], severity="none")

    # ---- Iterate commands; first failure short-circuits ----
    for command in commands:
        first_run = runner.run(command, repo_root)
        _log_command_run(
            event_log,
            run_id=run_id,
            task_id=task.id,
            command=command,
            result=first_run,
            attempt="first",
        )
        if first_run.exit_code == 0:
            continue  # green; try next command

        # ---- Failure path: ask the LLM ----
        first_report = _classify_with_llm(
            task_id=task.id,
            command=command,
            result=first_run,
            touched_files=touched_files,
            second_run_outcome="not_run",
            persona=persona,
            llm=llm,
            event_log=event_log,
            run_id=run_id,
        )

        # Per persona contract: severity="flaky" with second_run_outcome="not_run"
        # is the LLM signaling "please re-run and call me again". Honor it
        # for test/runtime categories only (lint/compile are deterministic;
        # the LLM shouldn't ask for a re-run on those, but defend anyway).
        if first_report.severity == "flaky" and _is_flake_eligible(first_report):
            second_run = runner.run(command, repo_root)
            _log_command_run(
                event_log,
                run_id=run_id,
                task_id=task.id,
                command=command,
                result=second_run,
                attempt="second",
            )
            second_outcome = "passed" if second_run.exit_code == 0 else "failed"

            final_report = _classify_with_llm(
                task_id=task.id,
                command=command,
                # We pass the SECOND run's output so the LLM has fresh
                # context to classify. The first run's severity verdict
                # is discarded — the persona contract is "classify based
                # on what you see now"; carrying the old verdict forward
                # would bias the second classification.
                result=second_run if second_outcome == "failed" else first_run,
                touched_files=touched_files,
                second_run_outcome=second_outcome,
                persona=persona,
                llm=llm,
                event_log=event_log,
                run_id=run_id,
            )
            event_log.log(
                agent="verifier",
                phase="report",
                run_id=run_id,
                payload={
                    "task_id": task.id,
                    "severity": final_report.severity,
                    "passed": final_report.passed,
                    "failure_count": len(final_report.failures),
                    "rerun_used": True,
                },
            )
            return final_report

        # No re-run needed: classification is final.
        event_log.log(
            agent="verifier",
            phase="report",
            run_id=run_id,
            payload={
                "task_id": task.id,
                "severity": first_report.severity,
                "passed": first_report.passed,
                "failure_count": len(first_report.failures),
                "rerun_used": False,
            },
        )
        return first_report

    # ---- All commands green ----
    report = TestReport(task_id=task.id, passed=True, failures=[], severity="none")
    event_log.log(
        agent="verifier",
        phase="report",
        run_id=run_id,
        payload={
            "task_id": task.id,
            "severity": "none",
            "passed": True,
            "failure_count": 0,
            "rerun_used": False,
        },
    )
    return report


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _is_flake_eligible(report: TestReport) -> bool:
    """Per persona rule 2: lint/compile errors are never flaky."""
    if not report.failures:
        return False
    return all(f.category in ("test", "runtime") for f in report.failures)


def _classify_with_llm(
    *,
    task_id: str,
    command: VerificationCommand,
    result: CommandResult,
    touched_files: list[str],
    second_run_outcome: str,
    persona: Persona,
    llm: LLMClient,
    event_log: EventLog,
    run_id: str,
) -> TestReport:
    """Render the persona prompt and ask the LLM for a TestReport.

    Validates two contract points the persona promises but pydantic alone
    can't enforce: task_id echo and passed/severity coherence.
    """
    stdout_tail = result.stdout[-_OUTPUT_TAIL_CHARS:]
    stderr_tail = result.stderr[-_OUTPUT_TAIL_CHARS:]

    system_prompt = persona.render(
        task_id=task_id,
        command=command.command,
        exit_code=str(result.exit_code),
        stdout=stdout_tail,
        stderr=stderr_tail,
        touched_files="\n".join(touched_files) if touched_files else "(none)",
        second_run_outcome=second_run_outcome,
    )
    user_message = "Produce the TestReport as a single JSON object matching the schema."

    t0 = time.monotonic()
    response = llm.complete(system=system_prompt, user=user_message, schema=TestReport)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if not isinstance(response.content, TestReport):
        # Defensive — same contract guard as planner.py. If a provider
        # regression returns the wrong type, fail loudly here.
        raise VerifierError(
            f"LLM returned content of type {type(response.content).__name__}, "
            f"expected TestReport. Provider: {response.provider} model: {response.model}."
        )

    report: TestReport = response.content

    event_log.log(
        agent="verifier",
        phase="llm_call_complete",
        run_id=run_id,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        duration_ms=elapsed_ms,
        cost_usd=response.cost_usd,
        payload={
            "task_id": task_id,
            "command": command.name,
            "second_run_outcome": second_run_outcome,
            "model": response.model,
            "provider": response.provider,
            "retried_validation": response.retried_validation,
            "severity": report.severity,
        },
    )

    # Hard contract checks per verifier.md:
    # - task_id echoed verbatim
    # - passed iff severity=="none"
    # - failures non-empty when passed=False
    if report.task_id != task_id:
        raise VerifierError(
            f"TestReport.task_id mismatch: expected {task_id!r}, got {report.task_id!r}."
        )
    if report.passed != (report.severity == "none"):
        raise VerifierError(
            f"TestReport contract violation: passed={report.passed} but "
            f"severity={report.severity!r}. passed must be true iff severity='none'."
        )
    if not report.passed and not report.failures:
        raise VerifierError(
            "TestReport contract violation: passed=False with empty failures list."
        )

    # If the LLM returned no failure for a failing command, synthesize a
    # minimal one so the runner has something to feed into the fix-loop
    # prompt. This shouldn't happen — the contract above catches it —
    # but the synthesis path is here as belt-and-braces for the case
    # where severity is one of {warning,flaky,critical} but the LLM
    # produced a thin failures list.
    if report.failures and report.failures[0].command != command.command:
        # Some models drop or rename the command. Patch it through so the
        # runner can build a deterministic fix-loop summary.
        report = report.model_copy(
            update={
                "failures": [
                    f.model_copy(update={"command": command.command})
                    for f in report.failures
                ]
            }
        )

    return report


def _log_command_run(
    event_log: EventLog,
    *,
    run_id: str,
    task_id: str,
    command: VerificationCommand,
    result: CommandResult,
    attempt: str,
) -> None:
    """Log one subprocess invocation. Full output goes here (no truncation)
    so the eventual run report can show exactly what ran. The persona
    prompt sees only the tail."""
    event_log.log(
        agent="verifier",
        phase="command_complete",
        run_id=run_id,
        duration_ms=result.duration_ms,
        payload={
            "task_id": task_id,
            "command_name": command.name,
            "command": command.command,
            "stage": command.stage,
            "exit_code": result.exit_code,
            "timed_out": result.timed_out,
            "attempt": attempt,
            "stdout": result.stdout,
            "stderr": result.stderr,
        },
    )


# Re-export Failure for convenience — runner.py builds fix-loop summaries
# from TestReport.failures, so making the symbol obvious from this module
# keeps imports tidy.
__all__ = [
    "CommandResult",
    "CommandRunner",
    "Failure",
    "VerifierError",
    "run_verifier",
]
