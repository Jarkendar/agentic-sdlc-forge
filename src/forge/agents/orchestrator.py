"""Orchestrator agent — top-level run driver for Stage 7.

The Orchestrator owns the run: it calls Planner, iterates the plan
through `runner.run_task_with_fix_loop`, handles skipped tasks (those
whose dependencies didn't complete), persists `RunState` after every
state transition, and finally hands off to Reporter.

Architecture decision (D1, locked in Stage 7 planning):
    The router is *deterministic*. The orchestrator persona prompt
    (`.forge/personas/orchestrator.md`) stays as documentation and as
    an optional **shadow** path — enabled by a config knob, OFF by
    default. When shadow is on, every routing turn calls the LLM with
    the same inputs the deterministic router got, validates that the
    LLM's choice is in `legal_actions`, and logs the comparison. The
    LLM never affects the actual route. This gives us prompt-tuning
    data for the future without putting a paid call on the critical
    path.

Resumability:
    `RunState` is saved at five points:
      1. immediately after the run starts (status=PLANNING)
      2. after the Planner returns (plan populated, status=EXECUTING)
      3. before each task starts (current_task_id set)
      4. after each task ends (completed/failed/skipped lists updated)
      5. before Reporter is invoked (status=REPORTING) and after
         (status=DONE | ESCALATED | FAILED)
    The `forge run --resume <run_id>` path loads the saved state and
    skips work that already happened — tasks with id in
    `completed_task_ids` / `failed_task_ids` / `skipped_task_ids` are
    not re-run.

Reporter contract:
    Reporter is invoked for *every* terminal status (DONE, ESCALATED,
    FAILED). The only case we skip Reporter is when the run never
    produced any events — that path is handled by the CLI, not here,
    because by the time we reach `run_orchestrator` we've already
    written at least the run_started event.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from forge.agents.planner import run_planner
from forge.agents.reporter import run_reporter
from forge.aider_runner import AiderRunner
from forge.config import ForgeConfig
from forge.event_log import EventLog
from forge.git_ops import ensure_clean_worktree, ensure_run_branch
from forge.llm.base import LLMClient
from forge.personas import Persona
from forge.router import LastEventKind, RouterInput, decide_next_action
from forge.runner import run_task_with_fix_loop
from forge.schemas import (
    RunReport,
    RunState,
    RunStatus,
    Task,
)
from forge.state import save_state


class OrchestratorError(Exception):
    """Raised on pre-flight problems (dirty worktree, bad config).

    Once the orchestrator has started executing tasks, *no* exceptions
    leave this module — outcomes are recorded in RunState and the run
    transitions to ESCALATED/FAILED instead. This keeps `forge run`'s
    error handling simple: pre-flight = exception, runtime = state.
    """


@dataclass(frozen=True)
class OrchestratorDeps:
    """Bundle of runtime dependencies the orchestrator needs.

    Bundled instead of expanded because `run_orchestrator`'s signature
    would otherwise be 12+ keyword args. Frozen so callers can't mutate
    it mid-run (would invite race conditions in any future async path).
    """

    config: ForgeConfig
    personas: dict[str, Persona]
    repo_root: Path
    forge_root: Path  # typically repo_root / ".forge"
    architecture_map: str
    planner_llm: LLMClient
    verifier_llm: LLMClient
    reporter_llm: LLMClient
    aider: AiderRunner


def run_orchestrator(
    *,
    state: RunState,
    deps: OrchestratorDeps,
    event_log: EventLog,
) -> RunState:
    """Drive a run end-to-end, returning the final RunState.

    The caller (cli.cmd_run) is responsible for:
        - building `deps` (config, personas, LLM clients, aider runner)
        - opening the EventLog (it stays open across multiple agents)
        - calling `save_state` on the returned state if it cares about
          the final snapshot (we also save inside, so this is belt-
          and-braces).

    Args:
        state: Starting RunState. For a fresh run this has status=PLANNING
            and no plan. For a resume it has whatever was last persisted.
        deps: Runtime dependencies. See `OrchestratorDeps`.
        event_log: Open EventLog. Orchestrator emits `orchestrator/start`,
            `orchestrator/decision`, `orchestrator/skipped`, and
            `orchestrator/terminal` events.

    Returns:
        Final `RunState` with status in {DONE, ESCALATED, FAILED}.
        State is also persisted to disk via `save_state` before return.

    Raises:
        OrchestratorError: Pre-flight failure (e.g. dirty worktree).
            Raised before any task runs. After the first task, all
            failures are recorded in RunState and the run terminates
            via the normal state-machine path.
    """
    # ---- Pre-flight ----
    # Done up front so we fail fast before any LLM calls. Dirty worktree
    # is the only invariant we check here — config/credentials are the
    # caller's responsibility (CLI already validates them).
    try:
        ensure_clean_worktree(deps.repo_root)
    except Exception as e:
        raise OrchestratorError(
            f"Pre-flight: worktree is not clean. Commit or stash before running. ({e})"
        ) from e

    event_log.log(
        agent="orchestrator",
        phase="start",
        run_id=state.run_id,
        payload={
            "user_story_chars": len(state.user_story),
            "resumed": state.plan is not None,
            "completed_tasks_at_start": list(state.completed_task_ids),
            "failed_tasks_at_start": list(state.failed_task_ids),
            "skipped_tasks_at_start": list(state.skipped_task_ids),
        },
    )

    # The run branch must exist before any task runs. Idempotent — safe
    # to call on resume even if it was created in a previous attempt.
    ensure_run_branch(deps.repo_root, state.run_id)

    # ---- PLAN ----
    # Skip if we're resuming a run that already has a plan.
    if state.plan is None:
        state = _do_plan(state=state, deps=deps, event_log=event_log)
        # `_do_plan` may set status=FAILED if Planner blew up; bail to
        # Reporter so the user gets a record.
        if state.status == RunStatus.FAILED:
            return _finalize(state=state, deps=deps, event_log=event_log)

    assert state.plan is not None, "post-PLAN invariant: plan must be set"

    # ---- EXECUTE loop ----
    state = _run_task_loop(state=state, deps=deps, event_log=event_log)

    # ---- REPORT ----
    return _finalize(state=state, deps=deps, event_log=event_log)


# ---------------------------------------------------------------------------
# Phase: PLAN
# ---------------------------------------------------------------------------


def _do_plan(
    *,
    state: RunState,
    deps: OrchestratorDeps,
    event_log: EventLog,
) -> RunState:
    """Run the Planner and update state.

    On success: state.plan is populated, status moves to EXECUTING.
    On failure: status moves to FAILED with a recorded reason.
    """
    state.status = RunStatus.PLANNING
    save_state(state, deps.forge_root)

    planner_persona = deps.personas.get("planner")
    if planner_persona is None:
        state.status = RunStatus.FAILED
        event_log.log(
            agent="orchestrator",
            phase="terminal",
            run_id=state.run_id,
            payload={
                "status": "failed",
                "reason": "planner persona missing",
            },
        )
        save_state(state, deps.forge_root)
        return state

    try:
        plan = run_planner(
            user_story=state.user_story,
            run_id=state.run_id,
            architecture_map=deps.architecture_map,
            repo_root=deps.repo_root,
            persona=planner_persona,
            llm=deps.planner_llm,
            event_log=event_log,
        )
    except Exception as e:
        # Planner can raise PlannerError, LLMTransportError, etc. We
        # treat anything from this layer as a hard run failure: no
        # plan, no tasks to execute. Reporter still runs over whatever
        # got logged.
        state.status = RunStatus.FAILED
        event_log.log(
            agent="orchestrator",
            phase="terminal",
            run_id=state.run_id,
            payload={
                "status": "failed",
                "reason": f"planner raised {type(e).__name__}: {e}",
            },
        )
        save_state(state, deps.forge_root)
        return state

    state.plan = plan
    state.status = RunStatus.EXECUTING
    save_state(state, deps.forge_root)

    _log_router_decision(
        event_log=event_log,
        state=state,
        router_input=RouterInput(
            current_state="PLAN",
            last_event_kind="plan_succeeded",
            retry_caps_exhausted=False,
            more_tasks=bool(plan.tasks),
        ),
    )
    return state


# ---------------------------------------------------------------------------
# Phase: EXECUTE loop
# ---------------------------------------------------------------------------


def _run_task_loop(
    *,
    state: RunState,
    deps: OrchestratorDeps,
    event_log: EventLog,
) -> RunState:
    """Iterate the plan in Planner-declared order, running each task.

    Order is `plan.tasks` as given — we trust Planner's ordering and
    use `depends_on` only to decide skipped-vs-execute (D4 in Stage 7
    plan).
    """
    assert state.plan is not None  # caller invariant

    # Pre-build the set of already-done IDs so resume paths skip them
    # without re-running. We mutate it as we go so the per-iteration
    # `_pick_next_task` decisions stay consistent.
    already_processed = (
        set(state.completed_task_ids)
        | set(state.failed_task_ids)
        | set(state.skipped_task_ids)
    )

    verifier_persona = deps.personas.get("verifier")
    if verifier_persona is None:
        # Hard failure — can't run any task without a verifier. Record
        # all unprocessed tasks as skipped with this reason so the
        # report explains what happened.
        state.status = RunStatus.FAILED
        for task in state.plan.tasks:
            if task.id not in already_processed:
                _record_skipped(
                    state=state,
                    event_log=event_log,
                    task=task,
                    reason="verifier persona missing from .forge/personas/",
                )
        save_state(state, deps.forge_root)
        return state

    for task in state.plan.tasks:
        if task.id in already_processed:
            # Resume path: this task already terminated in a previous
            # attempt. Skip silently — no event, no state change.
            continue

        # --- Skip check ---
        missing_dep = _find_missing_dependency(
            task=task, completed=set(state.completed_task_ids), state=state
        )
        if missing_dep is not None:
            _record_skipped(
                state=state,
                event_log=event_log,
                task=task,
                reason=(
                    f"depends_on task {missing_dep.task_id!r} "
                    f"not completed (status={missing_dep.status})"
                ),
            )
            already_processed.add(task.id)
            save_state(state, deps.forge_root)
            continue

        # --- Run-wide cap check (before invoking the runner) ---
        if state.total_retries >= deps.config.limits.max_retries_per_run:
            # Mark every remaining task as skipped with a clear reason.
            event_log.log(
                agent="orchestrator",
                phase="run_cap_exhausted",
                run_id=state.run_id,
                payload={
                    "total_retries": state.total_retries,
                    "max_retries_per_run": deps.config.limits.max_retries_per_run,
                },
            )
            _record_skipped(
                state=state,
                event_log=event_log,
                task=task,
                reason=(
                    f"run-wide retry cap reached "
                    f"({state.total_retries}/{deps.config.limits.max_retries_per_run})"
                ),
            )
            already_processed.add(task.id)
            state.status = RunStatus.ESCALATED
            save_state(state, deps.forge_root)
            continue

        # --- Execute the task ---
        state.current_task_id = task.id
        # Don't downgrade ESCALATED back to EXECUTING — once any task
        # has escalated, the run-level status sticks even if subsequent
        # tasks succeed. This preserves the "run is broken" signal for
        # Reporter and any human looking at the final state.json.
        if state.status != RunStatus.ESCALATED:
            state.status = RunStatus.EXECUTING
        save_state(state, deps.forge_root)

        try:
            report = run_task_with_fix_loop(
                task=task,
                run_id=state.run_id,
                repo_root=deps.repo_root,
                aider=deps.aider,
                event_log=event_log,
                verifier_llm=deps.verifier_llm,
                verifier_persona=verifier_persona,
                verification_commands=deps.config.verification.commands,
                max_retries_per_task=deps.config.limits.max_retries_per_task,
            )
        except Exception as e:
            # The fix-loop runner shouldn't normally raise (Stage 6's
            # contract: outcomes are RunReports). But if it does, treat
            # the task as failed and keep going — one task blowing up
            # shouldn't abort the whole run silently.
            event_log.log(
                agent="orchestrator",
                phase="runner_crash",
                run_id=state.run_id,
                payload={
                    "task_id": task.id,
                    "error": f"{type(e).__name__}: {e}",
                },
            )
            state.failed_task_ids.append(task.id)
            already_processed.add(task.id)
            state.current_task_id = None
            save_state(state, deps.forge_root)
            continue

        # --- Record outcome ---
        _record_task_outcome(
            state=state,
            event_log=event_log,
            task=task,
            report=report,
        )
        already_processed.add(task.id)
        state.current_task_id = None

        # Log the router's view of what happens next. Pure observability
        # — the deterministic loop here makes the actual decision, but
        # the router exercise keeps the persona's decision table honest
        # against real runs (every transition produces an event a future
        # shadow-LLM eval would match against).
        _log_router_decision_for_outcome(
            event_log=event_log,
            state=state,
            report=report,
            deps=deps,
        )

        save_state(state, deps.forge_root)

    # End of loop — set terminal status.
    #
    # ESCALATED is the strongest signal: if any task hit retry caps OR
    # the run-wide cap fired, the run as a whole is escalated regardless
    # of whether other tasks also failed. `_record_task_outcome` sets
    # status=ESCALATED on per-task escalation; the run-cap branch above
    # also sets it. Preserve in both cases.
    if state.status != RunStatus.ESCALATED:
        if any(t.id in state.failed_task_ids for t in state.plan.tasks):
            state.status = RunStatus.FAILED
        elif all(
            t.id in state.completed_task_ids
            or t.id in state.skipped_task_ids
            for t in state.plan.tasks
        ):
            # All accounted-for via completed or skipped. If anything
            # was skipped due to a dependency or cap, the user wants to
            # know — but "DONE" here doesn't mean "every task ran",
            # it means "we exhausted the plan without an escalation".
            # The report breaks down the per-task statuses for clarity.
            state.status = RunStatus.DONE
        else:
            # Shouldn't be reachable — every task ends in one of the
            # three lists or escalates. Defence in depth.
            state.status = RunStatus.FAILED

    save_state(state, deps.forge_root)
    return state


# ---------------------------------------------------------------------------
# Skipped detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _MissingDep:
    """One unsatisfied dependency for a task — the reason it skips."""

    task_id: str
    status: str  # "failed" | "skipped" | "unknown"


def _find_missing_dependency(
    *, task: Task, completed: set[str], state: RunState
) -> _MissingDep | None:
    """Return the first depends_on entry that isn't in completed.

    Returns None when every dependency is satisfied.

    The status string distinguishes "depends on a task that failed"
    from "depends on a task that itself was skipped" — useful for
    chained-skip diagnostics in the report. "unknown" means the
    depends_on points at an ID that isn't in the plan at all
    (Planner bug); we still skip rather than error out, because a
    planner glitch shouldn't take down the whole run.
    """
    if not task.depends_on:
        return None

    failed = set(state.failed_task_ids)
    skipped = set(state.skipped_task_ids)
    known = {t.id for t in state.plan.tasks} if state.plan else set()

    for dep_id in task.depends_on:
        if dep_id in completed:
            continue
        if dep_id in failed:
            return _MissingDep(task_id=dep_id, status="failed")
        if dep_id in skipped:
            return _MissingDep(task_id=dep_id, status="skipped")
        if dep_id not in known:
            return _MissingDep(task_id=dep_id, status="unknown")
        # Dependency is in the plan but hasn't been processed yet —
        # this means Planner ordered them wrong. Treat as missing
        # rather than execute out of order.
        return _MissingDep(task_id=dep_id, status="not_yet_run")

    return None


def _record_skipped(
    *,
    state: RunState,
    event_log: EventLog,
    task: Task,
    reason: str,
) -> None:
    """Append to skipped_task_ids and emit a synthetic executor:validated
    event so Reporter and downstream consumers see a uniform shape.

    The synthetic event mirrors the real Executor's `validated` event:
    `status="skipped"`, empty `files_changed`, and a `skip_reason`
    payload field that real events don't carry (Reporter knows to look
    for it). The `executor:skipped` phase tag is also emitted alongside
    so naive log readers can grep for either.
    """
    if task.id in state.skipped_task_ids:
        return  # idempotent — resume safety

    state.skipped_task_ids.append(task.id)

    event_log.log(
        agent="executor",
        phase="skipped",
        run_id=state.run_id,
        payload={
            "task_id": task.id,
            "status": "skipped",
            "files_changed": [],
            "skip_reason": reason,
        },
    )
    # Mirror as a validated event with status=skipped so Reporter's
    # task-table aggregation, which keys off `executor:validated`,
    # picks the skip up without a special case.
    event_log.log(
        agent="executor",
        phase="validated",
        run_id=state.run_id,
        payload={
            "task_id": task.id,
            "status": "skipped",
            "files_changed": [],
            "skip_reason": reason,
        },
    )


# ---------------------------------------------------------------------------
# Outcome bookkeeping
# ---------------------------------------------------------------------------


def _record_task_outcome(
    *,
    state: RunState,
    event_log: EventLog,
    task: Task,
    report: RunReport,
) -> None:
    """Translate one RunReport into RunState updates.

    Single source of truth for the mapping; keeps the main loop free
    of bookkeeping logic.
    """
    # Each report.attempts > 1 means we retried inside the fix loop.
    # Every retry past the first counts toward the run-wide budget.
    retries_used = max(0, report.attempts - 1)
    if retries_used > 0:
        state.retry_counts[task.id] = retries_used
        state.total_retries += retries_used

    if report.status == "success":
        state.completed_task_ids.append(task.id)
    elif report.status == "escalated":
        state.failed_task_ids.append(task.id)
        # Run-wide ESCALATED is a stronger signal than per-task failure
        # — once any task escalates the run is broken; the user wants
        # to see ESCALATED in the final report.
        state.status = RunStatus.ESCALATED
    else:  # "failed"
        state.failed_task_ids.append(task.id)

    event_log.log(
        agent="orchestrator",
        phase="task_outcome",
        run_id=state.run_id,
        payload={
            "task_id": task.id,
            "report_status": report.status,
            "attempts": report.attempts,
            "retries_used": retries_used,
            "total_retries_after": state.total_retries,
            "escalation_reason": report.escalation_reason,
        },
    )


# ---------------------------------------------------------------------------
# Router shadow logging
# ---------------------------------------------------------------------------


def _log_router_decision(
    *,
    event_log: EventLog,
    state: RunState,
    router_input: RouterInput,
) -> None:
    """Log what the deterministic router would pick for the given input.

    This is the per-turn observability hook. When (and if) we wire a
    shadow LLM, this is where we'd also call the LLM, compare, and
    log the delta. Today it just records the deterministic decision so
    future shadow integration has a reference point in every run.
    """
    decision = decide_next_action(router_input)
    event_log.log(
        agent="orchestrator",
        phase="decision",
        run_id=state.run_id,
        payload={
            "current_state": router_input.current_state,
            "last_event_kind": router_input.last_event_kind,
            "retry_caps_exhausted": router_input.retry_caps_exhausted,
            "more_tasks": router_input.more_tasks,
            "router_action": decision.action,
            "router_reasoning": decision.reasoning,
        },
    )


def _log_router_decision_for_outcome(
    *,
    event_log: EventLog,
    state: RunState,
    report: RunReport,
    deps: OrchestratorDeps,
) -> None:
    """Translate a RunReport into a router input and log the decision.

    Slightly indirect: we run the router twice per task (after EXECUTE,
    then after VERIFY) to produce a decision trail the contract test
    can grade. The real loop doesn't act on the router's output — it
    has already executed both phases via `run_task_with_fix_loop`.
    """
    assert state.plan is not None

    # ExecutionResult → router input. For success, the runner went on
    # to verify; for failed/escalated, we use the report status alone.
    if report.final_execution.status == "success":
        # Successful execution; map verify outcome onto router kinds.
        verify_kind: LastEventKind
        if report.status == "success":
            # Successful execution path: the verifier passed (or warned/flaked
            # — runner treats them all as success). Log EXECUTE→VERIFY then
            # VERIFY→NEXT_TASK so the trail is complete.
            verify_kind = "tests_passed"
        else:
            # report.status in {"failed", "escalated"} on the success exec
            # path means verifier returned critical and either retries
            # were available (now exhausted, hence "escalated") or this
            # is some weird intermediate. Treat as critical.
            verify_kind = "tests_critical"

        _log_router_decision(
            event_log=event_log,
            state=state,
            router_input=RouterInput(
                current_state="EXECUTE",
                last_event_kind="execution_succeeded",
                retry_caps_exhausted=False,
                more_tasks=True,
            ),
        )
        _log_router_decision(
            event_log=event_log,
            state=state,
            router_input=RouterInput(
                current_state="VERIFY",
                last_event_kind=verify_kind,
                retry_caps_exhausted=_caps_exhausted(state=state, deps=deps),
                more_tasks=_more_tasks(state=state),
            ),
        )
    else:
        # Hard execute failure (failed / no_changes / skipped — but
        # skipped never reaches here because it bypasses the runner).
        _log_router_decision(
            event_log=event_log,
            state=state,
            router_input=RouterInput(
                current_state="EXECUTE",
                last_event_kind="execution_failed",
                retry_caps_exhausted=_caps_exhausted(state=state, deps=deps),
                more_tasks=_more_tasks(state=state),
            ),
        )

    # NEXT_TASK turn — always logged at end of task, even on failure,
    # so the decision trail closes cleanly.
    _log_router_decision(
        event_log=event_log,
        state=state,
        router_input=RouterInput(
            current_state="NEXT_TASK",
            last_event_kind="more_tasks_remain" if _more_tasks(state=state) else "no_tasks_remain",
            retry_caps_exhausted=False,
            more_tasks=_more_tasks(state=state),
        ),
    )


# ---------------------------------------------------------------------------
# Caps helpers
# ---------------------------------------------------------------------------


def _caps_exhausted(*, state: RunState, deps: OrchestratorDeps) -> bool:
    """True if EITHER per-task cap (for current task) or run cap is hit.

    Note: by the time the runner returns a RunReport, it has already
    exercised the per-task cap internally. The router's view of "caps
    exhausted" is therefore mostly redundant with the runner's own
    "escalated" status — but we compute it explicitly so the shadow-LLM
    path (when enabled) sees the same booleans the deterministic
    router does.
    """
    per_run = deps.config.limits.max_retries_per_run
    if state.total_retries >= per_run:
        return True
    if state.current_task_id is None:
        return False
    per_task = deps.config.limits.max_retries_per_task
    return state.retry_counts.get(state.current_task_id, 0) >= per_task


def _more_tasks(*, state: RunState) -> bool:
    """True if at least one task in the plan hasn't been processed yet.

    "Processed" = present in completed | failed | skipped. We don't
    use `current_task_id` here because at the call site that task has
    just finished and been added to one of the lists.
    """
    if state.plan is None:
        return False
    processed = (
        set(state.completed_task_ids)
        | set(state.failed_task_ids)
        | set(state.skipped_task_ids)
    )
    return any(t.id not in processed for t in state.plan.tasks)


# ---------------------------------------------------------------------------
# Phase: REPORT (finalize)
# ---------------------------------------------------------------------------


def _finalize(
    *,
    state: RunState,
    deps: OrchestratorDeps,
    event_log: EventLog,
) -> RunState:
    """Run Reporter for any terminal status and save state."""
    state.status = (
        RunStatus.REPORTING
        if state.status not in (RunStatus.DONE, RunStatus.ESCALATED, RunStatus.FAILED)
        else state.status
    )
    save_state(state, deps.forge_root)

    reporter_persona = deps.personas.get("reporter")
    if reporter_persona is None:
        event_log.log(
            agent="orchestrator",
            phase="terminal",
            run_id=state.run_id,
            payload={
                "status": state.status.value,
                "reporter_skipped_reason": "reporter persona missing",
            },
        )
        save_state(state, deps.forge_root)
        return state

    try:
        run_reporter(
            run_id=state.run_id,
            user_story=state.user_story,
            forge_root=deps.forge_root,
            persona=reporter_persona,
            llm=deps.reporter_llm,
            event_log=event_log,
        )
    except Exception as e:
        # A Reporter failure is annoying but not fatal — we still have
        # the event log on disk, and the user can re-run Reporter via
        # `forge report <run_id>` (added in this stage's CLI work).
        event_log.log(
            agent="orchestrator",
            phase="reporter_failed",
            run_id=state.run_id,
            payload={
                "error": f"{type(e).__name__}: {e}",
            },
        )

    # Compute the final status: REPORTING is a transient state, so map
    # back to a terminal one. The choice depends on what happened
    # during the run, which we encoded earlier on `state.status`
    # before transitioning to REPORTING.
    if state.status == RunStatus.REPORTING:
        # Recompute terminal status from task lists (the loop sets it
        # already, but if we got here from a clean PLAN-only path the
        # status might still be REPORTING).
        if state.plan and any(t.id in state.failed_task_ids for t in state.plan.tasks):
            state.status = RunStatus.FAILED
        else:
            state.status = RunStatus.DONE

    event_log.log(
        agent="orchestrator",
        phase="terminal",
        run_id=state.run_id,
        payload={
            "status": state.status.value,
            "completed": list(state.completed_task_ids),
            "failed": list(state.failed_task_ids),
            "skipped": list(state.skipped_task_ids),
            "total_retries": state.total_retries,
        },
    )

    state.current_task_id = None
    save_state(state, deps.forge_root)
    return state
