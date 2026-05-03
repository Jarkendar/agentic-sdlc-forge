"""Append-only JSONL event log — single source of truth for what happened in a run.

Every agent writes structured events here. The file is the basis for:
- resumability (RunState references last_event_offset)
- the final RUN_REPORT.md (Reporter reads it back)
- debugging (humans grep it)

Design decisions (see IMPLEMENTATION_PLAN §0.4.2):
- JSONL, one event per line. Easy to grep, easy to stream, easy to recover.
- fsync after every write. We trade throughput for crash-safety.
- Append-only. We never rewrite. Corrupted last line is tolerated; everything
  before it is preserved.
- Reader skips malformed lines with a warning rather than crashing — a partial
  last write must not poison reads of the rest of the log.
"""

from __future__ import annotations

import json
import os
import warnings
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel


def _utcnow_iso() -> str:
    """ISO8601 UTC with timezone — matches schemas._utcnow but pre-formatted."""
    return datetime.now(UTC).isoformat()


class Event(BaseModel):
    """One structured event line in the log.

    `payload` is intentionally a free dict — different agents log different
    shapes. If an agent has a stable event type worth schematizing, add it
    as a typed model in schemas.py and dump it into payload via model_dump().
    """

    timestamp: str
    run_id: str
    agent: str
    phase: str
    payload: dict[str, Any] = {}
    tokens_in: int | None = None
    tokens_out: int | None = None
    duration_ms: int | None = None


class EventLog:
    """Append-only JSONL writer with fsync after each event.

    Open once per run, close at the end. Use as a context manager or call
    close() explicitly. `log()` is the only writer method — that's by design,
    we don't expose anything that could rewrite the file.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Open in append+binary mode. Binary because we want full control over
        # newlines (always '\n', never platform-dependent '\r\n').
        self._fh = self.path.open("ab")

    def log(
        self,
        agent: str,
        phase: str,
        payload: dict[str, Any] | BaseModel | None = None,
        *,
        run_id: str,
        tokens_in: int | None = None,
        tokens_out: int | None = None,
        duration_ms: int | None = None,
    ) -> None:
        """Append one event. Flushes and fsyncs before returning.

        `payload` accepts either a dict or any pydantic model — the model is
        dumped via model_dump(mode='json') so Path/datetime/Enum survive.
        """
        if isinstance(payload, BaseModel):
            payload_dict = payload.model_dump(mode="json")
        else:
            payload_dict = payload or {}

        event = Event(
            timestamp=_utcnow_iso(),
            run_id=run_id,
            agent=agent,
            phase=phase,
            payload=payload_dict,
            tokens_in=tokens_in,
            tokens_out=tokens_out,
            duration_ms=duration_ms,
        )

        # model_dump_json gives us a single JSON object on one line.
        # Encode to bytes ourselves so '\n' is exactly one byte.
        line = event.model_dump_json().encode("utf-8") + b"\n"

        self._fh.write(line)
        self._fh.flush()
        # fsync forces the OS to actually write to disk, not just to the
        # page cache. Without this, a power loss could lose the last
        # several seconds of events even though write() returned success.
        os.fsync(self._fh.fileno())

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> EventLog:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # ----- Reader -----

    @staticmethod
    def read(path: Path) -> Iterator[Event]:
        """Read all events from a log file, skipping corrupt lines with a warning.

        A corrupt line is almost always the last one — caused by a crash
        mid-write. Earlier lines are guaranteed intact (fsync per event),
        so we yield those and skip the broken tail.
        """
        path = Path(path)
        if not path.exists():
            return
        with path.open("rb") as fh:
            for lineno, raw in enumerate(fh, start=1):
                # Strip trailing newline; tolerate lack thereof on the last line.
                stripped = raw.rstrip(b"\n").rstrip(b"\r")
                if not stripped:
                    continue
                try:
                    obj = json.loads(stripped)
                    yield Event.model_validate(obj)
                except (json.JSONDecodeError, ValueError) as e:
                    warnings.warn(
                        f"Skipping malformed event at {path}:{lineno}: {e}",
                        stacklevel=2,
                    )
                    continue
