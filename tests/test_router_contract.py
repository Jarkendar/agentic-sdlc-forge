"""Contract test: deterministic router MUST agree with orchestrator.md.

Per Stage 7's D1 decision, `.forge/personas/orchestrator.md` is the
canonical description of the state machine; `forge.router` is the
executable form. This test parses the markdown decision table and
verifies that for every row the router returns the same action.

Drift between MD and code is exactly what this test exists to catch.
If you change one without the other, this fails — fix both.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from forge.router import RouterInput, decide_next_action
from forge.schemas import OrchestratorAction

ORCHESTRATOR_MD = (
    Path(__file__).parent.parent / ".forge" / "personas" / "orchestrator.md"
)


# ---------------------------------------------------------------------------
# Mapping: MD column values → router input fields
# ---------------------------------------------------------------------------
#
# We deliberately keep these maps short and explicit. If a future row adds
# a new state or event word, the test fails with a KeyError pointing at the
# missing entry — easier to find than a silent fallthrough.

_STATE_MAP: dict[str, str] = {
    "PLAN": "PLAN",
    "EXECUTE": "EXECUTE",
    "VERIFY": "VERIFY",
    "NEXT_TASK": "NEXT_TASK",
}

_EVENT_MAP: dict[str, str] = {
    "plan succeeded": "plan_succeeded",
    "execution succeeded": "execution_succeeded",
    "execution failed": "execution_failed",
    "tests passed": "tests_passed",
    "tests failed (critical)": "tests_critical",
    "tests flaky": "tests_flaky",
    "more tasks remain": "more_tasks_remain",
    "no tasks remain": "no_tasks_remain",
}

# The "Decision" column in the MD sometimes carries qualifiers like
# "VERIFY (re-run)" — strip them for comparison against the Literal type.
_DECISION_RE = re.compile(r"^([A-Z_]+)")


def _parse_decision_table() -> list[tuple[str, str, str, OrchestratorAction]]:
    """Yield (current_state, event_kind, caps_marker, decision) tuples
    from the markdown table.

    `caps_marker` is the raw string in the "Retry caps OK" column:
    "yes", "no", or "—". Caller maps it onto `retry_caps_exhausted`.
    """
    text = ORCHESTRATOR_MD.read_text(encoding="utf-8")

    # Find the markdown table under "# Decision table". We anchor on the
    # header row to avoid picking up other tables (the file currently has
    # only one, but defensive coding here costs nothing).
    table_match = re.search(
        r"# Decision table.*?\n"
        r"\| Current state \| Last event \| Retry caps OK \| Decision \|\n"
        r"\|[-|: ]+\|\n"
        r"((?:\|.+\|\n)+)",
        text,
        re.DOTALL,
    )
    if table_match is None:
        raise AssertionError(
            "Could not locate the decision table under '# Decision table' "
            "in orchestrator.md. Check the file structure."
        )

    rows: list[tuple[str, str, str, OrchestratorAction]] = []
    for line in table_match.group(1).strip().splitlines():
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        assert len(cells) == 4, f"Malformed row in decision table: {line!r}"

        state_md, event_md, caps_md, decision_md = cells

        # Look up state — MUST match an entry in _STATE_MAP.
        if state_md not in _STATE_MAP:
            raise AssertionError(
                f"orchestrator.md decision table mentions state {state_md!r} "
                f"which is not in test mapping. Update _STATE_MAP."
            )

        # Normalize event for lookup — lowercase + strip enum value parens
        event_key = event_md.lower()
        if event_key not in _EVENT_MAP:
            raise AssertionError(
                f"orchestrator.md decision table mentions event {event_md!r} "
                f"which is not in test mapping. Update _EVENT_MAP."
            )

        decision_match = _DECISION_RE.match(decision_md)
        if decision_match is None:
            raise AssertionError(
                f"Could not parse decision {decision_md!r}"
            )
        decision = decision_match.group(1)

        rows.append(
            (
                _STATE_MAP[state_md],
                _EVENT_MAP[event_key],
                caps_md,
                decision,  # type: ignore[arg-type]
            )
        )

    assert rows, "Decision table is empty — parser bug or persona drift"
    return rows


def _caps_variants(caps_marker: str) -> list[bool]:
    """For caps_marker == '—' the decision is independent of caps —
    test both True and False to prove it. For 'yes'/'no' fix the value.

    Returns the list of `retry_caps_exhausted` bools to feed the router.
    """
    if caps_marker == "—":
        return [False, True]
    if caps_marker == "yes":
        return [False]  # "caps OK = yes" means caps NOT exhausted
    if caps_marker == "no":
        return [True]  # "caps OK = no" means caps exhausted
    raise AssertionError(f"Unknown caps marker: {caps_marker!r}")


@pytest.mark.parametrize("row", _parse_decision_table())
def test_router_matches_persona_decision_table(
    row: tuple[str, str, str, OrchestratorAction],
) -> None:
    """For every row in orchestrator.md's decision table, the router
    returns the same action."""
    state, event, caps_marker, expected_decision = row

    for caps_exhausted in _caps_variants(caps_marker):
        # For NEXT_TASK, the "more_tasks_remain" / "no_tasks_remain"
        # event itself encodes the boolean — keep more_tasks in sync.
        more_tasks = event != "no_tasks_remain"

        inp = RouterInput(
            current_state=state,  # type: ignore[arg-type]
            last_event_kind=event,  # type: ignore[arg-type]
            retry_caps_exhausted=caps_exhausted,
            more_tasks=more_tasks,
        )
        actual = decide_next_action(inp)
        assert actual.action == expected_decision, (
            f"Router/persona drift: state={state} event={event} "
            f"caps_exhausted={caps_exhausted} → expected {expected_decision}, "
            f"got {actual.action} ({actual.reasoning!r})"
        )


def test_decision_table_exists_in_md() -> None:
    """Defensive: bare-minimum sanity that the parser found something.

    Without this, a typo in orchestrator.md's table headers would make
    `_parse_decision_table` raise on collection and obscure the cause.
    """
    rows = _parse_decision_table()
    # 10 rows in the table as of Stage 7; this number changes legitimately
    # if rows are added/removed, so we assert "at least 8" instead of "==10"
    # to avoid making the test brittle.
    assert len(rows) >= 8, f"Decision table has too few rows: {len(rows)}"
