"""EventLog tests — round-trip, fsync behavior, crash recovery.

The crash-recovery test is the most important one. It physically kills a
subprocess mid-write and asserts that all events flushed before the kill
are still readable. If this test breaks, the log is not crash-safe and
silent data loss is on the table.
"""

from __future__ import annotations

import multiprocessing as mp
import os
import signal
import sys
import time
from pathlib import Path

import pytest

from forge.event_log import Event, EventLog
from forge.schemas import Failure

# ---------- Basic write / read ----------


def test_log_writes_one_line_per_event(tmp_path: Path) -> None:
    log_file = tmp_path / "events.jsonl"
    with EventLog(log_file) as log:
        log.log(agent="planner", phase="start", run_id="r1")
        log.log(agent="planner", phase="end", run_id="r1", tokens_in=100, tokens_out=200)

    contents = log_file.read_bytes()
    assert contents.count(b"\n") == 2  # exactly one newline per event


def test_log_round_trip(tmp_path: Path) -> None:
    log_file = tmp_path / "events.jsonl"
    with EventLog(log_file) as log:
        log.log(agent="planner", phase="start", run_id="r1")
        log.log(
            agent="executor",
            phase="end",
            run_id="r1",
            payload={"task_id": "t1", "status": "ok"},
            tokens_in=50,
            tokens_out=300,
            duration_ms=1500,
        )

    events = list(EventLog.read(log_file))
    assert len(events) == 2
    assert events[0].agent == "planner"
    assert events[0].phase == "start"
    assert events[1].payload == {"task_id": "t1", "status": "ok"}
    assert events[1].tokens_in == 50
    assert events[1].duration_ms == 1500


def test_log_accepts_pydantic_model_as_payload(tmp_path: Path) -> None:
    """Passing a BaseModel must dump to JSON-safe dict, including Path fields."""
    failure = Failure(
        task_id="t1",
        stage="verify_test",
        command="pytest",
        exit_code=1,
        category="test",
        file_hint=Path("tests/test_x.py"),
        line_hint=10,
    )
    log_file = tmp_path / "events.jsonl"
    with EventLog(log_file) as log:
        log.log(agent="verifier", phase="failure", run_id="r1", payload=failure)

    [event] = list(EventLog.read(log_file))
    # Path serialized as string
    assert event.payload["file_hint"] == "tests/test_x.py"
    assert event.payload["line_hint"] == 10


def test_log_appends_does_not_overwrite(tmp_path: Path) -> None:
    """Re-opening an existing log file must append, never truncate."""
    log_file = tmp_path / "events.jsonl"
    with EventLog(log_file) as log:
        log.log(agent="a", phase="p", run_id="r1")
    with EventLog(log_file) as log:
        log.log(agent="b", phase="p", run_id="r1")

    events = list(EventLog.read(log_file))
    assert [e.agent for e in events] == ["a", "b"]


def test_read_missing_file_yields_nothing(tmp_path: Path) -> None:
    """Reading a non-existent log must not crash — fresh runs have no log yet."""
    events = list(EventLog.read(tmp_path / "does_not_exist.jsonl"))
    assert events == []


def test_read_empty_file_yields_nothing(tmp_path: Path) -> None:
    log_file = tmp_path / "events.jsonl"
    log_file.touch()
    assert list(EventLog.read(log_file)) == []


# ---------- Corruption tolerance ----------


def test_read_skips_malformed_line_with_warning(tmp_path: Path) -> None:
    """A corrupt line in the middle must not stop reading — yield around it.

    In practice corruption only happens on the last line (a crash mid-write),
    but we test a middle corruption to prove the reader doesn't bail early.
    """
    log_file = tmp_path / "events.jsonl"
    with EventLog(log_file) as log:
        log.log(agent="a", phase="p", run_id="r1")
    # Manually inject a broken line
    with log_file.open("ab") as fh:
        fh.write(b"this is not json\n")
    with EventLog(log_file) as log:
        log.log(agent="b", phase="p", run_id="r1")

    with pytest.warns(UserWarning, match="malformed event"):
        events = list(EventLog.read(log_file))
    assert [e.agent for e in events] == ["a", "b"]


def test_read_tolerates_truncated_last_line(tmp_path: Path) -> None:
    """A crash mid-write may leave a half-written last line. Earlier lines must survive."""
    log_file = tmp_path / "events.jsonl"
    with EventLog(log_file) as log:
        log.log(agent="a", phase="p", run_id="r1")
        log.log(agent="b", phase="p", run_id="r1")
    # Simulate truncation by appending a partial JSON without newline
    with log_file.open("ab") as fh:
        fh.write(b'{"timestamp":"2026')  # no closing brace, no newline

    with pytest.warns(UserWarning):
        events = list(EventLog.read(log_file))
    assert [e.agent for e in events] == ["a", "b"]


# ---------- Crash recovery (the important one) ----------


def _writer_subprocess(log_path: str, n_events: int, ready_event) -> None:  # type: ignore[no-untyped-def]
    """Child process: write N events, then signal parent to kill us.

    Run as a script-like function because mp.Process pickles it; keep imports
    and behavior self-contained.
    """
    # Re-import inside child — multiprocessing on some platforms re-imports anyway
    # but being explicit hurts nothing.
    from pathlib import Path as P

    from forge.event_log import EventLog as EL

    log = EL(P(log_path))
    for i in range(n_events):
        log.log(agent="writer", phase=f"event-{i}", run_id="crash-test")
    # Tell parent we're done writing
    ready_event.set()
    # Block forever; parent will SIGKILL us
    while True:
        time.sleep(60)


@pytest.mark.skipif(
    sys.platform == "win32",
    reason="SIGKILL semantics differ on Windows; fsync guarantees still hold but test is POSIX-shaped",
)
def test_events_survive_sigkill(tmp_path: Path) -> None:
    """The headline crash-recovery test.

    Spawn a subprocess that writes N events then blocks. SIGKILL it.
    All N events must be readable from the log — fsync per event guarantees
    they hit disk before the kill.

    SIGKILL (not SIGTERM) is deliberate: it's the worst case the OS allows,
    no chance for cleanup, no Python finalizers run.
    """
    log_path = tmp_path / "events.jsonl"
    n_events = 5

    ctx = mp.get_context("spawn")  # spawn = clean child, no fork-inherited state
    ready = ctx.Event()
    proc = ctx.Process(target=_writer_subprocess, args=(str(log_path), n_events, ready))
    proc.start()

    # Wait for the writer to finish writing all events
    assert ready.wait(timeout=10), "writer subprocess did not finish in time"

    # Kill -9 — no graceful shutdown, no buffer flush via Python's atexit.
    # Anything not already on disk via fsync is gone forever.
    os.kill(proc.pid, signal.SIGKILL)
    proc.join(timeout=5)
    assert not proc.is_alive()

    # Now read what survived
    events = list(EventLog.read(log_path))
    assert len(events) == n_events, (
        f"expected {n_events} events to survive SIGKILL after fsync, got {len(events)}"
    )
    assert [e.phase for e in events] == [f"event-{i}" for i in range(n_events)]


# ---------- Event schema sanity ----------


def test_event_required_fields() -> None:
    """Spot-check Event itself — guards against silent schema drift."""
    e = Event(timestamp="2026-01-01T00:00:00Z", run_id="r1", agent="a", phase="p")
    assert e.payload == {}
    assert e.tokens_in is None
