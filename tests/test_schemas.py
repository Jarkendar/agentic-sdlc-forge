"""Schema round-trip and validation tests.

The contract is: anything we write to disk must come back identical when
read. Pydantic's mode='json' serialization handles Path/datetime/Enum;
these tests prove it for every persisted model.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from forge.schemas import (
    SCHEMA_VERSION,
    ExecutionResult,
    Failure,
    Plan,
    RunReport,
    RunState,
    RunStatus,
    Task,
    TestReport,
    VerificationCommand,
)

# ---------- Task ----------


def test_task_minimal_fields() -> None:
    task = Task(id="t1", goal="do thing")
    assert task.id == "t1"
    assert task.files == []
    assert task.acceptance_criteria == []
    assert task.depends_on == []


def test_task_full_round_trip() -> None:
    original = Task(
        id="t1",
        goal="add login endpoint",
        files=[Path("src/auth.py"), Path("tests/test_auth.py")],
        acceptance_criteria=["POST /login returns 200", "JWT in response"],
        depends_on=["t0"],
    )
    restored = Task.model_validate_json(original.model_dump_json())
    assert restored == original


def test_task_extra_fields_forbidden() -> None:
    # extra="forbid" protects us from typos like 'depends_one'
    with pytest.raises(ValidationError):
        Task.model_validate({"id": "t1", "goal": "g", "depends_one": ["t0"]})


# ---------- Plan ----------


def test_plan_round_trip() -> None:
    plan = Plan(
        run_id="20260101-120000-abcdef",
        user_story="user can log in",
        tasks=[
            Task(id="t1", goal="add endpoint"),
            Task(id="t2", goal="add tests", depends_on=["t1"]),
        ],
    )
    restored = Plan.model_validate_json(plan.model_dump_json())
    assert restored == plan
    assert restored.schema_version == SCHEMA_VERSION


def test_plan_default_timestamps_are_utc() -> None:
    plan = Plan(run_id="r1", user_story="x", tasks=[])
    assert plan.created_at.tzinfo is not None
    assert plan.created_at.tzinfo.utcoffset(plan.created_at).total_seconds() == 0


# ---------- ExecutionResult ----------


def test_execution_result_round_trip() -> None:
    result = ExecutionResult(
        task_id="t1",
        status="success",
        aider_stdout="edited 2 files",
        files_changed=[Path("src/auth.py")],
        duration_ms=4321,
    )
    assert ExecutionResult.model_validate_json(result.model_dump_json()) == result


def test_execution_result_rejects_invalid_status() -> None:
    with pytest.raises(ValidationError):
        ExecutionResult(task_id="t1", status="kinda_worked")  # type: ignore[arg-type]


# ---------- Failure & TestReport ----------


def test_failure_round_trip_with_hints() -> None:
    f = Failure(
        task_id="t1",
        stage="verify_test",
        command="pytest tests/",
        exit_code=1,
        stdout_excerpt="...assert 1 == 2",
        stderr_excerpt="",
        category="test",
        file_hint=Path("tests/test_auth.py"),
        line_hint=42,
        message="assertion failed in test_login",
    )
    assert Failure.model_validate_json(f.model_dump_json()) == f


def test_failure_optional_fields_default_to_none() -> None:
    f = Failure(
        stage="execute",
        command="aider --message ...",
        exit_code=2,
        category="unknown",
    )
    assert f.task_id is None
    assert f.file_hint is None
    assert f.message is None


def test_test_report_round_trip() -> None:
    report = TestReport(
        task_id="t1",
        passed=False,
        failures=[
            Failure(
                stage="verify_test",
                command="pytest",
                exit_code=1,
                category="test",
            )
        ],
        severity="critical",
    )
    assert TestReport.model_validate_json(report.model_dump_json()) == report


# ---------- VerificationCommand ----------


def test_verification_command_round_trip() -> None:
    cmd = VerificationCommand(
        name="pytest",
        command="pytest -q",
        stage="verify_test",
        timeout_seconds=300,
    )
    assert VerificationCommand.model_validate_json(cmd.model_dump_json()) == cmd


def test_verification_command_default_timeout() -> None:
    cmd = VerificationCommand(name="ruff", command="ruff check .", stage="verify_lint")
    assert cmd.timeout_seconds == 300


def test_verification_command_rejects_unknown_stage() -> None:
    with pytest.raises(ValidationError):
        VerificationCommand(
            name="x", command="y", stage="verify_smoke",  # type: ignore[arg-type]
        )


def test_verification_command_rejects_zero_timeout() -> None:
    with pytest.raises(ValidationError):
        VerificationCommand(name="x", command="y", stage="verify_test", timeout_seconds=0)


def test_verification_command_rejects_empty_strings() -> None:
    with pytest.raises(ValidationError):
        VerificationCommand(name="", command="y", stage="verify_test")
    with pytest.raises(ValidationError):
        VerificationCommand(name="x", command="", stage="verify_test")


# ---------- RunReport ----------


def _make_execution_result(task_id: str = "t1", status: str = "success") -> ExecutionResult:
    return ExecutionResult(task_id=task_id, status=status, files_changed=[Path("a.py")])


def _make_test_report(task_id: str = "t1", severity: str = "none") -> TestReport:
    if severity == "none":
        return TestReport(task_id=task_id, passed=True, failures=[], severity="none")
    return TestReport(
        task_id=task_id,
        passed=False,
        failures=[
            Failure(
                stage="verify_test",
                command="pytest",
                exit_code=1,
                category="test",
                message="boom",
            )
        ],
        severity=severity,  # type: ignore[arg-type]
    )


def test_run_report_success_round_trip() -> None:
    report = RunReport(
        task_id="t1",
        status="success",
        attempts=1,
        final_execution=_make_execution_result(),
        final_test_report=_make_test_report(),
    )
    assert RunReport.model_validate_json(report.model_dump_json()) == report


def test_run_report_escalated_with_critical_test_report() -> None:
    report = RunReport(
        task_id="t1",
        status="escalated",
        attempts=3,
        final_execution=_make_execution_result(),
        final_test_report=_make_test_report(severity="critical"),
        escalation_reason="max_retries_per_task=3 exhausted",
    )
    restored = RunReport.model_validate_json(report.model_dump_json())
    assert restored.status == "escalated"
    assert restored.attempts == 3
    assert restored.escalation_reason is not None
    assert restored.final_test_report is not None
    assert restored.final_test_report.severity == "critical"


def test_run_report_failed_allows_none_test_report() -> None:
    """status='failed' means the Verifier never ran — TestReport is None."""
    report = RunReport(
        task_id="t1",
        status="failed",
        attempts=1,
        final_execution=_make_execution_result(status="no_changes"),
        final_test_report=None,
        escalation_reason="executor returned no_changes",
    )
    assert RunReport.model_validate_json(report.model_dump_json()) == report


def test_run_report_attempts_must_be_at_least_one() -> None:
    with pytest.raises(ValidationError):
        RunReport(
            task_id="t1",
            status="success",
            attempts=0,
            final_execution=_make_execution_result(),
        )


def test_run_report_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        RunReport(
            task_id="t1",
            status="kinda",  # type: ignore[arg-type]
            attempts=1,
            final_execution=_make_execution_result(),
        )


# ---------- RunState ----------


def test_run_state_minimal() -> None:
    state = RunState(run_id="r1", user_story="x")
    assert state.status == RunStatus.PLANNING
    assert state.completed_task_ids == []
    assert state.retry_counts == {}
    assert state.total_retries == 0


def test_run_state_full_round_trip() -> None:
    state = RunState(
        run_id="20260101-120000-abcdef",
        user_story="add login",
        plan=Plan(
            run_id="20260101-120000-abcdef",
            user_story="add login",
            tasks=[Task(id="t1", goal="endpoint")],
        ),
        status=RunStatus.EXECUTING,
        current_task_id="t1",
        completed_task_ids=[],
        retry_counts={"t1": 1},
        total_retries=1,
        last_event_offset=4096,
    )
    restored = RunState.model_validate_json(state.model_dump_json())
    assert restored == state


def test_run_status_serializes_as_string() -> None:
    """Enum must round-trip as its string value, not as 'RunStatus.PLANNING'."""
    state = RunState(run_id="r1", user_story="x", status=RunStatus.EXECUTING)
    dumped = state.model_dump_json()
    assert '"status":"executing"' in dumped


def test_run_state_rejects_unknown_status() -> None:
    with pytest.raises(ValidationError):
        RunState(run_id="r1", user_story="x", status="cosmic_ray")  # type: ignore[arg-type]


def test_run_state_extra_fields_forbidden() -> None:
    with pytest.raises(ValidationError):
        RunState.model_validate({"run_id": "r1", "user_story": "x", "magic": True})


def test_run_state_paths_round_trip_through_plan() -> None:
    """Path objects nested deep in Plan.tasks[].files must survive JSON."""
    state = RunState(
        run_id="r1",
        user_story="x",
        plan=Plan(
            run_id="r1",
            user_story="x",
            tasks=[Task(id="t1", goal="g", files=[Path("a/b.py")])],
        ),
    )
    restored = RunState.model_validate_json(state.model_dump_json())
    assert restored.plan is not None
    assert restored.plan.tasks[0].files == [Path("a/b.py")]


def test_schema_version_is_current() -> None:
    state = RunState(run_id="r1", user_story="x")
    assert state.schema_version == SCHEMA_VERSION


def test_datetime_serialization_is_iso_with_tz() -> None:
    state = RunState(
        run_id="r1",
        user_story="x",
        created_at=datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC),
    )
    dumped = state.model_dump_json()
    # ISO with timezone — no naive datetimes ever leak to disk
    assert '"created_at":"2026-01-01T12:00:00Z"' in dumped or \
           '"created_at":"2026-01-01T12:00:00+00:00"' in dumped
