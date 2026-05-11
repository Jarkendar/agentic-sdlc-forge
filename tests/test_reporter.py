"""Reporter agent tests.

Coverage:
- Happy path: events + cost aggregation → LLM call → RUN_REPORT.md written
- Reporter persona must have output_schema=None
- Missing events file raises ReporterError
- Cost summary correctly aggregates multi-agent costs and ignores
  non-LLM events (where tokens_in/out/cost are all None)
- LLM returning a non-string is caught with a useful error
- Truncation kicks in for very large event logs
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import BaseModel

from forge.agents.reporter import ReporterError, run_reporter
from forge.event_log import EventLog
from forge.llm.base import LLMClient, LLMResponse
from forge.personas import Persona
from forge.schemas import Plan, Task


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class FakeLLMClient(LLMClient):
    """Captures the call args and returns whatever `response` we wired."""

    provider = "fake"

    def __init__(self, response: LLMResponse) -> None:
        self.response = response
        self.calls: list[dict[str, object]] = []

    def complete(
        self,
        *,
        system: str,
        user: str,
        schema: type[BaseModel] | None = None,
    ) -> LLMResponse:
        self.calls.append({"system": system, "user": user, "schema": schema})
        return self.response


def _make_response(content: str | BaseModel, **overrides: object) -> LLMResponse:
    defaults: dict[str, object] = {
        "content": content,
        "tokens_in": 100,
        "tokens_out": 200,
        "cost_usd": 0.003,
        "duration_ms": 500,
        "model": "fake-model",
        "provider": "fake",
        "finish_reason": "end_turn",
        "retried_validation": False,
    }
    defaults.update(overrides)
    return LLMResponse(**defaults)  # type: ignore[arg-type]


def _reporter_persona(tmp_path: Path, output_schema: type[BaseModel] | None = None) -> Persona:
    """Build a minimal Reporter persona with the expected required_vars
    interpolated into the body. Reporter persona uniquely has output_schema=None."""
    body = (
        "Reporter for run {{run_id}}\n"
        "Story: {{user_story}}\n"
        "Events:\n{{events_jsonl}}\n"
        "Cost:\n{{cost_summary}}\n"
    )
    return Persona(
        name="reporter",
        output_schema=output_schema,
        required_vars=("run_id", "user_story", "events_jsonl", "cost_summary"),
        references=(),
        body=body,
        source_path=tmp_path / "reporter.md",
    )


def _seed_events(forge_root: Path, run_id: str, events: list[dict[str, object]]) -> Path:
    """Write a series of events to .forge/runs/<run_id>/events.jsonl."""
    log_path = forge_root / "runs" / run_id / "events.jsonl"
    with EventLog(log_path) as log:
        for ev in events:
            log.log(
                agent=str(ev["agent"]),
                phase=str(ev["phase"]),
                run_id=run_id,
                payload=ev.get("payload"),  # type: ignore[arg-type]
                tokens_in=ev.get("tokens_in"),  # type: ignore[arg-type]
                tokens_out=ev.get("tokens_out"),  # type: ignore[arg-type]
                cost_usd=ev.get("cost_usd"),  # type: ignore[arg-type]
            )
    return log_path


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_run_reporter_writes_run_report_md(tmp_path: Path) -> None:
    forge_root = tmp_path / ".forge"
    run_id = "20260101-120000-abcdef"
    _seed_events(
        forge_root,
        run_id,
        [
            {"agent": "planner", "phase": "start"},
            {
                "agent": "planner",
                "phase": "llm_call_complete",
                "tokens_in": 1000,
                "tokens_out": 500,
                "cost_usd": 0.015,
            },
        ],
    )

    persona = _reporter_persona(tmp_path)
    llm = FakeLLMClient(_make_response("# Run Report\n\nAll good.\n"))

    log_path = forge_root / "runs" / run_id / "events.jsonl"
    with EventLog(log_path) as event_log:
        out = run_reporter(
            run_id=run_id,
            user_story="add login",
            forge_root=forge_root,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    assert out == forge_root / "runs" / run_id / "RUN_REPORT.md"
    assert out.read_text() == "# Run Report\n\nAll good.\n"


def test_run_reporter_passes_pre_aggregated_cost_summary(tmp_path: Path) -> None:
    """Reporter persona contract demands a pre-built cost table. Verify
    it lands in the system prompt, with per-agent rows and a Total row."""
    forge_root = tmp_path / ".forge"
    run_id = "r1"
    _seed_events(
        forge_root,
        run_id,
        [
            {
                "agent": "planner",
                "phase": "x",
                "tokens_in": 100,
                "tokens_out": 200,
                "cost_usd": 0.01,
            },
            {
                "agent": "verifier",
                "phase": "y",
                "tokens_in": 50,
                "tokens_out": 80,
                "cost_usd": 0.005,
            },
            {
                "agent": "verifier",
                "phase": "y",
                "tokens_in": 60,
                "tokens_out": 90,
                "cost_usd": 0.007,
            },
            # non-LLM event — must be ignored
            {"agent": "executor", "phase": "start"},
        ],
    )

    persona = _reporter_persona(tmp_path)
    llm = FakeLLMClient(_make_response("ok"))
    log_path = forge_root / "runs" / run_id / "events.jsonl"
    with EventLog(log_path) as event_log:
        run_reporter(
            run_id=run_id,
            user_story="x",
            forge_root=forge_root,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    # The system prompt has the cost_summary embedded — fish it out.
    system = str(llm.calls[0]["system"])
    assert "| planner | 100 | 200 | 0.0100 |" in system
    # Verifier rows summed (50+60, 80+90, 0.005+0.007)
    assert "| verifier | 110 | 170 | 0.0120 |" in system
    # Executor with all-None must NOT appear in the cost table
    assert "| executor |" not in system
    # Total row
    assert "| **Total** | 210 | 370 | 0.0220 |" in system


def test_run_reporter_logs_its_own_events(tmp_path: Path) -> None:
    forge_root = tmp_path / ".forge"
    run_id = "r1"
    _seed_events(forge_root, run_id, [{"agent": "planner", "phase": "start"}])
    persona = _reporter_persona(tmp_path)
    llm = FakeLLMClient(
        _make_response(
            "report",
            tokens_in=42,
            tokens_out=99,
            cost_usd=0.0001,
        )
    )

    log_path = forge_root / "runs" / run_id / "events.jsonl"
    with EventLog(log_path) as event_log:
        run_reporter(
            run_id=run_id,
            user_story="x",
            forge_root=forge_root,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    phases = [e.phase for e in EventLog.read(log_path) if e.agent == "reporter"]
    assert "start" in phases
    assert "llm_call_complete" in phases
    assert "report_written" in phases


# ---------------------------------------------------------------------------
# Cost edge cases
# ---------------------------------------------------------------------------


def test_run_reporter_with_no_llm_events_shows_zero_cost_table(tmp_path: Path) -> None:
    """All deterministic agents (executor, orchestrator) → no LLM costs.
    The cost table must still be present, with a 'no LLM calls' marker."""
    forge_root = tmp_path / ".forge"
    run_id = "r1"
    _seed_events(
        forge_root,
        run_id,
        [
            {"agent": "executor", "phase": "start"},
            {"agent": "executor", "phase": "end"},
        ],
    )

    persona = _reporter_persona(tmp_path)
    llm = FakeLLMClient(_make_response("ok"))
    log_path = forge_root / "runs" / run_id / "events.jsonl"
    with EventLog(log_path) as event_log:
        run_reporter(
            run_id=run_id,
            user_story="x",
            forge_root=forge_root,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    system = str(llm.calls[0]["system"])
    assert "(no LLM calls recorded)" in system
    assert "| **Total** | 0 | 0 | 0.0000 |" in system


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_run_reporter_rejects_persona_with_output_schema(tmp_path: Path) -> None:
    """Reporter is the only persona where output_schema must be None
    (it produces free-text markdown)."""
    forge_root = tmp_path / ".forge"
    run_id = "r1"
    _seed_events(forge_root, run_id, [{"agent": "planner", "phase": "start"}])

    persona = _reporter_persona(tmp_path, output_schema=Plan)
    llm = FakeLLMClient(_make_response("ok"))
    log_path = forge_root / "runs" / run_id / "events.jsonl"
    with EventLog(log_path) as event_log:
        with pytest.raises(ReporterError, match="output_schema=null"):
            run_reporter(
                run_id=run_id,
                user_story="x",
                forge_root=forge_root,
                persona=persona,
                llm=llm,
                event_log=event_log,
            )
    # LLM never called
    assert llm.calls == []


def test_run_reporter_missing_events_file_raises(tmp_path: Path) -> None:
    forge_root = tmp_path / ".forge"
    run_id = "ghost"
    persona = _reporter_persona(tmp_path)
    llm = FakeLLMClient(_make_response("ok"))

    # Open an unrelated event log just to satisfy the signature.
    log_path = tmp_path / "elsewhere.jsonl"
    with EventLog(log_path) as event_log:
        with pytest.raises(ReporterError, match="Events file not found"):
            run_reporter(
                run_id=run_id,
                user_story="x",
                forge_root=forge_root,
                persona=persona,
                llm=llm,
                event_log=event_log,
            )


def test_run_reporter_rejects_non_string_content(tmp_path: Path) -> None:
    """If the LLM ever returns a BaseModel content (provider regression),
    fail loudly instead of crashing on write_text."""
    forge_root = tmp_path / ".forge"
    run_id = "r1"
    _seed_events(forge_root, run_id, [{"agent": "planner", "phase": "start"}])

    persona = _reporter_persona(tmp_path)
    # Inject a fake Task as content — would happen if a provider client
    # auto-validated the response against some schema even when we passed
    # schema=None.
    bad_content = Task(id="t1", goal="x")
    llm = FakeLLMClient(_make_response(bad_content))

    log_path = forge_root / "runs" / run_id / "events.jsonl"
    with EventLog(log_path) as event_log:
        with pytest.raises(ReporterError, match="non-string content"):
            run_reporter(
                run_id=run_id,
                user_story="x",
                forge_root=forge_root,
                persona=persona,
                llm=llm,
                event_log=event_log,
            )


# ---------------------------------------------------------------------------
# Truncation
# ---------------------------------------------------------------------------


def test_run_reporter_truncates_huge_event_log(tmp_path: Path) -> None:
    """When events.jsonl exceeds the truncation threshold, the prompt
    payload keeps head + tail with an ELIDED marker. We don't want a
    huge log to blow the prompt's token budget."""
    from forge.agents.reporter import _MAX_EVENTS_PAYLOAD_BYTES

    forge_root = tmp_path / ".forge"
    run_id = "huge"
    log_path = forge_root / "runs" / run_id / "events.jsonl"
    log_path.parent.mkdir(parents=True)

    # Hand-write the file so we hit the threshold without paying for
    # thousands of EventLog.log() fsyncs.
    with log_path.open("wb") as fh:
        line = (
            b'{"timestamp":"2026-01-01T00:00:00Z","run_id":"huge",'
            b'"agent":"x","phase":"y","payload":{"junk":"' + b"A" * 1000 + b'"}}\n'
        )
        target_bytes = _MAX_EVENTS_PAYLOAD_BYTES + 50_000
        n = target_bytes // len(line) + 1
        for _ in range(n):
            fh.write(line)

    persona = _reporter_persona(tmp_path)
    llm = FakeLLMClient(_make_response("# report"))
    with EventLog(log_path) as event_log:
        run_reporter(
            run_id=run_id,
            user_story="x",
            forge_root=forge_root,
            persona=persona,
            llm=llm,
            event_log=event_log,
        )

    system = str(llm.calls[0]["system"])
    assert "[ELIDED:" in system
    # Output must not be larger than the input — truncation actually shrunk it
    assert len(system.encode("utf-8")) < log_path.stat().st_size + len(persona.body)
