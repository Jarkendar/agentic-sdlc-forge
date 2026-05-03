"""Pydantic schemas — the contracts between agents and the persistence format.

Every inter-agent message and every persisted run artifact is one of these models.
Free-text strings between agents are forbidden; use these schemas instead.

When changing schemas in a backwards-incompatible way, bump SCHEMA_VERSION and
write a migrator for older state.json files.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

# Bump on any backwards-incompatible change to RunState, Plan, or Task.
# Runs persisted with an older version cannot be resumed without a migrator.
SCHEMA_VERSION = "1"


def _utcnow() -> datetime:
    """UTC now with timezone — never use naive datetimes in persisted data."""
    return datetime.now(UTC)


# ---------------------------------------------------------------------------
# Plan & Task — produced by the Planner, consumed by the Executor
# ---------------------------------------------------------------------------


class Task(BaseModel):
    """One atomic unit of work for the Executor.

    Atomicity rules (enforced by the Planner prompt, not this schema):
    - one logical change per task
    - touches a small, bounded set of files (≤3 by guidance)
    - has acceptance criteria checkable without running other tasks
    """

    model_config = ConfigDict(extra="forbid")

    id: str = Field(description="Stable task ID, unique within a Plan (e.g. 'task-001').")
    goal: str = Field(description="What this task should achieve, in one sentence.")
    files: list[Path] = Field(
        default_factory=list,
        description="Files the Executor is allowed to edit. Empty = creating new files.",
    )
    acceptance_criteria: list[str] = Field(
        default_factory=list,
        description="Concrete, checkable conditions that mean the task is done.",
    )
    depends_on: list[str] = Field(
        default_factory=list,
        description="IDs of tasks that must complete before this one can start.",
    )


class Plan(BaseModel):
    """The full task list for a single run, produced by the Planner."""

    model_config = ConfigDict(extra="forbid")

    run_id: str
    user_story: str = Field(description="The original human-provided user story.")
    tasks: list[Task]
    created_at: datetime = Field(default_factory=_utcnow)
    schema_version: str = SCHEMA_VERSION


# ---------------------------------------------------------------------------
# ExecutionResult — produced by the Executor after invoking Aider
# ---------------------------------------------------------------------------


class ExecutionResult(BaseModel):
    """Outcome of running one Task through the Executor + Aider."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    status: Literal["success", "failed", "skipped", "no_changes"]
    aider_stdout: str = ""
    aider_stderr: str = ""
    files_changed: list[Path] = Field(
        default_factory=list,
        description="Files actually modified, derived from `git diff` after the Aider run.",
    )
    duration_ms: int = 0


# ---------------------------------------------------------------------------
# Failure & TestReport — produced by the Verifier
# ---------------------------------------------------------------------------


class Failure(BaseModel):
    """One concrete thing that went wrong, with enough context to fix it.

    Captures: where it happened (stage + task), what was running (command),
    and what came out (excerpts). Full output goes to events.jsonl;
    excerpts here are for the Executor's fix-loop prompt.
    """

    model_config = ConfigDict(extra="forbid")

    # Location
    task_id: str | None = Field(
        default=None,
        description="Task that triggered this failure. None for repo-wide failures.",
    )
    stage: Literal[
        "execute",
        "verify_test",
        "verify_lint",
        "verify_build",
        "verify_compile",
    ]
    command: str = Field(description="Exact command that produced the failure.")

    # Output
    exit_code: int
    stdout_excerpt: str = Field(
        default="",
        description="Last ~2000 chars of stdout. Full output is in events.jsonl.",
    )
    stderr_excerpt: str = Field(default="")

    # Diagnostic
    category: Literal["test", "lint", "build", "compile", "runtime", "unknown"]
    file_hint: Path | None = Field(
        default=None,
        description="File parsed from the failure output, if identifiable.",
    )
    line_hint: int | None = None
    message: str | None = Field(
        default=None,
        description="Human-readable one-line summary, if the Verifier can produce one.",
    )


class TestReport(BaseModel):
    """Verifier's overall verdict on a run of test/lint/build commands."""

    model_config = ConfigDict(extra="forbid")

    task_id: str
    passed: bool
    failures: list[Failure] = Field(default_factory=list)
    severity: Literal["critical", "warning", "flaky", "none"] = Field(
        default="none",
        description=(
            "Aggregate severity. 'critical' triggers fix-loop; 'warning' is logged "
            "but does not block; 'flaky' is logged and retried once; 'none' = passed."
        ),
    )


# ---------------------------------------------------------------------------
# RunState — persisted snapshot at .forge/runs/<run_id>/state.json
# ---------------------------------------------------------------------------


class RunStatus(StrEnum):
    """States the Orchestrator FSM can be in.

    StrEnum (Python 3.11+) makes members serialize to their string value
    automatically — no .value plumbing needed.
    """

    PLANNING = "planning"
    EXECUTING = "executing"
    VERIFYING = "verifying"
    FIX_LOOP = "fix_loop"
    REPORTING = "reporting"
    DONE = "done"
    FAILED = "failed"
    ESCALATED = "escalated"  # hit hard retry caps; needs human


class RunState(BaseModel):
    """Full snapshot of a run. Single source of truth for resumability.

    Persisted as JSON at .forge/runs/<run_id>/state.json via state.py.
    Saved atomically (write-temp-then-rename) so a crash mid-write never
    leaves a half-written state.json.
    """

    model_config = ConfigDict(extra="forbid")

    # Identity
    run_id: str
    schema_version: str = SCHEMA_VERSION

    # Inputs
    user_story: str
    plan: Plan | None = Field(
        default=None,
        description="Set after the Planner runs. Stored here so resume needs no replan.",
    )

    # Progress
    status: RunStatus = RunStatus.PLANNING
    current_task_id: str | None = None
    completed_task_ids: list[str] = Field(default_factory=list)
    failed_task_ids: list[str] = Field(default_factory=list)
    skipped_task_ids: list[str] = Field(default_factory=list)

    # Retry budgets — see IMPLEMENTATION_PLAN §0.4.4
    retry_counts: dict[str, int] = Field(
        default_factory=dict,
        description="Per-task retry count. Cap: 3 per task.",
    )
    total_retries: int = Field(
        default=0,
        description="Run-wide retry count. Cap: 10 per run.",
    )

    # Resume hint
    last_event_offset: int = Field(
        default=0,
        description="Byte offset of the last event consumed from events.jsonl, for resume.",
    )

    # Timestamps
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
