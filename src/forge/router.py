"""Deterministic state-machine router — Stage 7.

This module is the executable form of the decision table in
`.forge/personas/orchestrator.md`. Per D1 (locked in Stage 7 planning),
the Orchestrator routes deterministically; the LLM persona stays as
documentation and as an optional shadow path (config knob, OFF by
default).

Why a dedicated module instead of folding this into `orchestrator.py`?
- The contract test (`tests/test_router_contract.py`) parses the
  decision table out of `orchestrator.md` and asserts that
  `decide_next_action` returns the same action for every row. Putting
  the routing function in its own thin module keeps that test focused
  and the orchestrator agent free to grow plumbing without dragging
  the contract surface with it.
- `orchestrator.py` will eventually own the *shadow LLM* call, retry
  bookkeeping, persistence — none of which belong in a pure router.

The router is a pure function of:
    (current_state, last_event_kind, retry_caps_exhausted, more_tasks)
and returns one `OrchestratorAction` plus a one-sentence reasoning
string. The orchestrator agent supplies those inputs derived from
RunState + the most recent log event; this module never touches I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from forge.schemas import OrchestratorAction

# Closed set of last-event kinds the router understands. Anything else
# is a contract violation and routes to ESCALATE (the "safest action"
# from orchestrator.md). Kept as a Literal so call sites can't silently
# pass an unknown string.
LastEventKind = Literal[
    "plan_succeeded",
    "execution_succeeded",
    "execution_failed",
    "tests_passed",
    "tests_critical",
    "tests_flaky",
    "more_tasks_remain",
    "no_tasks_remain",
]


@dataclass(frozen=True)
class RouterInput:
    """All inputs the deterministic router needs to pick the next action.

    Mirrors the decision table headers in orchestrator.md plus one
    extra (`more_tasks`) needed to distinguish NEXT_TASK→EXECUTE from
    NEXT_TASK→DONE. We don't fold that into `last_event_kind` because
    the kinds match observable events; "more tasks remain" is a
    derived predicate over RunState + Plan and the runtime computes it.

    `retry_caps_exhausted` is True when *either* the per-task or the
    per-run cap is reached for the current task. The orchestrator agent
    computes that — the router only needs the boolean.
    """

    current_state: Literal["PLAN", "EXECUTE", "VERIFY", "NEXT_TASK"]
    last_event_kind: LastEventKind
    retry_caps_exhausted: bool
    more_tasks: bool


@dataclass(frozen=True)
class RouterDecision:
    """What the router decided. Mirrors OrchestratorDecision but the
    `reasoning` is generated deterministically — no LLM in the loop.

    We don't reuse `OrchestratorDecision` directly because that schema
    is meant for LLM output (subject to extra="forbid" validation, etc.)
    and we want the router's data type to be a plain frozen dataclass
    for cheap equality in tests.
    """

    action: OrchestratorAction
    reasoning: str


def decide_next_action(inp: RouterInput) -> RouterDecision:
    """Pure function: inputs → action. No I/O, no LLM, no side effects.

    The decision table maps directly to the rows in orchestrator.md.
    The contract test `tests/test_router_contract.py` parses that
    file and verifies row-by-row that this function agrees.

    Anything outside the decision table routes to ESCALATE — that's
    the fallback explicitly required by orchestrator.md ("If the input
    does not match a row in this table, pick the safest action:
    ESCALATE.").
    """
    # PLAN row
    if inp.current_state == "PLAN":
        if inp.last_event_kind == "plan_succeeded":
            return RouterDecision(
                action="EXECUTE",
                reasoning="Plan produced; first task ready to execute.",
            )
        return _escalate("PLAN", inp.last_event_kind)

    # EXECUTE rows
    if inp.current_state == "EXECUTE":
        if inp.last_event_kind == "execution_succeeded":
            return RouterDecision(
                action="VERIFY",
                reasoning="Execution succeeded; running verification commands.",
            )
        if inp.last_event_kind == "execution_failed":
            if inp.retry_caps_exhausted:
                return RouterDecision(
                    action="ESCALATE",
                    reasoning="Execution failed and retry caps exhausted.",
                )
            return RouterDecision(
                action="FIX_LOOP",
                reasoning="Execution failed; retry budget remains.",
            )
        return _escalate("EXECUTE", inp.last_event_kind)

    # VERIFY rows
    if inp.current_state == "VERIFY":
        if inp.last_event_kind == "tests_passed":
            return RouterDecision(
                action="NEXT_TASK",
                reasoning="Tests passed; advancing to next task.",
            )
        if inp.last_event_kind == "tests_critical":
            if inp.retry_caps_exhausted:
                return RouterDecision(
                    action="ESCALATE",
                    reasoning="Critical test failure with retry caps exhausted.",
                )
            return RouterDecision(
                action="FIX_LOOP",
                reasoning="Critical test failure; retry budget remains.",
            )
        if inp.last_event_kind == "tests_flaky":
            # Per the persona contract, the runtime re-runs the verifier.
            # Stage 6's Verifier already does its own re-run internally
            # and yields a final classification, so in practice this
            # branch is rarely hit — but we honor the table for the case
            # where an outer caller forwards a flaky outcome directly.
            return RouterDecision(
                action="VERIFY",
                reasoning="Flaky result reported; re-running verification.",
            )
        return _escalate("VERIFY", inp.last_event_kind)

    # NEXT_TASK rows
    if inp.current_state == "NEXT_TASK":
        if inp.last_event_kind == "more_tasks_remain":
            return RouterDecision(
                action="EXECUTE",
                reasoning="More tasks in plan; executing the next one.",
            )
        if inp.last_event_kind == "no_tasks_remain":
            return RouterDecision(
                action="DONE",
                reasoning="No more tasks; run complete.",
            )
        return _escalate("NEXT_TASK", inp.last_event_kind)

    # Defence in depth — Literal narrows current_state but a future
    # refactor that loosens it should still fail safe.
    return _escalate(inp.current_state, inp.last_event_kind)


def _escalate(current_state: str, last_event_kind: str) -> RouterDecision:
    """Build the canonical ESCALATE decision for unmatched rows.

    Kept in one place so the reasoning string stays consistent for
    grep-ability in the event log.
    """
    return RouterDecision(
        action="ESCALATE",
        reasoning=(
            f"No decision-table row matches state={current_state!r} "
            f"event={last_event_kind!r}; escalating per safe-fallback rule."
        ),
    )
