"""Unit tests for the deterministic Orchestrator router.

The router is a pure function (no I/O, no LLM). One test per row of the
decision table in `.forge/personas/orchestrator.md`, plus tests for the
safe-fallback path on unmatched inputs. The contract test
(`tests/test_router_contract.py`) parses the persona file itself and
exercises the table from MD; this file exercises it from the code side.
"""

from __future__ import annotations

from forge.router import RouterInput, decide_next_action


def _inp(
    *,
    state: str,
    last: str,
    caps_exhausted: bool = False,
    more_tasks: bool = True,
) -> RouterInput:
    return RouterInput(
        current_state=state,  # type: ignore[arg-type]
        last_event_kind=last,  # type: ignore[arg-type]
        retry_caps_exhausted=caps_exhausted,
        more_tasks=more_tasks,
    )


# ---------- PLAN ----------


def test_plan_succeeded_routes_to_execute() -> None:
    d = decide_next_action(_inp(state="PLAN", last="plan_succeeded"))
    assert d.action == "EXECUTE"


def test_plan_with_unexpected_event_escalates() -> None:
    d = decide_next_action(_inp(state="PLAN", last="execution_failed"))
    assert d.action == "ESCALATE"
    assert "PLAN" in d.reasoning


# ---------- EXECUTE ----------


def test_execute_success_routes_to_verify() -> None:
    d = decide_next_action(_inp(state="EXECUTE", last="execution_succeeded"))
    assert d.action == "VERIFY"


def test_execute_failed_with_budget_routes_to_fix_loop() -> None:
    d = decide_next_action(
        _inp(state="EXECUTE", last="execution_failed", caps_exhausted=False)
    )
    assert d.action == "FIX_LOOP"


def test_execute_failed_no_budget_escalates() -> None:
    d = decide_next_action(
        _inp(state="EXECUTE", last="execution_failed", caps_exhausted=True)
    )
    assert d.action == "ESCALATE"


def test_execute_with_unexpected_event_escalates() -> None:
    d = decide_next_action(_inp(state="EXECUTE", last="tests_passed"))
    assert d.action == "ESCALATE"


# ---------- VERIFY ----------


def test_verify_passed_routes_to_next_task() -> None:
    d = decide_next_action(_inp(state="VERIFY", last="tests_passed"))
    assert d.action == "NEXT_TASK"


def test_verify_critical_with_budget_routes_to_fix_loop() -> None:
    d = decide_next_action(
        _inp(state="VERIFY", last="tests_critical", caps_exhausted=False)
    )
    assert d.action == "FIX_LOOP"


def test_verify_critical_no_budget_escalates() -> None:
    d = decide_next_action(
        _inp(state="VERIFY", last="tests_critical", caps_exhausted=True)
    )
    assert d.action == "ESCALATE"


def test_verify_flaky_routes_to_verify_rerun() -> None:
    d = decide_next_action(_inp(state="VERIFY", last="tests_flaky"))
    assert d.action == "VERIFY"


def test_verify_with_unexpected_event_escalates() -> None:
    d = decide_next_action(_inp(state="VERIFY", last="plan_succeeded"))
    assert d.action == "ESCALATE"


# ---------- NEXT_TASK ----------


def test_next_task_with_more_tasks_routes_to_execute() -> None:
    d = decide_next_action(_inp(state="NEXT_TASK", last="more_tasks_remain"))
    assert d.action == "EXECUTE"


def test_next_task_no_tasks_routes_to_done() -> None:
    d = decide_next_action(
        _inp(state="NEXT_TASK", last="no_tasks_remain", more_tasks=False)
    )
    assert d.action == "DONE"


def test_next_task_with_unexpected_event_escalates() -> None:
    d = decide_next_action(_inp(state="NEXT_TASK", last="tests_passed"))
    assert d.action == "ESCALATE"


# ---------- Reasoning quality ----------


def test_reasoning_is_one_sentence_under_30_words() -> None:
    """Decision table says ≤30 words. Verify across every row's output."""
    cases = [
        _inp(state="PLAN", last="plan_succeeded"),
        _inp(state="EXECUTE", last="execution_succeeded"),
        _inp(state="EXECUTE", last="execution_failed", caps_exhausted=False),
        _inp(state="EXECUTE", last="execution_failed", caps_exhausted=True),
        _inp(state="VERIFY", last="tests_passed"),
        _inp(state="VERIFY", last="tests_critical", caps_exhausted=False),
        _inp(state="VERIFY", last="tests_critical", caps_exhausted=True),
        _inp(state="VERIFY", last="tests_flaky"),
        _inp(state="NEXT_TASK", last="more_tasks_remain"),
        _inp(state="NEXT_TASK", last="no_tasks_remain", more_tasks=False),
    ]
    for case in cases:
        d = decide_next_action(case)
        word_count = len(d.reasoning.split())
        assert word_count <= 30, (
            f"reasoning too long ({word_count} words) for "
            f"state={case.current_state} last={case.last_event_kind}: "
            f"{d.reasoning!r}"
        )


def test_safe_fallback_mentions_unmatched_state_and_event() -> None:
    """Bad-event ESCALATE reasoning must include both state and event
    so a grep over the event log identifies the offending pair."""
    d = decide_next_action(_inp(state="PLAN", last="tests_passed"))
    assert d.action == "ESCALATE"
    assert "PLAN" in d.reasoning
    assert "tests_passed" in d.reasoning
