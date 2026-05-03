"""RunState persistence tests — atomic save, schema-version check, resume cycle."""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

import pytest

from forge.schemas import Plan, RunState, RunStatus, Task
from forge.state import (
    events_path,
    generate_run_id,
    load_state,
    run_dir,
    save_state,
    state_path,
)

# ---------- run_id generation ----------


def test_generate_run_id_format() -> None:
    rid = generate_run_id()
    # YYYYMMDD-HHMMSS-<6 hex>
    assert re.match(r"^\d{8}-\d{6}-[0-9a-f]{6}$", rid)


def test_generate_run_id_is_unique() -> None:
    """Two calls in the same second must not collide thanks to the hex suffix."""
    ids = {generate_run_id() for _ in range(100)}
    assert len(ids) == 100


def test_generate_run_id_uses_provided_time() -> None:
    fixed = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
    rid = generate_run_id(fixed)
    assert rid.startswith("20260102-030405-")


# ---------- Path helpers ----------


def test_run_dir_layout(tmp_path: Path) -> None:
    forge_root = tmp_path / ".forge"
    rid = "20260101-120000-abcdef"
    assert run_dir(forge_root, rid) == forge_root / "runs" / rid
    assert state_path(forge_root, rid) == forge_root / "runs" / rid / "state.json"
    assert events_path(forge_root, rid) == forge_root / "runs" / rid / "events.jsonl"


# ---------- save / load round trip ----------


def test_save_and_load_round_trip(tmp_path: Path) -> None:
    state = RunState(
        run_id="20260101-120000-abcdef",
        user_story="add login",
        status=RunStatus.EXECUTING,
        current_task_id="t1",
        retry_counts={"t1": 1},
    )
    save_state(state, tmp_path)
    restored = load_state(state.run_id, tmp_path)
    # updated_at gets bumped by save_state, so compare by dump excluding it
    assert restored.run_id == state.run_id
    assert restored.status == RunStatus.EXECUTING
    assert restored.current_task_id == "t1"
    assert restored.retry_counts == {"t1": 1}


def test_save_with_full_plan_round_trip(tmp_path: Path) -> None:
    """The whole plan lives in state.json so resume needs no replan."""
    plan = Plan(
        run_id="r1",
        user_story="x",
        tasks=[
            Task(id="t1", goal="g1", files=[Path("a.py")]),
            Task(id="t2", goal="g2", depends_on=["t1"]),
        ],
    )
    state = RunState(run_id="r1", user_story="x", plan=plan)
    save_state(state, tmp_path)
    restored = load_state("r1", tmp_path)
    assert restored.plan == plan


def test_save_creates_run_directory(tmp_path: Path) -> None:
    state = RunState(run_id="20260101-120000-abcdef", user_story="x")
    save_state(state, tmp_path)
    assert run_dir(tmp_path, state.run_id).is_dir()


def test_save_updates_updated_at(tmp_path: Path) -> None:
    state = RunState(run_id="r1", user_story="x")
    original = state.updated_at
    # Force a measurable gap
    state.updated_at = datetime(2020, 1, 1, tzinfo=UTC)
    save_state(state, tmp_path)
    assert state.updated_at > datetime(2020, 1, 1, tzinfo=UTC)
    assert state.updated_at != original  # actually got bumped


# ---------- Atomicity ----------


def test_save_does_not_leave_temp_files(tmp_path: Path) -> None:
    state = RunState(run_id="r1", user_story="x")
    save_state(state, tmp_path)
    target_dir = run_dir(tmp_path, "r1")
    leftover = [p.name for p in target_dir.iterdir() if p.name.startswith(".state-")]
    assert leftover == [], f"temp files left behind: {leftover}"


def test_save_overwrites_existing_state(tmp_path: Path) -> None:
    """Subsequent saves replace the file atomically — no append, no merge."""
    state = RunState(run_id="r1", user_story="x", status=RunStatus.PLANNING)
    save_state(state, tmp_path)

    state.status = RunStatus.DONE
    state.completed_task_ids = ["t1", "t2"]
    save_state(state, tmp_path)

    restored = load_state("r1", tmp_path)
    assert restored.status == RunStatus.DONE
    assert restored.completed_task_ids == ["t1", "t2"]


# ---------- Resume scenario ----------


def test_save_kill_reload_resume_cycle(tmp_path: Path) -> None:
    """Simulates: run starts, partially completes, process dies, resume.

    This is the contract the orchestrator depends on in Stage 7.
    """
    rid = generate_run_id()
    plan = Plan(
        run_id=rid,
        user_story="x",
        tasks=[Task(id="t1", goal="g1"), Task(id="t2", goal="g2"), Task(id="t3", goal="g3")],
    )
    state = RunState(
        run_id=rid,
        user_story="x",
        plan=plan,
        status=RunStatus.EXECUTING,
        current_task_id="t2",
        completed_task_ids=["t1"],
        retry_counts={"t2": 1},
        total_retries=1,
        last_event_offset=2048,
    )
    save_state(state, tmp_path)

    # ... process dies here (no cleanup, no graceful shutdown) ...

    # Resume
    resumed = load_state(rid, tmp_path)
    assert resumed.current_task_id == "t2"
    assert resumed.completed_task_ids == ["t1"]
    assert resumed.retry_counts == {"t2": 1}
    assert resumed.total_retries == 1
    assert resumed.last_event_offset == 2048
    assert resumed.plan is not None
    assert [t.id for t in resumed.plan.tasks] == ["t1", "t2", "t3"]


# ---------- Schema versioning ----------


def test_load_rejects_mismatched_schema_version(tmp_path: Path) -> None:
    """Loading a state.json with the wrong schema_version must fail loudly."""
    state = RunState(run_id="r1", user_story="x")
    save_state(state, tmp_path)

    # Tamper with the persisted version
    target = state_path(tmp_path, "r1")
    raw = target.read_text(encoding="utf-8")
    tampered = raw.replace('"schema_version": "1"', '"schema_version": "99"')
    assert tampered != raw, "tamper sentinel did not match — JSON format may have changed"
    target.write_text(tampered, encoding="utf-8")

    with pytest.raises(ValueError, match="Schema version mismatch"):
        load_state("r1", tmp_path)


def test_load_missing_state_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_state("does-not-exist", tmp_path)
