"""Planner agent — turns a user story into a validated `Plan`.

This is the first agent that actually calls an LLM. The contract:

    plan = run_planner(
        user_story=...,
        run_id=...,
        architecture_map=...,   # already-loaded text content
        repo_root=...,          # for file_tree generation
        persona=...,            # loaded by forge.personas
        llm=...,                # constructed by forge.llm.factory
        event_log=...,          # open EventLog for this run
    )

Quality gates (post-LLM):
- `plan.run_id` must match the run_id we passed in.
- `plan.user_story` must be a substring of (or equal to) what we sent.
  We don't require exact equality because some models normalize whitespace.
- Each task is checked for atomicity violations (>3 files, 0 acceptance
  criteria, "and"/"also" in goal). Violations are logged as
  `planner/quality_warning` events but do NOT reject the plan — the
  Definition of Done says "manually inspect", so the human reviews
  whether the warnings represent real problems.

What we DO reject (raise `PlannerError`):
- run_id mismatch (we asked for run X, model produced run Y — that's a
  contract break, not a quality concern).
- LLM-side failures bubble up as `LLMValidationError` /
  `LLMTransportError`; Planner doesn't try to "fix" those.

Persistence is NOT this module's concern. Stage 7's Orchestrator decides
where Plans live on disk; Stage 4 just returns the validated Plan.
"""

from __future__ import annotations

import time
from pathlib import Path

from forge.event_log import EventLog
from forge.file_tree import build_file_tree
from forge.llm.base import LLMClient
from forge.personas import Persona
from forge.schemas import Plan, Task

#: Atomicity thresholds — mirror the Planner prompt's "≤3 files" rule.
#: Kept here as a constant rather than re-derived from the prompt because
#: the runtime should be able to flag violations without parsing markdown.
MAX_FILES_PER_TASK = 3

#: Words that signal a task does two things at once. Prompt forbids them
#: in `goal`. Lowercased compare; bounded with whitespace to avoid matching
#: substrings like "andante" or "alsop".
_GOAL_FORBIDDEN_WORDS = ("and", "also")


class PlannerError(Exception):
    """Raised when the Planner produces a Plan that violates a hard contract.

    Quality warnings (oversized tasks, weak acceptance criteria) are logged
    but do not raise — see module docstring.
    """


def run_planner(
    *,
    user_story: str,
    run_id: str,
    architecture_map: str,
    repo_root: Path,
    persona: Persona,
    llm: LLMClient,
    event_log: EventLog,
) -> Plan:
    """Run the Planner end-to-end and return a validated `Plan`.

    Args:
        user_story: The human's request, verbatim.
        run_id: The current run's ID. The Planner echoes this back in the
            Plan; we verify equality to catch any contract drift.
        architecture_map: Text content of the project's architecture map
            (produced earlier by `forge init` interview, Stage 8). The
            caller loads this from disk; this function does not do file I/O
            on it because tests want to inject synthetic values.
        repo_root: Directory whose file layout the Planner should see.
            Used for `build_file_tree(repo_root)`.
        persona: Loaded Planner persona (frontmatter + body). Must declare
            output_schema=Plan; we assert this because a misconfigured
            persona would silently produce free text.
        llm: Provider client to call. Construction happens in
            `forge.llm.factory.get_client`.
        event_log: Open EventLog for this run. Planner emits at least three
            events: planner/start, planner/llm_call_complete, and
            planner/validated. Quality warnings emit additional events.

    Returns:
        A validated `Plan` with run_id and user_story matching the inputs.

    Raises:
        PlannerError: Schema-level validation failed at the Planner contract
            boundary (e.g. run_id mismatch).
        ValueError: Persona is misconfigured (wrong output_schema).
        LLMValidationError / LLMTransportError: Bubbled up from the LLM
            layer; Planner does not attempt to recover.
    """
    if persona.output_schema is not Plan:
        raise ValueError(
            f"Planner persona must declare output_schema=Plan, "
            f"got {persona.output_schema!r}. Check {persona.source_path}."
        )

    file_tree = build_file_tree(repo_root)

    event_log.log(
        agent="planner",
        phase="start",
        run_id=run_id,
        payload={
            "user_story_chars": len(user_story),
            "architecture_map_chars": len(architecture_map),
            "file_tree_lines": file_tree.count("\n") + (1 if file_tree else 0),
            "model": llm.__class__.__name__,
        },
    )

    system_prompt = persona.render(
        user_story=user_story,
        run_id=run_id,
        architecture_map=architecture_map,
        file_tree=file_tree,
    )

    # The user message is intentionally minimal — the persona body already
    # contains the entire spec, including the inputs interpolated above.
    # The user turn is just the trigger: "given that system prompt,
    # produce the Plan now".
    user_message = "Produce the Plan as a single JSON object matching the schema."

    t0 = time.monotonic()
    response = llm.complete(system=system_prompt, user=user_message, schema=Plan)
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    if not isinstance(response.content, Plan):
        # Defensive: LLMClient contract says schema=Plan -> content is Plan.
        # If a provider implementation regresses, we want a loud message
        # rather than a silent type error five lines later.
        raise PlannerError(
            f"LLM returned content of type {type(response.content).__name__}, "
            f"expected Plan. Provider: {response.provider} model: {response.model}."
        )

    plan: Plan = response.content

    event_log.log(
        agent="planner",
        phase="llm_call_complete",
        run_id=run_id,
        tokens_in=response.tokens_in,
        tokens_out=response.tokens_out,
        duration_ms=elapsed_ms,
        cost_usd=response.cost_usd,
        payload={
            "model": response.model,
            "provider": response.provider,
            "finish_reason": response.finish_reason,
            "retried_validation": response.retried_validation,
            "task_count": len(plan.tasks),
        },
    )

    # ----- Hard contract checks -----
    if plan.run_id != run_id:
        raise PlannerError(
            f"Plan.run_id mismatch: expected {run_id!r}, got {plan.run_id!r}. "
            f"The Planner must echo the run_id we provided."
        )

    # ----- Quality warnings (logged, not raised) -----
    warnings = _collect_quality_warnings(plan)
    for warning in warnings:
        event_log.log(
            agent="planner",
            phase="quality_warning",
            run_id=run_id,
            payload=warning,
        )

    event_log.log(
        agent="planner",
        phase="validated",
        run_id=run_id,
        payload={
            "task_count": len(plan.tasks),
            "warning_count": len(warnings),
        },
    )

    return plan


def _collect_quality_warnings(plan: Plan) -> list[dict[str, object]]:
    """Inspect each task for atomicity-rule violations.

    Returns a list of warning dicts, one per violation (a single task may
    produce multiple). Each dict is suitable as an EventLog payload.

    None of these reject the Plan — the Definition of Done for Stage 4 is
    "manually inspect"; warnings are signals for the human reviewer.
    """
    warnings: list[dict[str, object]] = []
    for task in plan.tasks:
        for warning in _check_task(task):
            warnings.append(warning)
    return warnings


def _check_task(task: Task) -> list[dict[str, object]]:
    """Return all atomicity violations for one task."""
    issues: list[dict[str, object]] = []

    if len(task.files) > MAX_FILES_PER_TASK:
        issues.append(
            {
                "task_id": task.id,
                "kind": "too_many_files",
                "files_count": len(task.files),
                "limit": MAX_FILES_PER_TASK,
            }
        )

    if not task.acceptance_criteria:
        issues.append(
            {
                "task_id": task.id,
                "kind": "no_acceptance_criteria",
            }
        )

    # "and"/"also" check on goal. Lowercase + whitespace-bounded so we
    # don't false-positive on words like "alsop". We accept "and" inside
    # other words (e.g. "land", "android") by requiring word boundaries.
    goal_words = task.goal.lower().split()
    forbidden = sorted(set(goal_words) & set(_GOAL_FORBIDDEN_WORDS))
    if forbidden:
        issues.append(
            {
                "task_id": task.id,
                "kind": "compound_goal",
                "forbidden_words": forbidden,
                "goal": task.goal,
            }
        )

    return issues
