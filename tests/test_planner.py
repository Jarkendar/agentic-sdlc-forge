"""Tests for forge.agents.planner.

Uses a fake LLMClient to drive the Planner deterministically — we don't
hit a real provider in unit tests. The fake mirrors LLMClient's contract:
return an LLMResponse whose `content` is the schema instance the caller
asked for.

Coverage:
- Happy path: valid Plan returns, EventLog gets the right events.
- run_id mismatch raises PlannerError.
- Quality warnings are logged but do not raise.
- Persona with wrong output_schema raises ValueError.
- LLM returns non-Plan content -> defensive PlannerError.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from pydantic import BaseModel

from forge.agents.planner import (
    MAX_FILES_PER_TASK,
    PlannerError,
    run_planner,
)
from forge.event_log import EventLog
from forge.llm.base import LLMClient, LLMResponse
from forge.personas import Persona
from forge.schemas import Plan, Task

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeLLMClient(LLMClient):
    """Returns whatever LLMResponse the test injects.

    `complete()` is non-functional beyond that — we don't simulate retries
    or transport errors. Tests that need to test those should patch on the
    real provider's level, not here.
    """

    provider = "fake"

    def __init__(self, response: LLMResponse) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self._response


def _make_response(content: BaseModel | str, **overrides: object) -> LLMResponse:
    """Build an LLMResponse with sensible defaults; overridable per test."""
    defaults: dict[str, object] = {
        "content": content,
        "tokens_in": 100,
        "tokens_out": 200,
        "cost_usd": 0.001,
        "duration_ms": 500,
        "model": "fake-model",
        "provider": "fake",
        "finish_reason": "end_turn",
        "retried_validation": False,
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)  # type: ignore[arg-type]


def _make_persona(tmp_path: Path, output_schema: type[BaseModel] | None = Plan) -> Persona:
    """Build a minimal Persona that satisfies run_planner's interface.

    We don't load from disk — direct construction is faster and keeps the
    tests independent of frontmatter parsing changes.
    """
    body = (
        "User story: {{user_story}}\n"
        "Run ID: {{run_id}}\n"
        "Architecture: {{architecture_map}}\n"
        "Tree: {{file_tree}}\n"
    )
    return Persona(
        name="planner",
        output_schema=output_schema,
        required_vars=("user_story", "run_id", "architecture_map", "file_tree"),
        references=(),
        body=body,
        source_path=tmp_path / "planner.md",
    )


def _good_plan(run_id: str = "run-x") -> Plan:
    return Plan(
        run_id=run_id,
        user_story="Add a hello endpoint.",
        tasks=[
            Task(
                id="task-001",
                goal="Create the hello endpoint handler.",
                files=[Path("src/api/hello.py")],
                acceptance_criteria=["GET /hello returns 200."],
                depends_on=[],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_happy_path_returns_plan_and_logs_events(tmp_path: Path) -> None:
    log_file = tmp_path / "events.jsonl"
    persona = _make_persona(tmp_path)
    plan = _good_plan(run_id="run-x")
    llm = FakeLLMClient(_make_response(plan))

    with EventLog(log_file) as event_log:
        result = run_planner(
            user_story="Add a hello endpoint.",
            run_id="run-x",
            architecture_map="The system is a tiny FastAPI app.",
            repo_root=tmp_path,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    assert result is plan

    events = list(EventLog.read(log_file))
    phases = [e.phase for e in events]
    # We expect at least: start, llm_call_complete, validated.
    # No quality warnings on this plan (1 file, 1 acceptance criterion, no "and").
    assert phases == ["start", "llm_call_complete", "validated"]

    llm_event = events[1]
    assert llm_event.tokens_in == 100
    assert llm_event.tokens_out == 200
    assert llm_event.cost_usd == pytest.approx(0.001)
    assert llm_event.payload["task_count"] == 1
    assert llm_event.payload["model"] == "fake-model"


def test_persona_render_called_with_all_required_vars(tmp_path: Path) -> None:
    persona = _make_persona(tmp_path)
    plan = _good_plan()
    llm = FakeLLMClient(_make_response(plan))

    with EventLog(tmp_path / "events.jsonl") as event_log:
        run_planner(
            user_story="story",
            run_id=plan.run_id,
            architecture_map="arch",
            repo_root=tmp_path,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    assert len(llm.calls) == 1
    system_prompt = llm.calls[0]["system"]
    assert isinstance(system_prompt, str)
    assert "story" in system_prompt
    assert "arch" in system_prompt
    assert plan.run_id in system_prompt


def test_plan_is_returned_as_json_serializable(tmp_path: Path) -> None:
    """A returned Plan must round-trip through JSON — Stage 5 will persist it."""
    persona = _make_persona(tmp_path)
    plan = _good_plan()
    llm = FakeLLMClient(_make_response(plan))

    with EventLog(tmp_path / "events.jsonl") as event_log:
        result = run_planner(
            user_story="story",
            run_id=plan.run_id,
            architecture_map="arch",
            repo_root=tmp_path,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    parsed = json.loads(result.model_dump_json())
    assert parsed["run_id"] == plan.run_id


# ---------------------------------------------------------------------------
# Hard contract checks
# ---------------------------------------------------------------------------


def test_run_id_mismatch_raises(tmp_path: Path) -> None:
    persona = _make_persona(tmp_path)
    plan = _good_plan(run_id="WRONG-id")
    llm = FakeLLMClient(_make_response(plan))

    with EventLog(tmp_path / "events.jsonl") as event_log, pytest.raises(
        PlannerError, match="run_id mismatch"
    ):
        run_planner(
            user_story="story",
            run_id="run-x",
            architecture_map="arch",
            repo_root=tmp_path,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )


def test_persona_with_wrong_output_schema_raises(tmp_path: Path) -> None:
    """Misconfigured persona (output_schema != Plan) must fail before LLM call."""
    persona = _make_persona(tmp_path, output_schema=Task)  # not Plan
    llm = FakeLLMClient(_make_response(_good_plan()))

    with EventLog(tmp_path / "events.jsonl") as event_log, pytest.raises(
        ValueError, match="output_schema=Plan"
    ):
        run_planner(
            user_story="story",
            run_id="run-x",
            architecture_map="arch",
            repo_root=tmp_path,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    # LLM was never invoked — fail fast before paid call
    assert llm.calls == []


def test_llm_returns_non_plan_content_raises(tmp_path: Path) -> None:
    """Defensive check: if a provider regression returns text instead of Plan."""
    persona = _make_persona(tmp_path)
    bad_response = _make_response("just a string, not a Plan")  # type: ignore[arg-type]
    llm = FakeLLMClient(bad_response)

    with EventLog(tmp_path / "events.jsonl") as event_log, pytest.raises(
        PlannerError, match="expected Plan"
    ):
        run_planner(
            user_story="story",
            run_id="run-x",
            architecture_map="arch",
            repo_root=tmp_path,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )


# ---------------------------------------------------------------------------
# Quality warnings — logged, not raised
# ---------------------------------------------------------------------------


def test_too_many_files_logs_warning(tmp_path: Path) -> None:
    log_file = tmp_path / "events.jsonl"
    persona = _make_persona(tmp_path)
    plan = Plan(
        run_id="run-x",
        user_story="story",
        tasks=[
            Task(
                id="task-001",
                goal="Touches lots of files.",
                files=[Path(f"src/f{i}.py") for i in range(MAX_FILES_PER_TASK + 2)],
                acceptance_criteria=["check it"],
            )
        ],
    )
    llm = FakeLLMClient(_make_response(plan))

    with EventLog(log_file) as event_log:
        result = run_planner(
            user_story="story",
            run_id="run-x",
            architecture_map="arch",
            repo_root=tmp_path,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    # Plan still returned despite warning
    assert result is plan

    events = list(EventLog.read(log_file))
    warning_events = [e for e in events if e.phase == "quality_warning"]
    assert len(warning_events) == 1
    assert warning_events[0].payload["kind"] == "too_many_files"
    assert warning_events[0].payload["task_id"] == "task-001"


def test_no_acceptance_criteria_logs_warning(tmp_path: Path) -> None:
    log_file = tmp_path / "events.jsonl"
    persona = _make_persona(tmp_path)
    plan = Plan(
        run_id="run-x",
        user_story="story",
        tasks=[
            Task(
                id="task-001",
                goal="Do thing.",
                files=[Path("a.py")],
                acceptance_criteria=[],  # forbidden by spirit, allowed by schema
            )
        ],
    )
    llm = FakeLLMClient(_make_response(plan))

    with EventLog(log_file) as event_log:
        run_planner(
            user_story="story",
            run_id="run-x",
            architecture_map="arch",
            repo_root=tmp_path,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    events = list(EventLog.read(log_file))
    warning_kinds = [e.payload.get("kind") for e in events if e.phase == "quality_warning"]
    assert "no_acceptance_criteria" in warning_kinds


def test_compound_goal_logs_warning(tmp_path: Path) -> None:
    """Goals containing 'and' or 'also' as standalone words flag as compound."""
    log_file = tmp_path / "events.jsonl"
    persona = _make_persona(tmp_path)
    plan = Plan(
        run_id="run-x",
        user_story="story",
        tasks=[
            Task(
                id="task-001",
                goal="Add the parser and update the docs.",
                files=[Path("a.py")],
                acceptance_criteria=["check it"],
            )
        ],
    )
    llm = FakeLLMClient(_make_response(plan))

    with EventLog(log_file) as event_log:
        run_planner(
            user_story="story",
            run_id="run-x",
            architecture_map="arch",
            repo_root=tmp_path,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    events = list(EventLog.read(log_file))
    warning_events = [e for e in events if e.phase == "quality_warning"]
    assert any(e.payload.get("kind") == "compound_goal" for e in warning_events)


def test_compound_goal_does_not_false_positive_on_substrings(tmp_path: Path) -> None:
    """Words like 'android' or 'land' contain 'and' but aren't compound."""
    log_file = tmp_path / "events.jsonl"
    persona = _make_persona(tmp_path)
    plan = Plan(
        run_id="run-x",
        user_story="story",
        tasks=[
            Task(
                id="task-001",
                goal="Build the android client.",
                files=[Path("a.py")],
                acceptance_criteria=["check it"],
            )
        ],
    )
    llm = FakeLLMClient(_make_response(plan))

    with EventLog(log_file) as event_log:
        run_planner(
            user_story="story",
            run_id="run-x",
            architecture_map="arch",
            repo_root=tmp_path,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    events = list(EventLog.read(log_file))
    compound_warnings = [
        e for e in events
        if e.phase == "quality_warning" and e.payload.get("kind") == "compound_goal"
    ]
    assert compound_warnings == []
