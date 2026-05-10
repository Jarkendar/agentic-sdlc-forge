"""Per-task fix-loop runner — Stage 6.

Drives one Task through Executor → Verifier, retrying on critical failure
up to `config.limits.max_retries_per_task`. The Orchestrator (Stage 7)
sits above this and runs the loop across multiple tasks; this module is
intentionally single-task so it composes cleanly.

Decision matrix per attempt:

    Executor returned…       | Action
    ─────────────────────────┼────────────────────────────────────────────
    failed                   | RunReport(status=failed) — no verify run
    no_changes               | RunReport(status=failed) — no verify run
    success → Verifier says… |
        severity=none        | RunReport(status=success)
        severity=warning     | RunReport(status=success)  [logged, non-blocking]
        severity=flaky       | RunReport(status=success)  [verifier already re-ran]
        severity=critical    | retry with previous_failure if attempts left,
                             | else RunReport(status=escalated)

`previous_failure` for the next attempt is built deterministically from
the last TestReport. See decision D9 in IMPLEMENTATION_PLAN — the LLM-
summarised version is post-MVP work.
"""

from __future__ import annotations

from pathlib import Path

from forge.agents.executor import run_executor
from forge.agents.verifier import run_verifier
from forge.aider_runner import AiderRunner
from forge.event_log import EventLog
from forge.git_ops import drop_task_branch
from forge.llm.base import LLMClient
from forge.personas import Persona
from forge.schemas import (
    ExecutionResult,
    Failure,
    RunReport,
    Task,
    TestReport,
    VerificationCommand,
)

# Tail sizes used when building the fix-loop prompt fragment fed back to
# Aider via Executor.previous_failure. Big enough to carry the actual
# error (a pytest traceback runs ~600-1000 chars), small enough that we
# don't blow Aider's context window with three runs of accumulated noise.
_FIX_LOOP_STDERR_TAIL = 800
_FIX_LOOP_STDOUT_TAIL = 400


def run_task_with_fix_loop(
    *,
    task: Task,
    run_id: str,
    repo_root: Path,
    aider: AiderRunner,
    event_log: EventLog,
    verifier_llm: LLMClient,
    verifier_persona: Persona,
    verification_commands: list[VerificationCommand],
    max_retries_per_task: int,
) -> RunReport:
    """Run one task with a bounded fix-loop. Returns a RunReport.

    Args:
        task: The Task to run.
        run_id: Run identifier (used for branch names + event correlation).
        repo_root: Working directory for Executor + Verifier subprocesses.
        aider: AiderRunner the Executor will dispatch to.
        event_log: Open EventLog for this run.
        verifier_llm: LLMClient bound to the verifier model.
        verifier_persona: Loaded Verifier persona (output_schema=TestReport).
        verification_commands: Commands the Verifier runs after each
            successful Executor pass. Empty list is allowed (test-only
            paths) and short-circuits to severity="none".
        max_retries_per_task: Hard cap. Total attempts = this value
            (1 means "one shot, no retries"). The Orchestrator (Stage 7)
            also tracks max_retries_per_run; we don't.

    Returns:
        RunReport summarising attempts and outcome.

    Raises:
        ExecutorError: pre-flight Executor failures (dirty worktree etc.)
            still propagate — the runner doesn't try to recover from
            those, since they indicate broken assumptions about repo
            state, not task-level bugs.
        LLMValidationError / LLMTransportError: from the Verifier-LLM
            layer; same reasoning.
    """
    if max_retries_per_task < 1:
        raise ValueError(
            f"max_retries_per_task must be ≥1, got {max_retries_per_task}. "
            f"A 0-attempt fix loop is meaningless — never run the task at all."
        )

    event_log.log(
        agent="runner",
        phase="task_start",
        run_id=run_id,
        payload={
            "task_id": task.id,
            "max_attempts": max_retries_per_task,
            "command_count": len(verification_commands),
        },
    )

    previous_failure = ""
    last_execution: ExecutionResult | None = None
    last_report: TestReport | None = None
    attempts = 0

    while attempts < max_retries_per_task:
        attempts += 1

        # On retries, the previous attempt's task branch still exists
        # (squashed+merged on success, or untouched on failed/no_changes
        # — but we never reach this point on those, since they early-return).
        # `create_task_branch` refuses to clobber, so we drop it first.
        # The fresh branch starts from the run-branch tip, which already
        # contains the previous iteration's merged code — Aider will see
        # the buggy state and (hopefully) fix it.
        if attempts > 1:
            drop_task_branch(repo_root, run_id, task.id)

        event_log.log(
            agent="runner",
            phase="attempt_start",
            run_id=run_id,
            payload={
                "task_id": task.id,
                "attempt": attempts,
                "max_attempts": max_retries_per_task,
                "is_fix_loop": bool(previous_failure),
            },
        )

        # ---- Executor ----
        execution = run_executor(
            task=task,
            run_id=run_id,
            repo_root=repo_root,
            aider=aider,
            event_log=event_log,
            previous_failure=previous_failure,
        )
        last_execution = execution

        if execution.status in ("failed", "no_changes"):
            # Mechanical failure: Aider crashed, made no edits, or wrote
            # out-of-scope files. Nothing for the Verifier to verify, and
            # no point feeding "tests failed" to Aider when it never even
            # got past git. Escalate immediately.
            reason = (
                "executor returned failed — Aider/git reported a problem"
                if execution.status == "failed"
                else "executor returned no_changes — Aider made no edits"
            )
            event_log.log(
                agent="runner",
                phase="task_failed",
                run_id=run_id,
                payload={
                    "task_id": task.id,
                    "attempts": attempts,
                    "executor_status": execution.status,
                    "reason": reason,
                },
            )
            return RunReport(
                task_id=task.id,
                status="failed",
                attempts=attempts,
                final_execution=execution,
                final_test_report=None,
                escalation_reason=reason,
            )

        # ---- Verifier ----
        report = run_verifier(
            task=task,
            execution_result=execution,
            commands=verification_commands,
            repo_root=repo_root,
            run_id=run_id,
            persona=verifier_persona,
            llm=verifier_llm,
            event_log=event_log,
        )
        last_report = report

        if report.severity in ("none", "warning", "flaky"):
            # All non-critical outcomes count as success at the runner
            # level. Warnings and flakes are logged by the Verifier; the
            # runner just records the win.
            event_log.log(
                agent="runner",
                phase="task_success",
                run_id=run_id,
                payload={
                    "task_id": task.id,
                    "attempts": attempts,
                    "final_severity": report.severity,
                },
            )
            return RunReport(
                task_id=task.id,
                status="success",
                attempts=attempts,
                final_execution=execution,
                final_test_report=report,
                escalation_reason=None,
            )

        # severity == "critical".
        # Only emit fix_loop_iteration when there's actually another
        # attempt coming — on the last attempt before escalation, the
        # next event in the log is `human_needed`, not `fix_loop_iteration`.
        if attempts < max_retries_per_task:
            previous_failure = build_failure_summary(report)
            event_log.log(
                agent="runner",
                phase="fix_loop_iteration",
                run_id=run_id,
                payload={
                    "task_id": task.id,
                    "attempt_just_finished": attempts,
                    "remaining_attempts": max_retries_per_task - attempts,
                    "failure_summary_chars": len(previous_failure),
                },
            )

    # ---- Cap exhausted ----
    # last_execution and last_report are guaranteed non-None here:
    # we entered the loop at least once (max_retries_per_task ≥1) and
    # only reach this point after a verifier verdict (the failed/no_changes
    # paths return early).
    assert last_execution is not None
    assert last_report is not None

    reason = (
        f"max_retries_per_task={max_retries_per_task} exhausted; "
        f"verifier's final severity was 'critical'"
    )
    event_log.log(
        agent="runner",
        phase="human_needed",
        run_id=run_id,
        payload={
            "task_id": task.id,
            "attempts": attempts,
            "reason": reason,
            "final_failures": [f.model_dump(mode="json") for f in last_report.failures],
        },
    )
    return RunReport(
        task_id=task.id,
        status="escalated",
        attempts=attempts,
        final_execution=last_execution,
        final_test_report=last_report,
        escalation_reason=reason,
    )


# ---------------------------------------------------------------------------
# Fix-loop summary builder
# ---------------------------------------------------------------------------


def build_failure_summary(report: TestReport) -> str:
    """Render a deterministic fix-loop prompt fragment from a TestReport.

    Output is fed verbatim into Executor's `previous_failure` parameter
    and then into Aider's prompt. Format: a short headline, then the
    failing command, then trimmed stderr/stdout.

    TODO(post-MVP, IMPLEMENTATION_PLAN Stage 9): replace with an LLM-
    generated summary that's task-aware, prioritizes the root cause
    across multiple failures, and elides framework noise. Deterministic
    truncation works for MVP but loses signal on cascade failures
    (e.g. one fixture error that cascades into 50 test failures —
    currently we feed back the first one alphabetically rather than
    the actual root cause).
    """
    if not report.failures:
        # Defensive: shouldn't happen since the runner only calls us
        # on severity="critical", which requires non-empty failures
        # by the Verifier's contract. Return a minimal stub so callers
        # don't have to nil-check.
        return f"Verification reported severity={report.severity!r} with no failures."

    top: Failure = report.failures[0]
    parts: list[str] = []
    if top.message:
        parts.append(top.message)
    parts.append(f"Command: {top.command} (exit code {top.exit_code})")

    stderr_tail = top.stderr_excerpt[-_FIX_LOOP_STDERR_TAIL:] if top.stderr_excerpt else ""
    stdout_tail = top.stdout_excerpt[-_FIX_LOOP_STDOUT_TAIL:] if top.stdout_excerpt else ""
    if stderr_tail:
        parts.append("--- stderr (tail) ---")
        parts.append(stderr_tail.rstrip())
    if stdout_tail:
        parts.append("--- stdout (tail) ---")
        parts.append(stdout_tail.rstrip())

    if len(report.failures) > 1:
        parts.append(
            f"(plus {len(report.failures) - 1} more failure(s) — "
            f"focus on the one above first.)"
        )

    return "\n".join(parts)


__all__ = ["build_failure_summary", "run_task_with_fix_loop"]
