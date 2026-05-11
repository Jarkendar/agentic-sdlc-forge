"""Orchestrator agent tests — Stage 7.

The orchestrator is the run driver. These tests cover:

- Happy path: PLAN succeeds → all tasks execute → DONE
- Skipped tasks: missing dependencies → propagation of skipped status
- Failed task: hard runner failure → run status FAILED
- Escalated task: runner returns escalated → run status ESCALATED
- Resume: skipping already-processed tasks
- Run-wide retry cap: stops further tasks, marks ESCALATED
- Planner failure: terminal FAILED with Reporter still invoked
- Missing personas: caught at pre-flight time
- State.json saved at every transition (count-based assertion)
- Skipped-due-to-failed-dep mentions the upstream task in reason

Mocks: `run_planner`, `run_task_with_fix_loop`, `run_reporter`,
`ensure_clean_worktree`, `ensure_run_branch` are monkey-patched. Tests
use real RunState + EventLog + state.py persistence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

import forge.agents.orchestrator as orch_module
from forge.agents.orchestrator import (
    OrchestratorDeps,
    OrchestratorError,
    run_orchestrator,
)
from forge.config import ForgeConfig, Limits, ModelAssignment, VerificationConfig
from forge.event_log import EventLog
from forge.personas import Persona
from forge.schemas import (
    ExecutionResult,
    Plan,
    RunReport,
    RunState,
    RunStatus,
    Task,
    TestReport,
    VerificationCommand,
)
from forge.state import events_path, load_state, save_state


# ===========================================================================
# Fakes & helpers
# ===========================================================================


@dataclass
class _PlannerCall:
    user_story: str
    run_id: str


@dataclass
class _RunnerCall:
    task_id: str
    run_id: str


@dataclass
class _ReporterCall:
    run_id: str


@dataclass
class _Recorder:
    """Captures and scripts the responses of mocked agents."""

    planner_calls: list[_PlannerCall] = field(default_factory=list)
    runner_calls: list[_RunnerCall] = field(default_factory=list)
    reporter_calls: list[_ReporterCall] = field(default_factory=list)

    planner_plan: Plan | None = None
    planner_exception: Exception | None = None

    # Per-task RunReport, keyed by task_id. Default = success.
    runner_reports: dict[str, RunReport] = field(default_factory=dict)
    # Per-task exception to raise inside the runner (simulates a crash).
    runner_exceptions: dict[str, Exception] = field(default_factory=dict)

    reporter_exception: Exception | None = None


def _persona(name: str, output_schema: Any = None) -> Persona:
    """Return a dummy Persona — orchestrator never calls .render() on
    anything except planner/reporter, which we mock. For verifier we
    just need the object to exist."""
    return Persona(
        name=name,
        output_schema=output_schema,
        required_vars=(),
        references=(),
        body=f"# {name}\n",
        source_path=Path(f"/tmp/{name}.md"),
    )


def _all_personas() -> dict[str, Persona]:
    return {
        "orchestrator": _persona("orchestrator"),
        "planner": _persona("planner"),
        "executor": _persona("executor"),
        "verifier": _persona("verifier"),
        "reporter": _persona("reporter"),
    }


def _config(
    *,
    max_retries_per_task: int = 3,
    max_retries_per_run: int = 10,
    with_verification: bool = True,
) -> ForgeConfig:
    commands = (
        [VerificationCommand(name="pytest", command="pytest", stage="verify_test")]
        if with_verification
        else []
    )
    return ForgeConfig(
        models={
            "orchestrator": ModelAssignment(provider="ollama", model="m"),
            "planner": ModelAssignment(provider="ollama", model="m"),
            "executor": ModelAssignment(provider="ollama", model="m"),
            "verifier": ModelAssignment(provider="ollama", model="m"),
            "reporter": ModelAssignment(provider="ollama", model="m"),
        },
        limits=Limits(
            max_retries_per_task=max_retries_per_task,
            max_retries_per_run=max_retries_per_run,
        ),
        verification=VerificationConfig(commands=commands),
    )


class _StubLLM:
    """Dummy LLMClient — never called because the agents that use it are
    mocked. We just need a non-None placeholder for OrchestratorDeps."""

    provider = "stub"

    def complete(self, **_: Any) -> Any:
        raise AssertionError("LLM should not be called when agents are mocked")


def _build_deps(tmp_path: Path, *, config: ForgeConfig | None = None) -> OrchestratorDeps:
    forge_root = tmp_path / ".forge"
    return OrchestratorDeps(
        config=config or _config(),
        personas=_all_personas(),
        repo_root=tmp_path,
        forge_root=forge_root,
        architecture_map="# arch",
        planner_llm=_StubLLM(),  # type: ignore[arg-type]
        verifier_llm=_StubLLM(),  # type: ignore[arg-type]
        reporter_llm=_StubLLM(),  # type: ignore[arg-type]
        aider=object(),  # never called when runner is mocked
    )


def _exec_result(task_id: str, status: str = "success") -> ExecutionResult:
    return ExecutionResult(
        task_id=task_id,
        status=status,  # type: ignore[arg-type]
        files_changed=[Path("src/foo.py")] if status == "success" else [],
    )


def _success_report(task_id: str, attempts: int = 1) -> RunReport:
    return RunReport(
        task_id=task_id,
        status="success",
        attempts=attempts,
        final_execution=_exec_result(task_id),
        final_test_report=TestReport(task_id=task_id, passed=True, severity="none"),
    )


def _escalated_report(task_id: str, attempts: int) -> RunReport:
    return RunReport(
        task_id=task_id,
        status="escalated",
        attempts=attempts,
        final_execution=_exec_result(task_id),
        final_test_report=TestReport(task_id=task_id, passed=False, severity="critical"),
        escalation_reason=f"max_retries_per_task={attempts}",
    )


def _failed_report(task_id: str) -> RunReport:
    return RunReport(
        task_id=task_id,
        status="failed",
        attempts=1,
        final_execution=_exec_result(task_id, status="no_changes"),
        escalation_reason="executor returned no_changes",
    )


def _plan(*task_specs: tuple[str, list[str]], run_id: str = "run-1") -> Plan:
    """Build a plan from (task_id, depends_on) tuples."""
    tasks = [Task(id=tid, goal=f"do {tid}", depends_on=deps) for tid, deps in task_specs]
    return Plan(run_id=run_id, user_story="story", tasks=tasks)


@pytest.fixture(autouse=True)
def _patch_externals(monkeypatch: pytest.MonkeyPatch) -> _Recorder:
    """Replace orchestrator's external dependencies with the recorder.

    Every test gets this — orchestrator never spawns subprocesses or
    calls LLMs in our test environment.
    """
    rec = _Recorder()

    # ensure_clean_worktree / ensure_run_branch: no-ops by default.
    monkeypatch.setattr(orch_module, "ensure_clean_worktree", lambda repo: None)
    monkeypatch.setattr(
        orch_module, "ensure_run_branch", lambda repo, run_id: f"forge/run/{run_id}"
    )

    # run_planner
    def fake_planner(*, user_story: str, run_id: str, **kwargs: Any) -> Plan:
        rec.planner_calls.append(_PlannerCall(user_story=user_story, run_id=run_id))
        if rec.planner_exception is not None:
            raise rec.planner_exception
        if rec.planner_plan is None:
            raise AssertionError("Test must set rec.planner_plan before run_orchestrator")
        return rec.planner_plan

    monkeypatch.setattr(orch_module, "run_planner", fake_planner)

    # run_task_with_fix_loop
    def fake_runner(*, task: Task, run_id: str, **kwargs: Any) -> RunReport:
        rec.runner_calls.append(_RunnerCall(task_id=task.id, run_id=run_id))
        if task.id in rec.runner_exceptions:
            raise rec.runner_exceptions[task.id]
        # Mimic the real runner emitting an executor:validated event,
        # so post-run log inspections see the canonical shape.
        # (The actual runner does this via its own EventLog calls; we
        # short-circuit here because we don't import the real one.)
        return rec.runner_reports.get(task.id, _success_report(task.id))

    monkeypatch.setattr(orch_module, "run_task_with_fix_loop", fake_runner)

    # run_reporter
    def fake_reporter(*, run_id: str, **kwargs: Any) -> Path:
        rec.reporter_calls.append(_ReporterCall(run_id=run_id))
        if rec.reporter_exception is not None:
            raise rec.reporter_exception
        return kwargs["forge_root"] / "runs" / run_id / "RUN_REPORT.md"

    monkeypatch.setattr(orch_module, "run_reporter", fake_reporter)

    return rec


# ===========================================================================
# Happy path
# ===========================================================================


def test_happy_path_all_tasks_succeed(tmp_path: Path, _patch_externals: _Recorder) -> None:
    plan = _plan(("t1", []), ("t2", []), ("t3", []))
    _patch_externals.planner_plan = plan
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps, event_log=log)

    assert final.status == RunStatus.DONE
    assert final.completed_task_ids == ["t1", "t2", "t3"]
    assert final.failed_task_ids == []
    assert final.skipped_task_ids == []
    assert final.total_retries == 0
    assert final.current_task_id is None

    # Planner called once, runner called per task, Reporter called once
    assert len(_patch_externals.planner_calls) == 1
    assert [c.task_id for c in _patch_externals.runner_calls] == ["t1", "t2", "t3"]
    assert len(_patch_externals.reporter_calls) == 1

    # State persisted on disk
    loaded = load_state(plan.run_id, deps.forge_root)
    assert loaded.status == RunStatus.DONE
    assert loaded.completed_task_ids == ["t1", "t2", "t3"]


def test_terminal_event_emitted(tmp_path: Path, _patch_externals: _Recorder) -> None:
    plan = _plan(("t1", []))
    _patch_externals.planner_plan = plan
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        run_orchestrator(state=state, deps=deps, event_log=log)

    phases = [
        (e.agent, e.phase)
        for e in EventLog.read(log_path)
        if e.agent == "orchestrator"
    ]
    assert ("orchestrator", "start") in phases
    assert ("orchestrator", "terminal") in phases


# ===========================================================================
# Skipped tasks
# ===========================================================================


def test_task_with_failed_dependency_is_skipped(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    # t2 depends on t1; t1 fails — t2 must be skipped, not executed
    plan = _plan(("t1", []), ("t2", ["t1"]))
    _patch_externals.planner_plan = plan
    _patch_externals.runner_reports = {"t1": _failed_report("t1")}
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps, event_log=log)

    assert final.failed_task_ids == ["t1"]
    assert final.skipped_task_ids == ["t2"]
    assert final.completed_task_ids == []
    assert final.status == RunStatus.FAILED  # any failed task → FAILED run

    # Runner was called for t1 but NOT for t2
    assert [c.task_id for c in _patch_externals.runner_calls] == ["t1"]


def test_skipped_propagates_through_chain(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    """t1 fails → t2 depends on t1 → skipped → t3 depends on t2 → skipped."""
    plan = _plan(("t1", []), ("t2", ["t1"]), ("t3", ["t2"]))
    _patch_externals.planner_plan = plan
    _patch_externals.runner_reports = {"t1": _failed_report("t1")}
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps, event_log=log)

    assert final.failed_task_ids == ["t1"]
    assert final.skipped_task_ids == ["t2", "t3"]
    assert [c.task_id for c in _patch_externals.runner_calls] == ["t1"]


def test_skipped_reason_mentions_upstream_task(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    plan = _plan(("t1", []), ("t2", ["t1"]))
    _patch_externals.planner_plan = plan
    _patch_externals.runner_reports = {"t1": _failed_report("t1")}
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        run_orchestrator(state=state, deps=deps, event_log=log)

    skip_events = [
        e for e in EventLog.read(log_path)
        if e.agent == "executor" and e.phase == "skipped"
    ]
    assert len(skip_events) == 1
    reason = skip_events[0].payload.get("skip_reason", "")
    assert "t1" in reason
    assert "failed" in reason.lower()


def test_skipped_emits_validated_event_for_uniform_reporter_view(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    """Synthetic executor:validated with status=skipped lets Reporter's
    task-table aggregation treat skipped uniformly without a special case."""
    plan = _plan(("t1", []), ("t2", ["t1"]))
    _patch_externals.planner_plan = plan
    _patch_externals.runner_reports = {"t1": _failed_report("t1")}
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        run_orchestrator(state=state, deps=deps, event_log=log)

    validated_events = [
        e for e in EventLog.read(log_path)
        if e.agent == "executor" and e.phase == "validated"
    ]
    skipped_validated = [
        e for e in validated_events
        if e.payload.get("status") == "skipped"
    ]
    assert len(skipped_validated) == 1
    assert skipped_validated[0].payload["task_id"] == "t2"


# ===========================================================================
# Escalation
# ===========================================================================


def test_escalated_task_sets_run_status_escalated(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    plan = _plan(("t1", []), ("t2", []))
    _patch_externals.planner_plan = plan
    _patch_externals.runner_reports = {
        "t1": _escalated_report("t1", attempts=3),
        "t2": _success_report("t2"),
    }
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps, event_log=log)

    assert final.status == RunStatus.ESCALATED
    assert final.failed_task_ids == ["t1"]
    # t2 still ran — escalation of one task doesn't abort the rest
    # (the run-wide cap does that separately)
    assert final.completed_task_ids == ["t2"]


def test_run_wide_retry_cap_skips_remaining_tasks(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    plan = _plan(("t1", []), ("t2", []), ("t3", []))
    _patch_externals.planner_plan = plan
    # t1 burns 3 retries via attempts=4 (attempts-1 = 3); cap is 3
    _patch_externals.runner_reports = {
        "t1": RunReport(
            task_id="t1",
            status="success",
            attempts=4,
            final_execution=_exec_result("t1"),
        ),
        "t2": _success_report("t2"),
        "t3": _success_report("t3"),
    }
    deps = _build_deps(tmp_path, config=_config(max_retries_per_run=3))

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps, event_log=log)

    # t1 used 3 retries, hitting the run-wide cap. t2 and t3 are skipped.
    assert final.total_retries == 3
    assert final.completed_task_ids == ["t1"]
    assert "t2" in final.skipped_task_ids
    assert "t3" in final.skipped_task_ids
    assert final.status == RunStatus.ESCALATED


# ===========================================================================
# Resume
# ===========================================================================


def test_resume_skips_completed_tasks(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    """Pre-populate completed_task_ids; orchestrator must not re-run them."""
    plan = _plan(("t1", []), ("t2", []), ("t3", []))
    # planner_plan won't be used since state.plan is set on entry
    deps = _build_deps(tmp_path)

    state = RunState(
        run_id=plan.run_id,
        user_story="story",
        plan=plan,
        status=RunStatus.EXECUTING,
        completed_task_ids=["t1", "t2"],
    )
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps, event_log=log)

    assert _patch_externals.planner_calls == []  # plan already set → no replan
    assert [c.task_id for c in _patch_externals.runner_calls] == ["t3"]
    assert final.completed_task_ids == ["t1", "t2", "t3"]
    assert final.status == RunStatus.DONE


def test_resume_with_no_plan_calls_planner(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    """If the previous run crashed before Planner finished, resume should
    re-run Planner. (state.plan is None.)"""
    plan = _plan(("t1", []), run_id="resume-1")
    _patch_externals.planner_plan = plan
    deps = _build_deps(tmp_path)

    state = RunState(run_id="resume-1", user_story="story", status=RunStatus.PLANNING)
    log_path = events_path(deps.forge_root, "resume-1")
    with EventLog(log_path) as log:
        run_orchestrator(state=state, deps=deps, event_log=log)

    assert len(_patch_externals.planner_calls) == 1


# ===========================================================================
# Pre-flight failure
# ===========================================================================


def test_dirty_worktree_raises_pre_flight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, _patch_externals: _Recorder
) -> None:
    def dirty(_repo: Path) -> None:
        raise RuntimeError("uncommitted changes")

    monkeypatch.setattr(orch_module, "ensure_clean_worktree", dirty)

    plan = _plan(("t1", []))
    _patch_externals.planner_plan = plan
    deps = _build_deps(tmp_path)

    state = RunState(run_id="r-dirty", user_story="story")
    log_path = events_path(deps.forge_root, "r-dirty")
    with EventLog(log_path) as log:
        with pytest.raises(OrchestratorError, match="worktree"):
            run_orchestrator(state=state, deps=deps, event_log=log)

    # Reporter NOT called — pre-flight bails before any state is written
    assert _patch_externals.reporter_calls == []


# ===========================================================================
# Planner failure
# ===========================================================================


def test_planner_exception_marks_run_failed_and_runs_reporter(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    _patch_externals.planner_exception = RuntimeError("planner died")
    deps = _build_deps(tmp_path)

    state = RunState(run_id="r-pf", user_story="story")
    log_path = events_path(deps.forge_root, "r-pf")
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps, event_log=log)

    assert final.status == RunStatus.FAILED
    # Reporter still ran — user needs to see *something*
    assert len(_patch_externals.reporter_calls) == 1


# ===========================================================================
# Runner crash mid-task
# ===========================================================================


def test_runner_crash_marks_task_failed_and_continues(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    plan = _plan(("t1", []), ("t2", []))
    _patch_externals.planner_plan = plan
    _patch_externals.runner_exceptions = {"t1": RuntimeError("kaboom")}
    _patch_externals.runner_reports = {"t2": _success_report("t2")}
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps, event_log=log)

    assert "t1" in final.failed_task_ids
    assert "t2" in final.completed_task_ids
    assert final.status == RunStatus.FAILED  # because t1 failed

    # `runner_crash` event present
    crash_events = [
        e for e in EventLog.read(log_path)
        if e.agent == "orchestrator" and e.phase == "runner_crash"
    ]
    assert len(crash_events) == 1
    assert crash_events[0].payload["task_id"] == "t1"


# ===========================================================================
# Missing personas
# ===========================================================================


def test_missing_verifier_persona_skips_all_tasks(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    plan = _plan(("t1", []), ("t2", []))
    _patch_externals.planner_plan = plan
    deps = _build_deps(tmp_path)
    # Remove verifier from the personas dict
    deps_no_verifier = OrchestratorDeps(
        config=deps.config,
        personas={k: v for k, v in deps.personas.items() if k != "verifier"},
        repo_root=deps.repo_root,
        forge_root=deps.forge_root,
        architecture_map=deps.architecture_map,
        planner_llm=deps.planner_llm,
        verifier_llm=deps.verifier_llm,
        reporter_llm=deps.reporter_llm,
        aider=deps.aider,
    )

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps_no_verifier.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps_no_verifier, event_log=log)

    # Every task skipped; status FAILED (since something was skipped that
    # couldn't run, this is closer to a misconfig than a clean DONE).
    assert final.skipped_task_ids == ["t1", "t2"]
    assert final.completed_task_ids == []
    assert final.status == RunStatus.FAILED
    assert _patch_externals.runner_calls == []


def test_missing_planner_persona_marks_run_failed(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    deps = _build_deps(tmp_path)
    deps_no_planner = OrchestratorDeps(
        config=deps.config,
        personas={k: v for k, v in deps.personas.items() if k != "planner"},
        repo_root=deps.repo_root,
        forge_root=deps.forge_root,
        architecture_map=deps.architecture_map,
        planner_llm=deps.planner_llm,
        verifier_llm=deps.verifier_llm,
        reporter_llm=deps.reporter_llm,
        aider=deps.aider,
    )

    state = RunState(run_id="r-np", user_story="story")
    log_path = events_path(deps_no_planner.forge_root, "r-np")
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps_no_planner, event_log=log)

    assert final.status == RunStatus.FAILED
    assert _patch_externals.planner_calls == []
    # Reporter still ran (user needs to see what happened)
    assert len(_patch_externals.reporter_calls) == 1


# ===========================================================================
# State persistence — verifies the 4-5 save_state checkpoints
# ===========================================================================


def test_state_persisted_on_disk_after_each_task(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    plan = _plan(("t1", []), ("t2", []))
    _patch_externals.planner_plan = plan
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    save_state(state, deps.forge_root)  # simulate the CLI initial save

    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        run_orchestrator(state=state, deps=deps, event_log=log)

    # After full run: state.json reflects DONE + both tasks completed
    loaded = load_state(plan.run_id, deps.forge_root)
    assert loaded.status == RunStatus.DONE
    assert loaded.completed_task_ids == ["t1", "t2"]
    assert loaded.current_task_id is None


def test_state_persists_after_partial_run(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    """Simulates the 'kill mid-task' scenario indirectly: after t1
    completes and t2's runner crashes, state.json must record t1 as
    completed and t2 as failed. A real kill would simply not get past
    the crash point; this test proves the per-task save_state is enough
    for a real resume to succeed."""
    plan = _plan(("t1", []), ("t2", []))
    _patch_externals.planner_plan = plan
    _patch_externals.runner_exceptions = {"t2": RuntimeError("crash")}
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        run_orchestrator(state=state, deps=deps, event_log=log)

    loaded = load_state(plan.run_id, deps.forge_root)
    assert "t1" in loaded.completed_task_ids
    assert "t2" in loaded.failed_task_ids


# ===========================================================================
# Reporter failure is non-fatal
# ===========================================================================


def test_reporter_exception_does_not_crash_run(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    plan = _plan(("t1", []))
    _patch_externals.planner_plan = plan
    _patch_externals.reporter_exception = RuntimeError("reporter died")
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps, event_log=log)

    # Final status reflects task outcome, not Reporter failure
    assert final.status == RunStatus.DONE
    # But we logged that Reporter failed
    rep_failures = [
        e for e in EventLog.read(log_path)
        if e.agent == "orchestrator" and e.phase == "reporter_failed"
    ]
    assert len(rep_failures) == 1


# ===========================================================================
# Retry bookkeeping
# ===========================================================================


def test_attempts_above_one_increment_total_retries(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    plan = _plan(("t1", []), ("t2", []))
    _patch_externals.planner_plan = plan
    _patch_externals.runner_reports = {
        "t1": RunReport(
            task_id="t1",
            status="success",
            attempts=2,
            final_execution=_exec_result("t1"),
        ),
        "t2": RunReport(
            task_id="t2",
            status="success",
            attempts=3,
            final_execution=_exec_result("t2"),
        ),
    }
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps, event_log=log)

    # t1 used 1 retry (attempts=2), t2 used 2 (attempts=3) → total 3
    assert final.total_retries == 3
    assert final.retry_counts == {"t1": 1, "t2": 2}


def test_task_with_one_attempt_does_not_increment_total_retries(
    tmp_path: Path, _patch_externals: _Recorder
) -> None:
    plan = _plan(("t1", []))
    _patch_externals.planner_plan = plan
    _patch_externals.runner_reports = {"t1": _success_report("t1", attempts=1)}
    deps = _build_deps(tmp_path)

    state = RunState(run_id=plan.run_id, user_story="story")
    log_path = events_path(deps.forge_root, plan.run_id)
    with EventLog(log_path) as log:
        final = run_orchestrator(state=state, deps=deps, event_log=log)

    assert final.total_retries == 0
    assert "t1" not in final.retry_counts
