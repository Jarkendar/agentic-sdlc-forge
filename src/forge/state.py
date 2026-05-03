"""Persisted run state — atomic save, safe load, run_id generation.

RunState is loaded at the start of every run command and saved after every
state transition. Saves are atomic: write to a temp file, fsync, then rename.
A crash mid-save can leave the temp file behind but never corrupts state.json.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from forge.schemas import RunState


def generate_run_id(now: datetime | None = None) -> str:
    """Generate a chronologically-sortable, collision-resistant run ID.

    Format: YYYYMMDD-HHMMSS-<6-hex-chars>
    The hex suffix prevents collisions if two runs start in the same second.
    """
    ts = (now or datetime.now(UTC)).strftime("%Y%m%d-%H%M%S")
    return f"{ts}-{secrets.token_hex(3)}"


def run_dir(forge_root: Path, run_id: str) -> Path:
    """Path to a run's directory: <forge_root>/runs/<run_id>/.

    `forge_root` is typically `<project>/.forge`. The directory is created
    on first save; callers don't need to mkdir it themselves.
    """
    return Path(forge_root) / "runs" / run_id


def state_path(forge_root: Path, run_id: str) -> Path:
    return run_dir(forge_root, run_id) / "state.json"


def events_path(forge_root: Path, run_id: str) -> Path:
    return run_dir(forge_root, run_id) / "events.jsonl"


def save_state(state: RunState, forge_root: Path) -> Path:
    """Persist RunState atomically.

    Writes to a temp file in the same directory, fsyncs it, then os.replace()s
    it onto state.json. os.replace is atomic on POSIX and on Windows (Python
    docs guarantee it). A crash before the rename leaves a stray temp file
    but state.json is untouched. A crash after the rename is fine — the new
    state is fully on disk.
    """
    state.updated_at = datetime.now(UTC)
    target = state_path(forge_root, state.run_id)
    target.parent.mkdir(parents=True, exist_ok=True)

    payload = state.model_dump_json(indent=2).encode("utf-8")

    # NamedTemporaryFile in the same dir guarantees os.replace is a same-FS rename.
    fd, tmp_name = tempfile.mkstemp(
        prefix=".state-",
        suffix=".tmp",
        dir=target.parent,
    )
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, target)
    except Exception:
        # Best-effort cleanup; if replace already happened, unlink will fail
        # silently and that's fine.
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp_name)
        raise
    return target


def load_state(run_id: str, forge_root: Path) -> RunState:
    """Load RunState from disk. Raises FileNotFoundError if absent.

    Schema-version mismatch raises ValueError — we refuse to silently load
    a state.json written by a different schema version. Migration is a
    deliberate operation, not a side effect.
    """
    target = state_path(forge_root, run_id)
    raw = target.read_text(encoding="utf-8")
    state = RunState.model_validate_json(raw)
    from forge.schemas import SCHEMA_VERSION

    if state.schema_version != SCHEMA_VERSION:
        raise ValueError(
            f"Schema version mismatch for run {run_id}: "
            f"file is {state.schema_version}, runtime is {SCHEMA_VERSION}. "
            f"Migration required."
        )
    return state
