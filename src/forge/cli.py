"""Command-line entry point for forge.

Subcommands:

    forge plan "<user story>"                   # produce a Plan
    forge execute <task_id> --plan plan.json    # run one task
    forge verify <task_id> --plan plan.json     # verify one task
    forge run "<user story>"                    # full pipeline (Stage 7)
    forge run --resume <run_id>                 # resume a saved run
    forge report <run_id>                       # re-render RUN_REPORT.md

`plan` is the Stage 4 entry point and writes the validated Plan as JSON
to stdout (or `--out` if given). `execute` and `verify` are the Stage 5
and 6 entry points. `run` and `report` are added in Stage 7 — `run` is
the full end-to-end pipeline; `report` re-renders the markdown report
from an existing event log without re-running anything else.

The CLI is split into argparse construction (`build_parser`) and the
per-subcommand handlers (`cmd_plan`, `cmd_execute`, ...) so tests can
drive the handler directly without spawning a subprocess.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from forge.agents.executor import ExecutorError, run_executor
from forge.agents.orchestrator import (
    OrchestratorDeps,
    OrchestratorError,
    run_orchestrator,
)
from forge.agents.planner import run_planner
from forge.agents.reporter import ReporterError, run_reporter
from forge.agents.verifier import run_verifier
from forge.aider_runner import AiderNotFoundError, AiderRunner
from forge.config import load_config, validate_credentials
from forge.event_log import EventLog
from forge.llm.factory import get_client
from forge.personas import load_all_personas
from forge.schemas import ExecutionResult, Plan, RunState, RunStatus, TestReport
from forge.state import events_path, generate_run_id, load_state, save_state

#: Default paths relative to `--repo`. Centralized so tests and Stage 8's
#: `forge init` can reference the same constants.
DEFAULT_CONFIG = Path(".forge/config.toml")
DEFAULT_PERSONAS = Path(".forge/personas")
DEFAULT_ARCHITECTURE = Path(".forge/knowledge/architecture.md")


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser. Separated for testability."""
    parser = argparse.ArgumentParser(prog="forge", description="Agentic SDLC Forge")
    sub = parser.add_subparsers(dest="command", required=True)

    # ----------- plan ------------------------------------------------------
    plan = sub.add_parser("plan", help="Run the Planner on a user story.")
    plan.add_argument(
        "user_story",
        help="The user story to plan, in quotes.",
    )
    plan.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config TOML. Default: <repo>/{DEFAULT_CONFIG}.",
    )
    plan.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repo root. Default: current directory.",
    )
    plan.add_argument(
        "--architecture",
        type=Path,
        default=None,
        help=(
            f"Path to the generated architecture map. "
            f"Default: <repo>/{DEFAULT_ARCHITECTURE}. "
            f"If missing, run `forge init` first (Stage 8)."
        ),
    )
    plan.add_argument(
        "--out",
        type=Path,
        default=None,
        help="Write Plan JSON to this file instead of stdout.",
    )
    plan.set_defaults(func=cmd_plan)

    # ----------- execute ---------------------------------------------------
    execute = sub.add_parser(
        "execute",
        help="Run the Executor on one task from a plan.",
    )
    execute.add_argument(
        "task_id",
        help="The task ID to execute (e.g. task-001).",
    )
    execute.add_argument(
        "--plan",
        type=Path,
        required=True,
        help="Path to plan.json (produced by `forge plan --out`).",
    )
    execute.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repo root. Default: current directory.",
    )
    execute.set_defaults(func=cmd_execute)

    # ----------- verify ----------------------------------------------------
    verify = sub.add_parser(
        "verify",
        help="Run the Verifier on one task using config.toml's verification commands.",
    )
    verify.add_argument(
        "task_id",
        help="The task ID to verify (e.g. task-001).",
    )
    verify.add_argument(
        "--plan",
        type=Path,
        required=True,
        help="Path to plan.json. Used for the run_id and task lookup.",
    )
    verify.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config TOML. Default: <repo>/{DEFAULT_CONFIG}.",
    )
    verify.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repo root. Default: current directory.",
    )
    verify.set_defaults(func=cmd_verify)

    # ----------- run (Stage 7) --------------------------------------------
    run_p = sub.add_parser(
        "run",
        help="Full pipeline: plan, execute, verify, report. Supports --resume.",
    )
    # `user_story` is optional because `--resume <run_id>` doesn't need it.
    # Validation happens in the handler so the error message can be specific.
    run_p.add_argument(
        "user_story",
        nargs="?",
        default=None,
        help="The user story to run. Omit when using --resume.",
    )
    run_p.add_argument(
        "--resume",
        metavar="RUN_ID",
        default=None,
        help=(
            "Resume an existing run by ID. The run's state.json must "
            "exist under .forge/runs/<RUN_ID>/ and be in a non-terminal "
            "status (i.e. not DONE/FAILED/ESCALATED)."
        ),
    )
    run_p.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config TOML. Default: <repo>/{DEFAULT_CONFIG}.",
    )
    run_p.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repo root. Default: current directory.",
    )
    run_p.add_argument(
        "--architecture",
        type=Path,
        default=None,
        help=f"Path to architecture map. Default: <repo>/{DEFAULT_ARCHITECTURE}.",
    )
    run_p.set_defaults(func=cmd_run)

    # ----------- report (Stage 7) -----------------------------------------
    report_p = sub.add_parser(
        "report",
        help="Re-render RUN_REPORT.md from an existing run's event log.",
    )
    report_p.add_argument(
        "run_id",
        help="The run ID to report on (e.g. 20260511-120000-abcdef).",
    )
    report_p.add_argument(
        "--config",
        type=Path,
        default=None,
        help=f"Path to config TOML. Default: <repo>/{DEFAULT_CONFIG}.",
    )
    report_p.add_argument(
        "--repo",
        type=Path,
        default=Path("."),
        help="Repo root. Default: current directory.",
    )
    report_p.set_defaults(func=cmd_report)

    return parser


# ===========================================================================
# cmd_plan — Stage 4
# ===========================================================================


def cmd_plan(args: argparse.Namespace) -> int:
    """Handler for `forge plan`. Returns process exit code."""
    repo: Path = args.repo.resolve()
    config_path: Path = (args.config or (repo / DEFAULT_CONFIG)).resolve()
    architecture_path: Path = (args.architecture or (repo / DEFAULT_ARCHITECTURE)).resolve()
    personas_dir: Path = (repo / DEFAULT_PERSONAS).resolve()

    if not architecture_path.exists():
        print(
            f"error: architecture map not found at {architecture_path}.\n"
            f"Run `forge init` to generate it, or pass --architecture <path>.",
            file=sys.stderr,
        )
        return 1

    architecture_map = architecture_path.read_text(encoding="utf-8")

    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        validate_credentials(config)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        personas = load_all_personas(personas_dir)
    except Exception as e:
        print(f"error: failed to load personas from {personas_dir}: {e}", file=sys.stderr)
        return 1

    if "planner" not in personas:
        print(
            f"error: planner persona missing from {personas_dir}. "
            f"Found: {sorted(personas.keys())}.",
            file=sys.stderr,
        )
        return 1

    run_id = generate_run_id()
    forge_root = repo / ".forge"
    log_path = events_path(forge_root, run_id)
    llm = get_client("planner", config)

    print(f"[forge] run_id: {run_id}", file=sys.stderr)
    print(f"[forge] events: {log_path}", file=sys.stderr)

    with EventLog(log_path) as event_log:
        try:
            plan = run_planner(
                user_story=args.user_story,
                run_id=run_id,
                architecture_map=architecture_map,
                repo_root=repo,
                persona=personas["planner"],
                llm=llm,
                event_log=event_log,
            )
        except Exception as e:
            print(f"error: planner failed: {e}", file=sys.stderr)
            return 1

    plan_json = plan.model_dump_json(indent=2)

    if args.out is not None:
        args.out.write_text(plan_json + "\n", encoding="utf-8")
        print(f"[forge] wrote plan to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(plan_json + "\n")

    sys.stderr.write(_summary(plan))
    return 0


# ===========================================================================
# cmd_execute — Stage 5
# ===========================================================================


def cmd_execute(args: argparse.Namespace) -> int:
    """Handler for `forge execute <task_id>`. Returns process exit code."""
    repo: Path = args.repo.resolve()
    plan_path: Path = args.plan.resolve()

    if not plan_path.exists():
        print(f"error: plan file not found at {plan_path}", file=sys.stderr)
        return 1

    try:
        plan = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"error: failed to load plan from {plan_path}: {e}", file=sys.stderr)
        return 1

    task = next((t for t in plan.tasks if t.id == args.task_id), None)
    if task is None:
        known = ", ".join(t.id for t in plan.tasks) or "(no tasks in plan)"
        print(
            f"error: task {args.task_id!r} not found in plan. Known tasks: {known}",
            file=sys.stderr,
        )
        return 1

    try:
        aider = AiderRunner()
    except AiderNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    forge_root = repo / ".forge"
    log_path = events_path(forge_root, plan.run_id)

    print(f"[forge] run_id: {plan.run_id}", file=sys.stderr)
    print(f"[forge] task: {task.id}", file=sys.stderr)
    print(f"[forge] events: {log_path}", file=sys.stderr)

    with EventLog(log_path) as event_log:
        try:
            result = run_executor(
                task=task,
                run_id=plan.run_id,
                repo_root=repo,
                aider=aider,
                event_log=event_log,
            )
        except ExecutorError as e:
            print(f"error: executor pre-flight failed: {e}", file=sys.stderr)
            return 1

    sys.stdout.write(result.model_dump_json(indent=2) + "\n")
    sys.stderr.write(_execution_summary(result))
    return 0 if result.status == "success" else 2


# ===========================================================================
# cmd_verify — Stage 6
# ===========================================================================


def cmd_verify(args: argparse.Namespace) -> int:
    """Handler for `forge verify <task_id>`. Returns process exit code."""
    repo: Path = args.repo.resolve()
    plan_path: Path = args.plan.resolve()
    config_path: Path = (args.config or (repo / DEFAULT_CONFIG)).resolve()
    personas_dir: Path = (repo / DEFAULT_PERSONAS).resolve()

    if not plan_path.exists():
        print(f"error: plan file not found at {plan_path}", file=sys.stderr)
        return 1

    try:
        plan = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"error: failed to load plan from {plan_path}: {e}", file=sys.stderr)
        return 1

    task = next((t for t in plan.tasks if t.id == args.task_id), None)
    if task is None:
        known = ", ".join(t.id for t in plan.tasks) or "(no tasks in plan)"
        print(
            f"error: task {args.task_id!r} not found in plan. Known tasks: {known}",
            file=sys.stderr,
        )
        return 1

    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        validate_credentials(config)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        personas = load_all_personas(personas_dir)
    except Exception as e:
        print(f"error: failed to load personas from {personas_dir}: {e}", file=sys.stderr)
        return 1

    if "verifier" not in personas:
        print(
            f"error: verifier persona missing from {personas_dir}. "
            f"Found: {sorted(personas.keys())}.",
            file=sys.stderr,
        )
        return 1

    if not config.verification.commands:
        print(
            "error: no verification commands configured. "
            f"Add a [[verification.commands]] section to {config_path} "
            "(see .forge/presets/ for examples).",
            file=sys.stderr,
        )
        return 1

    forge_root = repo / ".forge"
    log_path = events_path(forge_root, plan.run_id)

    execution_result = _reconstruct_execution_result(log_path, task.id)
    if execution_result is None:
        print(
            f"error: no successful executor:validated event found for "
            f"task {task.id!r} in {log_path}. Run `forge execute {task.id}` first.",
            file=sys.stderr,
        )
        return 1

    llm = get_client("verifier", config)

    print(f"[forge] run_id: {plan.run_id}", file=sys.stderr)
    print(f"[forge] task: {task.id}", file=sys.stderr)
    print(f"[forge] events: {log_path}", file=sys.stderr)

    with EventLog(log_path) as event_log:
        report = run_verifier(
            task=task,
            run_id=plan.run_id,
            execution_result=execution_result,
            repo_root=repo,
            commands=config.verification.commands,
            persona=personas["verifier"],
            llm=llm,
            event_log=event_log,
        )

    sys.stdout.write(report.model_dump_json(indent=2) + "\n")
    sys.stderr.write(_verify_summary(report))
    return 0 if report.severity != "critical" else 2


# ===========================================================================
# cmd_run — Stage 7
# ===========================================================================


def cmd_run(args: argparse.Namespace) -> int:
    """Handler for `forge run "<user story>"` and `forge run --resume <run_id>`.

    Exit codes:
        0 — run finished with status=DONE (all tasks succeeded or were
            non-blockingly skipped)
        1 — pre-flight error (missing config/personas/architecture,
            invalid resume target, etc.)
        2 — run completed but in FAILED or ESCALATED status. The
            report has been written; the user should inspect it.
    """
    repo: Path = args.repo.resolve()
    config_path: Path = (args.config or (repo / DEFAULT_CONFIG)).resolve()
    architecture_path: Path = (args.architecture or (repo / DEFAULT_ARCHITECTURE)).resolve()
    personas_dir: Path = (repo / DEFAULT_PERSONAS).resolve()
    forge_root = repo / ".forge"

    # ---- Validate arg combinations ----
    if args.resume is not None and args.user_story is not None:
        print(
            "error: --resume is mutually exclusive with a user_story argument. "
            "Either start a new run with a user story, or resume an existing one.",
            file=sys.stderr,
        )
        return 1
    if args.resume is None and args.user_story is None:
        print(
            "error: provide a user_story argument to start a new run, "
            "or use --resume <run_id> to resume an existing one.",
            file=sys.stderr,
        )
        return 1

    # ---- Load config / credentials / personas ----
    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        validate_credentials(config)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        personas = load_all_personas(personas_dir)
    except Exception as e:
        print(f"error: failed to load personas from {personas_dir}: {e}", file=sys.stderr)
        return 1

    # All five personas required. Reporter is optional only in the sense
    # that the orchestrator handles its absence gracefully — but for
    # `forge run` we insist all of them are present, since absence
    # almost certainly means a misconfigured `.forge/`.
    required = {"orchestrator", "planner", "executor", "verifier", "reporter"}
    missing = required - set(personas)
    if missing:
        print(
            f"error: missing personas in {personas_dir}: {sorted(missing)}. "
            f"Found: {sorted(personas)}.",
            file=sys.stderr,
        )
        return 1

    if not config.verification.commands:
        print(
            "error: no verification commands configured. "
            f"Add a [[verification.commands]] section to {config_path}.",
            file=sys.stderr,
        )
        return 1

    # ---- Architecture map: only required for *new* runs. On resume
    # the plan is already in state.json and we don't re-run Planner.
    architecture_map = ""
    if args.resume is None:
        if not architecture_path.exists():
            print(
                f"error: architecture map not found at {architecture_path}.\n"
                f"Run `forge init` to generate it, or pass --architecture <path>.",
                file=sys.stderr,
            )
            return 1
        architecture_map = architecture_path.read_text(encoding="utf-8")

    # ---- Build / load RunState ----
    if args.resume is not None:
        try:
            state = load_state(args.resume, forge_root)
        except FileNotFoundError:
            print(
                f"error: no saved state for run {args.resume!r} at "
                f"{forge_root}/runs/{args.resume}/state.json. "
                f"Cannot resume.",
                file=sys.stderr,
            )
            return 1
        except ValueError as e:
            # Schema-version mismatch from load_state.
            print(f"error: {e}", file=sys.stderr)
            return 1

        if state.status in (RunStatus.DONE, RunStatus.FAILED, RunStatus.ESCALATED):
            print(
                f"error: run {state.run_id!r} is in terminal state "
                f"{state.status.value!r}. Nothing to resume.",
                file=sys.stderr,
            )
            return 1
    else:
        run_id = generate_run_id()
        state = RunState(run_id=run_id, user_story=args.user_story)
        # Persist immediately so a crash before the first save_state inside
        # the orchestrator still leaves us a recoverable snapshot.
        save_state(state, forge_root)

    # ---- Build LLM clients ----
    planner_llm = get_client("planner", config)
    verifier_llm = get_client("verifier", config)
    reporter_llm = get_client("reporter", config)

    # ---- Build AiderRunner ----
    try:
        aider = AiderRunner()
    except AiderNotFoundError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    deps = OrchestratorDeps(
        config=config,
        personas=personas,
        repo_root=repo,
        forge_root=forge_root,
        architecture_map=architecture_map,
        planner_llm=planner_llm,
        verifier_llm=verifier_llm,
        reporter_llm=reporter_llm,
        aider=aider,
    )

    log_path = events_path(forge_root, state.run_id)

    print(f"[forge] run_id: {state.run_id}", file=sys.stderr)
    print(f"[forge] events: {log_path}", file=sys.stderr)
    if args.resume is not None:
        print(
            f"[forge] resuming from status={state.status.value} "
            f"(completed={len(state.completed_task_ids)}, "
            f"failed={len(state.failed_task_ids)}, "
            f"skipped={len(state.skipped_task_ids)})",
            file=sys.stderr,
        )

    with EventLog(log_path) as event_log:
        try:
            final = run_orchestrator(state=state, deps=deps, event_log=event_log)
        except OrchestratorError as e:
            print(f"error: orchestrator pre-flight: {e}", file=sys.stderr)
            return 1

    sys.stderr.write(_run_summary(final, forge_root))

    if final.status == RunStatus.DONE:
        return 0
    return 2


# ===========================================================================
# cmd_report — Stage 7
# ===========================================================================


def cmd_report(args: argparse.Namespace) -> int:
    """Handler for `forge report <run_id>`. Re-renders RUN_REPORT.md.

    Useful for: (a) iterating on the reporter persona without re-running
    a real pipeline, (b) regenerating after editing reporter.md, (c)
    producing a report for a run that crashed before Reporter could run.

    Exit codes:
        0 — report written successfully
        1 — pre-flight error (no state, no events, missing personas, etc.)
    """
    repo: Path = args.repo.resolve()
    config_path: Path = (args.config or (repo / DEFAULT_CONFIG)).resolve()
    personas_dir: Path = (repo / DEFAULT_PERSONAS).resolve()
    forge_root = repo / ".forge"

    try:
        state = load_state(args.run_id, forge_root)
    except FileNotFoundError:
        print(
            f"error: no saved state for run {args.run_id!r}. "
            f"Looked at {forge_root}/runs/{args.run_id}/state.json.",
            file=sys.stderr,
        )
        return 1
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        config = load_config(config_path)
    except (FileNotFoundError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        validate_credentials(config)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        personas = load_all_personas(personas_dir)
    except Exception as e:
        print(f"error: failed to load personas from {personas_dir}: {e}", file=sys.stderr)
        return 1

    if "reporter" not in personas:
        print(
            f"error: reporter persona missing from {personas_dir}.",
            file=sys.stderr,
        )
        return 1

    llm = get_client("reporter", config)
    log_path = events_path(forge_root, state.run_id)

    print(f"[forge] run_id: {state.run_id}", file=sys.stderr)
    print(f"[forge] events: {log_path}", file=sys.stderr)

    with EventLog(log_path) as event_log:
        try:
            out_path = run_reporter(
                run_id=state.run_id,
                user_story=state.user_story,
                forge_root=forge_root,
                persona=personas["reporter"],
                llm=llm,
                event_log=event_log,
            )
        except ReporterError as e:
            print(f"error: {e}", file=sys.stderr)
            return 1

    print(f"[forge] wrote report to {out_path}", file=sys.stderr)
    return 0


# ===========================================================================
# Helpers
# ===========================================================================


def _reconstruct_execution_result(log_path: Path, task_id: str) -> ExecutionResult | None:
    """Scan the events log for the most recent successful
    executor:validated event for the given task and rebuild an
    ExecutionResult from it. Used by `cmd_verify` and indirectly by
    Stage 7's idempotency tests.

    Returns a synthetic ExecutionResult with the recovered files_changed
    list, or None when no such event exists.
    """
    if not log_path.exists():
        return None

    last_files: list[Path] | None = None
    for event in EventLog.read(log_path):
        if event.agent != "executor" or event.phase != "validated":
            continue
        payload = event.payload
        if payload.get("task_id") != task_id:
            continue
        if payload.get("status") != "success":
            continue
        files_raw = payload.get("files_changed") or []
        last_files = [Path(p) for p in files_raw]

    if last_files is None:
        return None

    return ExecutionResult(
        task_id=task_id,
        status="success",
        aider_stdout="",
        aider_stderr="",
        files_changed=last_files,
        duration_ms=0,
    )


def _verify_summary(report: TestReport) -> str:
    """Short stderr summary mirroring _execution_summary's tone."""
    lines: list[str] = ["", f"## Verification result: {report.task_id}", ""]
    lines.append(f"**Severity:** {report.severity}")
    lines.append(f"**Passed:** {report.passed}")
    if report.failures:
        lines.append("")
        lines.append(f"**Failures ({len(report.failures)}):**")
        for f in report.failures:
            headline = f.message or f"{f.category} failure"
            lines.append(f"- [{f.category}] {headline}")
            lines.append(f"  - command: `{f.command}` (exit {f.exit_code})")
    lines.append("")
    return "\n".join(lines)


def _execution_summary(result: ExecutionResult) -> str:
    """Short stderr summary for a single task execution."""
    lines: list[str] = ["", f"## Execution result: {result.task_id}", ""]
    lines.append(f"**Status:** {result.status}")
    if result.files_changed:
        lines.append("")
        lines.append("**Files changed:**")
        for f in result.files_changed:
            lines.append(f"- {f}")
    if result.status != "success" and result.aider_stderr:
        excerpt = result.aider_stderr[-800:]
        lines.append("")
        lines.append("**Stderr (excerpt):**")
        lines.append("```")
        lines.append(excerpt)
        lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _summary(plan: Plan) -> str:
    """One-shot human-readable plan summary for stderr."""
    lines: list[str] = []
    lines.append("")
    lines.append(f"## Plan for run {plan.run_id}")
    lines.append("")
    lines.append(f"**User story:** {plan.user_story}")
    lines.append("")
    lines.append(f"**Tasks ({len(plan.tasks)}):**")
    lines.append("")
    for task in plan.tasks:
        deps = f" (depends on: {', '.join(task.depends_on)})" if task.depends_on else ""
        files = (
            ", ".join(str(f) for f in task.files) if task.files else "(no files declared)"
        )
        lines.append(f"- **{task.id}** — {task.goal}{deps}")
        lines.append(f"  - files: {files}")
        lines.append(f"  - acceptance: {len(task.acceptance_criteria)} criteria")
    lines.append("")
    return "\n".join(lines)


def _run_summary(state: RunState, forge_root: Path) -> str:
    """Short stderr summary for `forge run` end."""
    report_path = forge_root / "runs" / state.run_id / "RUN_REPORT.md"
    lines: list[str] = []
    lines.append("")
    lines.append(f"## Run {state.run_id}")
    lines.append("")
    lines.append(f"**Status:** {state.status.value}")
    lines.append(f"**Completed:** {len(state.completed_task_ids)}")
    lines.append(f"**Failed:** {len(state.failed_task_ids)}")
    lines.append(f"**Skipped:** {len(state.skipped_task_ids)}")
    lines.append(f"**Total retries:** {state.total_retries}")
    if report_path.exists():
        lines.append("")
        lines.append(f"Report: {report_path}")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())