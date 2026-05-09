"""Aider subprocess wrapper — bounded, output-captured, timeout-safe.

This module owns *only* the subprocess. It does not know about tasks, branches,
git, or events. Caller (Executor) is responsible for assembling the message,
deciding which files to pass, building the prompt context, and interpreting
the result.

Timeout strategy (D6):

    On Linux we spawn aider in a new session (start_new_session=True) so
    the child becomes the leader of its own process group. On timeout we
    SIGKILL the whole group via os.killpg(pgid, SIGKILL). This catches
    aider's own children (linters, test runners) — without process-group
    kill they would linger as orphans owned by init.

    Per IMPLEMENTATION_PLAN: MVP is Linux-first. Windows would need a
    different path (CREATE_NEW_PROCESS_GROUP + GenerateConsoleCtrlEvent).
    Tracked as a future README item.

Aider binary lookup:

    By default we resolve `aider` on $PATH at construction time and fail
    fast if it's missing — confusing FileNotFoundErrors at run time are
    worse than a clear error at startup. Tests inject `binary=` explicitly.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AiderNotFoundError(RuntimeError):
    """Raised when the aider binary can't be found on PATH at construction.

    Aider is an external dependency (D9): we don't pin it in pyproject.toml
    because its CLI flags evolve and users want their own version. The cost
    of that decision is having to fail loudly when it's missing.
    """


class AiderTimeoutError(RuntimeError):
    """Raised by run() when timeout fires AND raise_on_timeout=True.

    The default flow is to return an AiderResult with timed_out=True so the
    caller can record it as an ExecutionResult.failed without exception
    plumbing. Stage 7 escalation may prefer the exception form.
    """


# ---------------------------------------------------------------------------
# Data shapes
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AiderInvocation:
    """One Aider call's parameters. Frozen — these don't mutate during a run."""

    message: str
    files: list[Path]
    cwd: Path
    timeout_seconds: int = 600


@dataclass(frozen=True)
class AiderResult:
    """Outcome of one subprocess run, before any task-level interpretation.

    Caller (Executor) maps this onto ExecutionResult.status by also looking
    at git state — exit_code alone doesn't distinguish 'success but no edits'
    from 'success with edits'.
    """

    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    timed_out: bool


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------


class AiderRunner:
    """Calls `aider` as a subprocess. One instance reusable across tasks.

    Construction-time check for the binary keeps the surface area small:
    the runner is the only thing in the codebase that needs to know where
    aider lives.
    """

    def __init__(self, binary: Path | None = None) -> None:
        if binary is None:
            located = shutil.which("aider")
            if located is None:
                raise AiderNotFoundError(
                    "aider not found on PATH. Install it with `uv tool install aider-chat` "
                    "or `pipx install aider-chat`, or pass binary=Path('/path/to/aider')."
                )
            binary = Path(located)
        self.binary = binary

    # ------------------------------------------------------------------
    # Command construction (separated for testability)
    # ------------------------------------------------------------------

    def build_command(self, invocation: AiderInvocation) -> list[str]:
        """Assemble the argv for one aider call.

        Public so tests can lock in the flags without spawning a process,
        and so the Executor can log the exact command that was run.

        --yes        : never prompt; there's no human in the loop.
        --no-stream  : block until done, return one final response.
        --message X  : the prompt; passed via two args to dodge any quoting
                       quirks Aider might have on Windows-style shells.
        Files come last — Aider treats positional args after flags as files
        to add to the chat session.
        """
        cmd: list[str] = [
            str(self.binary),
            "--yes",
            "--no-stream",
            "--message",
            invocation.message,
        ]
        cmd.extend(str(f) for f in invocation.files)
        return cmd

    # ------------------------------------------------------------------
    # Run
    # ------------------------------------------------------------------

    def run(
        self,
        invocation: AiderInvocation,
        *,
        raise_on_timeout: bool = False,
    ) -> AiderResult:
        """Execute aider, capture streams, enforce timeout. Always returns an
        AiderResult unless raise_on_timeout=True and a timeout fires.

        Behavior:
        - stdout/stderr are captured as bytes, then decoded UTF-8 with
          'replace' for invalid sequences. We never raise on decode.
        - On timeout we SIGKILL the process group, drain whatever output
          was buffered, and stamp a 'forge: timeout after Ns' line into
          stderr (per executor.md contract).
        - duration_ms is wall clock for the whole subprocess lifecycle,
          including any time spent killing the group on timeout.
        """
        cmd = self.build_command(invocation)

        start = time.perf_counter()
        proc = subprocess.Popen(
            cmd,
            cwd=invocation.cwd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            # Leader of its own process group on POSIX. Without this,
            # killpg below would target our own group.
            start_new_session=True,
        )

        timed_out = False
        try:
            stdout_b, stderr_b = proc.communicate(timeout=invocation.timeout_seconds)
        except subprocess.TimeoutExpired:
            timed_out = True
            # Best-effort kill of the whole process group. If pgid lookup
            # fails (very rare race), fall back to killing just the parent.
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                # Process already gone, or we lost the right to signal it.
                # communicate() below will still drain buffers either way.
                pass
            # Drain whatever buffered output exists. communicate() with no
            # timeout blocks until pipes close — they will, because the
            # process is dead.
            stdout_b, stderr_b = proc.communicate()

        duration_ms = int((time.perf_counter() - start) * 1000)

        stdout = stdout_b.decode("utf-8", errors="replace")
        stderr = stderr_b.decode("utf-8", errors="replace")

        if timed_out:
            # Stamp the canonical timeout line. Append rather than replace
            # so any partial stderr from aider survives for debugging.
            timeout_line = f"forge: timeout after {invocation.timeout_seconds}s"
            stderr = (stderr + "\n" + timeout_line) if stderr else timeout_line
            if raise_on_timeout:
                raise AiderTimeoutError(timeout_line)

        return AiderResult(
            exit_code=proc.returncode if proc.returncode is not None else -1,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
            timed_out=timed_out,
        )
