"""Command-line entry point for forge.

Currently exposes one subcommand:

    forge plan "<user story>" \
        [--config .forge/config.toml] \
        [--repo .] \
        [--architecture .forge/knowledge/architecture.md] \
        [--out plan.json]

`plan` writes the validated Plan as JSON to stdout (or `--out` if given)
and a short human-readable summary to stderr. Stage 4 explicitly does NOT
persist the Plan to .forge/runs/ — Stage 7's Orchestrator owns that path.

The CLI is split into argparse construction (`build_parser`) and the
handler (`cmd_plan`) so tests can drive the handler directly without
spawning a subprocess.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from forge.agents.executor import ExecutorError, run_executor
from forge.agents.planner import run_planner
from forge.aider_runner import AiderNotFoundError, AiderRunner
from forge.config import load_config, validate_credentials
from forge.event_log import EventLog
from forge.llm.factory import get_client
from forge.personas import load_all_personas
from forge.schemas import ExecutionResult, Plan
from forge.state import events_path, generate_run_id

#: Default paths relative to `--repo`. Centralized so tests and Stage 8's
#: `forge init` can reference the same constants.
DEFAULT_CONFIG = Path(".forge/config.toml")
DEFAULT_PERSONAS = Path(".forge/personas")
DEFAULT_ARCHITECTURE = Path(".forge/knowledge/architecture.md")


def build_parser() -> argparse.ArgumentParser:
    """Construct the argparse parser. Separated for testability."""
    parser = argparse.ArgumentParser(prog="forge", description="Agentic SDLC Forge")
    sub = parser.add_subparsers(dest="command", required=True)

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

    return parser


def cmd_plan(args: argparse.Namespace) -> int:
    """Handler for `forge plan`. Returns process exit code."""
    repo: Path = args.repo.resolve()
    config_path: Path = (args.config or (repo / DEFAULT_CONFIG)).resolve()
    architecture_path: Path = (args.architecture or (repo / DEFAULT_ARCHITECTURE)).resolve()
    personas_dir: Path = (repo / DEFAULT_PERSONAS).resolve()

    # ----- Load inputs (fail fast with clear messages before LLM call) -----
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

    # Planner uses an LLM provider, so we MUST validate credentials before
    # spending wall-clock time loading personas / building file trees.
    try:
        validate_credentials(config)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1

    try:
        personas = load_all_personas(personas_dir)
    except Exception as e:  # PersonaLoadError or filesystem issue
        print(f"error: failed to load personas from {personas_dir}: {e}", file=sys.stderr)
        return 1

    if "planner" not in personas:
        print(
            f"error: planner persona missing from {personas_dir}. "
            f"Found: {sorted(personas.keys())}.",
            file=sys.stderr,
        )
        return 1

    # ----- Wire up runtime objects -----
    run_id = generate_run_id()
    forge_root = repo / ".forge"
    log_path = events_path(forge_root, run_id)
    llm = get_client("planner", config)

    print(f"[forge] run_id: {run_id}", file=sys.stderr)
    print(f"[forge] events: {log_path}", file=sys.stderr)

    # ----- Run -----
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

    # ----- Output -----
    plan_json = plan.model_dump_json(indent=2)

    if args.out is not None:
        args.out.write_text(plan_json + "\n", encoding="utf-8")
        print(f"[forge] wrote plan to {args.out}", file=sys.stderr)
    else:
        sys.stdout.write(plan_json + "\n")

    sys.stderr.write(_summary(plan))
    return 0


def cmd_execute(args: argparse.Namespace) -> int:
    """Handler for `forge execute <task_id>`. Returns process exit code.

    Per Stage 5 / D5: run_id comes from the plan, not regenerated. Events
    for this execution append to .forge/runs/<plan.run_id>/events.jsonl,
    keeping the trail of one run (planning + executions) in one file.

    Exit codes:
        0  — task completed with status="success"
        1  — pre-flight error (missing plan, unknown task_id, dirty repo,
              missing aider binary, etc.) — execution did not start
        2  — execution completed but task status was failed/no_changes;
              result JSON still printed to stdout for tooling
    """
    repo: Path = args.repo.resolve()
    plan_path: Path = args.plan.resolve()

    if not plan_path.exists():
        print(f"error: plan file not found at {plan_path}", file=sys.stderr)
        return 1

    try:
        plan = Plan.model_validate_json(plan_path.read_text(encoding="utf-8"))
    except Exception as e:  # malformed JSON or schema mismatch
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

    # AiderRunner failing here means aider isn't on PATH — fail fast with
    # a clear message instead of a confusing FileNotFoundError mid-run.
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

    # Surface non-success as a non-zero exit so shell pipelines can branch on it.
    # The result JSON still went to stdout — callers who care about the detail
    # can parse it; callers who just need a yes/no can check the exit code.
    return 0 if result.status == "success" else 2


def _execution_summary(result: ExecutionResult) -> str:
    """Short stderr summary mirroring _summary's tone."""
    lines: list[str] = ["", f"## Execution result: {result.task_id}", ""]
    lines.append(f"**Status:** {result.status}")
    if result.files_changed:
        lines.append("")
        lines.append("**Files changed:**")
        for f in result.files_changed:
            lines.append(f"- {f}")
    if result.status != "success" and result.aider_stderr:
        # Trim long stderr to last 800 chars — full output is in events.jsonl
        excerpt = result.aider_stderr[-800:]
        lines.append("")
        lines.append("**Stderr (excerpt):**")
        lines.append("```")
        lines.append(excerpt)
        lines.append("```")
    lines.append("")
    return "\n".join(lines)


def _summary(plan: Plan) -> str:
    """One-shot human-readable summary for stderr.

    Markdown-shaped so a user can pipe it to a file and read it later, but
    plain enough to scan in a terminal.
    """
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


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())