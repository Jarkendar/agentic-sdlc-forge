"""Executor agent — runs one Task through Aider + git, returns ExecutionResult.

Per IMPLEMENTATION_PLAN Stage 5 and decision D1=A: this is a *deterministic*
agent. It does not call an LLM. The semantic work (planning, code generation)
happens upstream (Planner) and inside Aider; the Executor's job is plumbing.

Lifecycle for one task:

    1. ensure_clean_worktree         — fail early on dirty repo
    2. ensure_run_branch             — idempotent (D3.1)
    3. checkpoint run-branch HEAD    — for merge anchoring later
    4. create_task_branch            — branched from run tip
    5. checkpoint task-branch HEAD   — for squash anchoring later
    6. build aider prompt            — structured markdown (D2 option C)
    7. AiderRunner.run               — subprocess, captured streams
    8. classify outcome:
         exit != 0 / timeout       -> failed (no squash, no merge)
         no commits since (5)      -> no_changes
         out-of-scope edits        -> failed (no squash, no merge) — D3.8
         else                      -> success: squash + merge --no-ff
    9. checkout run branch          — D3.5: always end on run branch

Persona file (.forge/personas/executor.md) is the human-readable contract.
This module is the executable version; both must agree. If you change the
status semantics here, update executor.md too.
"""

from __future__ import annotations

from pathlib import Path

from forge.aider_runner import AiderInvocation, AiderResult, AiderRunner
from forge.event_log import EventLog
from forge.git_ops import (
    GitOpsError,
    OutOfScopeEdit,
    create_task_branch,
    current_head_sha,
    diff_files_since,
    ensure_clean_worktree,
    ensure_run_branch,
    has_new_commits_since,
    merge_task_into_run,
    run_branch_name,
    squash_task_commits,
)
from forge.schemas import ExecutionResult, Task

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class ExecutorError(Exception):
    """Raised when the Executor cannot start (dirty worktree, missing branch).

    Once Aider has actually run, the outcome is always an ExecutionResult —
    we never raise mid-execution. This split keeps the caller's error
    handling simple: pre-flight = exception, runtime = result.
    """


# ---------------------------------------------------------------------------
# Conventional commit type heuristic
# ---------------------------------------------------------------------------
#
# MVP heuristic per first lowercased word of task.goal. Tracked as a
# post-MVP item in IMPLEMENTATION_PLAN Stage 9 — eventually `Task` will
# carry an explicit `commit_type` field, set by the Planner. Until then
# this heuristic + chore fallback is good enough.

_COMMIT_TYPE_BY_VERB: dict[str, str] = {
    # feat — additions / new capability
    "add": "feat",
    "implement": "feat",
    "create": "feat",
    "introduce": "feat",
    "support": "feat",
    "build": "feat",
    # fix — bug repair
    "fix": "fix",
    "resolve": "fix",
    "repair": "fix",
    "correct": "fix",
    "patch": "fix",
    # refactor — restructure without behavior change
    "refactor": "refactor",
    "extract": "refactor",
    "rename": "refactor",
    "move": "refactor",
    "simplify": "refactor",
    "restructure": "refactor",
    # test — test-only change
    "test": "test",
    "cover": "test",
    # docs — documentation only
    "document": "docs",
    "clarify": "docs",
    # chore — deps, config, maintenance
    "bump": "chore",
    "upgrade": "chore",
    "update": "chore",
}


def detect_commit_type(goal: str) -> str:
    """Heuristic: map task.goal's first word to a conventional commit type.

    Falls back to ``chore`` for unknown verbs and empty goals — it's the
    least-wrong default; a misclassified chore at least won't masquerade as
    a feature in changelogs.
    """
    if not goal.strip():
        return "chore"
    first_word = goal.strip().split()[0].lower().rstrip(".,:;!?")
    return _COMMIT_TYPE_BY_VERB.get(first_word, "chore")


# ---------------------------------------------------------------------------
# Aider prompt assembly (D2 option C — structured markdown)
# ---------------------------------------------------------------------------


def _build_aider_message(task: Task, *, previous_failure: str = "") -> str:
    """Assemble the --message string Aider receives.

    Layout (markdown-shaped so Aider's own rendering keeps it readable):
        # Goal
        ...
        # Acceptance criteria
        - ...
        # Files in scope
        - ...
        # Previous failure   (only if non-empty)
        ...
    """
    parts: list[str] = []
    parts.append("# Goal")
    parts.append(task.goal)
    parts.append("")
    parts.append("# Acceptance criteria")
    if task.acceptance_criteria:
        for ac in task.acceptance_criteria:
            parts.append(f"- {ac}")
    else:
        parts.append("(none specified)")
    parts.append("")
    parts.append("# Files in scope")
    if task.files:
        for f in task.files:
            parts.append(f"- {f}")
    else:
        parts.append("(no files declared — Planner left this empty)")
    if previous_failure:
        parts.append("")
        parts.append("# Previous attempt failed")
        parts.append(previous_failure)
        parts.append("")
        parts.append("Address the failure. Do not redo work that succeeded.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Squashed commit message
# ---------------------------------------------------------------------------


def _build_squash_message(task: Task, run_id: str) -> str:
    """Conventional commit for the squashed task branch.

    Format follows .forge/git_flow.md: ``<type>: <subject>`` (≤70 chars), two
    blank lines, body explaining the task. Footer carries forge traceability
    so the future Documentalist can distinguish forge-driven commits from
    manual ones.
    """
    commit_type = detect_commit_type(task.goal)
    # Subject: lowercase first letter after the type prefix, strip trailing period
    raw_subject = task.goal.strip().rstrip(".")
    subject_body = raw_subject[0].lower() + raw_subject[1:] if raw_subject else "task"
    subject = f"{commit_type}: {subject_body}"
    # Hard cap at 70 chars total (git_flow.md rule)
    if len(subject) > 70:
        subject = subject[:67].rstrip() + "..."

    lines: list[str] = [subject, "", ""]
    lines.append(f"Implements task {task.id} from run {run_id}.")
    if task.acceptance_criteria:
        lines.append("")
        lines.append("Acceptance criteria:")
        for ac in task.acceptance_criteria:
            lines.append(f"- {ac}")
    lines.append("")
    lines.append(f"forge-task-id: {task.id}")
    lines.append(f"forge-run-id: {run_id}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def run_executor(
    *,
    task: Task,
    run_id: str,
    repo_root: Path,
    aider: AiderRunner,
    event_log: EventLog,
    previous_failure: str = "",
) -> ExecutionResult:
    """Run one Task end-to-end. Returns the validated ExecutionResult.

    Args:
        task: The Task to execute (typically from a Plan).
        run_id: Identifier of the current run; controls branch names and
            EventLog correlation.
        repo_root: Filesystem path of the git repo. Must be clean.
        aider: An AiderRunner (or test fake matching its protocol).
        event_log: Open EventLog for this run.
        previous_failure: If this is a fix-loop retry, a short summary of
            what went wrong last time. Passed verbatim to Aider via the
            prompt, per executor.md.

    Returns:
        ExecutionResult with status in {success, failed, no_changes}.
        ``skipped`` is reserved for the Orchestrator (Stage 7) which knows
        about depends_on.

    Raises:
        ExecutorError: pre-flight failure (dirty worktree, etc.). Once Aider
            has run we always return a result; pre-flight is the only path
            where exceptions surface.
    """
    # ---- Pre-flight ----
    try:
        ensure_clean_worktree(repo_root)
    except GitOpsError as e:
        raise ExecutorError(str(e)) from e

    event_log.log(
        agent="executor",
        phase="start",
        run_id=run_id,
        payload={
            "task_id": task.id,
            "goal": task.goal,
            "files": [str(f) for f in task.files],
            "is_fix_loop": bool(previous_failure),
        },
    )

    # ---- Branch setup ----
    ensure_run_branch(repo_root, run_id)
    run_base_sha = current_head_sha(repo_root)  # noqa: F841 — reserved for future merge audit
    create_task_branch(repo_root, run_id, task.id)
    task_base_sha = current_head_sha(repo_root)

    # ---- Build prompt ----
    message = _build_aider_message(task, previous_failure=previous_failure)
    invocation = AiderInvocation(
        message=message,
        files=list(task.files),
        cwd=repo_root,
        # 600s default per IMPLEMENTATION_PLAN §0.6.1 / executor.md.
        # Configurable via .forge/config.toml (limits.task_timeout_seconds)
        # is wired in Stage 7 — for Stage 5 we hardcode the same default.
        timeout_seconds=600,
    )

    # ---- Run Aider ----
    aider_result: AiderResult = aider.run(invocation)

    event_log.log(
        agent="executor",
        phase="aider_complete",
        run_id=run_id,
        duration_ms=aider_result.duration_ms,
        payload={
            "task_id": task.id,
            "exit_code": aider_result.exit_code,
            "timed_out": aider_result.timed_out,
            "stdout": aider_result.stdout,
            "stderr": aider_result.stderr,
        },
    )

    # ---- Classify outcome ----
    result = _classify_and_finalize(
        task=task,
        run_id=run_id,
        repo_root=repo_root,
        task_base_sha=task_base_sha,
        aider_result=aider_result,
    )

    event_log.log(
        agent="executor",
        phase="validated",
        run_id=run_id,
        duration_ms=aider_result.duration_ms,
        payload={
            "task_id": task.id,
            "status": result.status,
            "files_changed": [str(f) for f in result.files_changed],
        },
    )
    return result


# ---------------------------------------------------------------------------
# Outcome classification
# ---------------------------------------------------------------------------


def _classify_and_finalize(
    *,
    task: Task,
    run_id: str,
    repo_root: Path,
    task_base_sha: str,
    aider_result: AiderResult,
) -> ExecutionResult:
    """Decide status and either squash+merge (success) or just leave the
    task branch around (failed/no_changes), then return to the run branch.
    """
    files_changed = diff_files_since(repo_root, task_base_sha)

    # Hard failure modes — no further inspection needed
    if aider_result.timed_out or aider_result.exit_code != 0:
        _checkout_run_branch(repo_root, run_id)
        return ExecutionResult(
            task_id=task.id,
            status="failed",
            aider_stdout=aider_result.stdout,
            aider_stderr=aider_result.stderr,
            files_changed=files_changed,
            duration_ms=aider_result.duration_ms,
        )

    # Aider exited 0 but produced no commits — soft failure
    if not has_new_commits_since(repo_root, task_base_sha):
        _checkout_run_branch(repo_root, run_id)
        return ExecutionResult(
            task_id=task.id,
            status="no_changes",
            aider_stdout=aider_result.stdout,
            aider_stderr=aider_result.stderr,
            files_changed=[],
            duration_ms=aider_result.duration_ms,
        )

    # Aider made commits — check scope
    scope = OutOfScopeEdit.detect(changed=files_changed, allowed=task.files)
    if scope.has_violations:
        offending_str = ", ".join(str(p) for p in scope.offending)
        annotated_stderr = _annotate_stderr(
            aider_result.stderr,
            f"forge: out-of-scope edit ({offending_str})",
        )
        _checkout_run_branch(repo_root, run_id)
        return ExecutionResult(
            task_id=task.id,
            status="failed",
            aider_stdout=aider_result.stdout,
            aider_stderr=annotated_stderr,
            # Report ALL files changed, including the violating ones, so the
            # human reviewer can see exactly what aider did off-script.
            files_changed=files_changed,
            duration_ms=aider_result.duration_ms,
        )

    # Success path: squash and merge --no-ff
    squash_message = _build_squash_message(task, run_id)
    squash_task_commits(repo_root, base_sha=task_base_sha, message=squash_message)
    merge_task_into_run(repo_root, run_id, task.id)
    # merge_task_into_run leaves us on run branch — exactly where we want
    # to end up per D3.5.

    return ExecutionResult(
        task_id=task.id,
        status="success",
        aider_stdout=aider_result.stdout,
        aider_stderr=aider_result.stderr,
        files_changed=files_changed,
        duration_ms=aider_result.duration_ms,
    )


def _checkout_run_branch(repo_root: Path, run_id: str) -> None:
    """Return HEAD to the run branch. Used on every non-success path so the
    caller's invariant (D3.5) holds regardless of how we got here.
    """
    import subprocess

    subprocess.run(
        ["git", "checkout", run_branch_name(run_id)],
        cwd=repo_root,
        check=True,
        capture_output=True,
    )


def _annotate_stderr(existing: str, line: str) -> str:
    """Append a forge-internal message to Aider's stderr without losing context."""
    if not existing:
        return line
    return f"{existing}\n{line}"
