"""AiderRunner tests — subprocess invocation, timeout handling, output capture.

These tests do NOT actually run aider. We mock subprocess.Popen so we can
control exit codes, stdout/stderr, and timing without depending on the binary.

The aider binary itself is verified at construction time via shutil.which.
"""

from __future__ import annotations

import signal
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from forge.aider_runner import (
    AiderInvocation,
    AiderNotFoundError,
    AiderResult,
    AiderRunner,
    AiderTimeoutError,
)

# ---------------------------------------------------------------------------
# Construction-time checks
# ---------------------------------------------------------------------------


def test_runner_raises_when_aider_binary_missing() -> None:
    """Failing fast at construction beats a confusing FileNotFoundError later."""
    with patch("forge.aider_runner.shutil.which", return_value=None), pytest.raises(
        AiderNotFoundError, match="aider not found on PATH"
    ):
        AiderRunner()


def test_runner_accepts_explicit_binary_path(tmp_path: Path) -> None:
    """Explicit path skips PATH lookup — useful when aider is in a venv."""
    fake_aider = tmp_path / "aider"
    fake_aider.write_text("#!/bin/sh\nexit 0\n")
    fake_aider.chmod(0o755)
    runner = AiderRunner(binary=fake_aider)
    assert runner.binary == fake_aider


# ---------------------------------------------------------------------------
# Command construction
# ---------------------------------------------------------------------------


def _make_invocation(**overrides: Any) -> AiderInvocation:
    defaults: dict[str, Any] = {
        "message": "do the thing",
        "files": [Path("src/a.py")],
        "cwd": Path("/tmp/repo"),
        "timeout_seconds": 600,
    }
    defaults.update(overrides)
    return AiderInvocation(**defaults)


def test_command_includes_yes_and_no_stream_flags() -> None:
    """Per executor.md: --yes --no-stream are non-negotiable.

    Without these, Aider would prompt for input and there's no human in the loop.
    """
    runner = AiderRunner(binary=Path("/usr/bin/aider"))
    cmd = runner.build_command(_make_invocation())
    assert "--yes" in cmd
    assert "--no-stream" in cmd


def test_command_passes_message_via_message_flag() -> None:
    runner = AiderRunner(binary=Path("/usr/bin/aider"))
    cmd = runner.build_command(_make_invocation(message="add login endpoint"))
    # --message <value> form (two args) — never --message= for portability
    assert "--message" in cmd
    msg_idx = cmd.index("--message")
    assert cmd[msg_idx + 1] == "add login endpoint"


def test_command_includes_each_file_as_positional_arg() -> None:
    """Files come after flags. Aider treats positional args as files to /add."""
    runner = AiderRunner(binary=Path("/usr/bin/aider"))
    cmd = runner.build_command(
        _make_invocation(files=[Path("src/a.py"), Path("src/b.py")])
    )
    assert "src/a.py" in cmd
    assert "src/b.py" in cmd
    # Files must be after --message (defensive ordering — Aider parses
    # everything-after-flags as files)
    assert cmd.index("src/a.py") > cmd.index("--message")


def test_command_starts_with_binary_path() -> None:
    runner = AiderRunner(binary=Path("/opt/bin/aider"))
    cmd = runner.build_command(_make_invocation())
    assert cmd[0] == "/opt/bin/aider"


# ---------------------------------------------------------------------------
# Successful run
# ---------------------------------------------------------------------------


def _mock_popen(
    *,
    exit_code: int = 0,
    stdout: bytes = b"",
    stderr: bytes = b"",
    timeout_on_communicate: bool = False,
) -> MagicMock:
    """Build a MagicMock that quacks like subprocess.Popen for our purposes."""
    proc = MagicMock()
    proc.pid = 12345
    proc.returncode = exit_code
    if timeout_on_communicate:
        # First call raises TimeoutExpired; second call (after kill) returns.
        import subprocess as _subp

        proc.communicate.side_effect = [
            _subp.TimeoutExpired(cmd="aider", timeout=1),
            (stdout, stderr),
        ]
    else:
        proc.communicate.return_value = (stdout, stderr)
    return proc


def test_run_returns_result_with_exit_code_and_streams() -> None:
    runner = AiderRunner(binary=Path("/usr/bin/aider"))
    proc = _mock_popen(exit_code=0, stdout=b"edited 1 file", stderr=b"")

    with patch("forge.aider_runner.subprocess.Popen", return_value=proc):
        result = runner.run(_make_invocation())

    assert isinstance(result, AiderResult)
    assert result.exit_code == 0
    assert result.stdout == "edited 1 file"
    assert result.stderr == ""
    assert result.timed_out is False
    assert result.duration_ms >= 0


def test_run_decodes_streams_as_utf8_with_replacement() -> None:
    """Aider output may include filenames with non-UTF-8 bytes — never crash on decode."""
    runner = AiderRunner(binary=Path("/usr/bin/aider"))
    # Lone continuation byte = invalid UTF-8
    proc = _mock_popen(exit_code=0, stdout=b"ok\x80end", stderr=b"")

    with patch("forge.aider_runner.subprocess.Popen", return_value=proc):
        result = runner.run(_make_invocation())

    # Replacement char appears, no exception raised
    assert "ok" in result.stdout
    assert "end" in result.stdout


def test_run_propagates_nonzero_exit_code() -> None:
    runner = AiderRunner(binary=Path("/usr/bin/aider"))
    proc = _mock_popen(exit_code=2, stdout=b"", stderr=b"oops")

    with patch("forge.aider_runner.subprocess.Popen", return_value=proc):
        result = runner.run(_make_invocation())

    assert result.exit_code == 2
    assert result.stderr == "oops"
    assert result.timed_out is False


# ---------------------------------------------------------------------------
# Timeout handling — D6: process group + SIGKILL
# ---------------------------------------------------------------------------


def test_run_kills_process_group_on_timeout() -> None:
    """Per D6: timeout must SIGKILL the entire process group, not just the
    parent. Aider can spawn linters/tests as children — without process group
    kill they linger as orphans."""
    runner = AiderRunner(binary=Path("/usr/bin/aider"))
    proc = _mock_popen(
        exit_code=-9,
        stdout=b"partial",
        stderr=b"",
        timeout_on_communicate=True,
    )

    killpg_calls: list[tuple[int, int]] = []

    def fake_killpg(pgid: int, sig: int) -> None:
        killpg_calls.append((pgid, sig))

    with patch("forge.aider_runner.subprocess.Popen", return_value=proc), patch(
        "forge.aider_runner.os.killpg", side_effect=fake_killpg
    ), patch("forge.aider_runner.os.getpgid", return_value=12345):
        result = runner.run(_make_invocation(timeout_seconds=1))

    assert result.timed_out is True
    # SIGKILL was sent to the process group
    assert killpg_calls
    assert killpg_calls[0][1] == signal.SIGKILL


def test_run_marks_timeout_in_stderr() -> None:
    """Per executor.md: 'forge: timeout after 600s' line on stderr after a timeout."""
    runner = AiderRunner(binary=Path("/usr/bin/aider"))
    proc = _mock_popen(timeout_on_communicate=True, stdout=b"", stderr=b"")

    with patch("forge.aider_runner.subprocess.Popen", return_value=proc), patch(
        "forge.aider_runner.os.killpg"
    ), patch("forge.aider_runner.os.getpgid", return_value=99):
        result = runner.run(_make_invocation(timeout_seconds=600))

    assert result.timed_out is True
    assert "forge: timeout after 600s" in result.stderr


def test_run_raises_aider_timeout_error_when_explicitly_requested() -> None:
    """Some callers (Stage 7 escalation path) prefer an exception over a result.
    The default is to return AiderResult with timed_out=True; opt-in to raise."""
    runner = AiderRunner(binary=Path("/usr/bin/aider"))
    proc = _mock_popen(timeout_on_communicate=True, stdout=b"", stderr=b"")

    with patch("forge.aider_runner.subprocess.Popen", return_value=proc), patch(
        "forge.aider_runner.os.killpg"
    ), patch("forge.aider_runner.os.getpgid", return_value=99), pytest.raises(AiderTimeoutError):
        runner.run(_make_invocation(timeout_seconds=600), raise_on_timeout=True)


# ---------------------------------------------------------------------------
# Process group setup — Linux-specific (D6)
# ---------------------------------------------------------------------------


def test_run_starts_aider_in_new_process_group() -> None:
    """Popen must be called with start_new_session=True so the child gets its
    own process group ID. Without this, killpg would target *our* group."""
    runner = AiderRunner(binary=Path("/usr/bin/aider"))
    proc = _mock_popen(exit_code=0, stdout=b"", stderr=b"")

    captured_kwargs: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> MagicMock:
        captured_kwargs.update(kwargs)
        return proc

    with patch("forge.aider_runner.subprocess.Popen", side_effect=capture):
        runner.run(_make_invocation())

    assert captured_kwargs.get("start_new_session") is True


def test_run_passes_cwd_to_popen() -> None:
    runner = AiderRunner(binary=Path("/usr/bin/aider"))
    proc = _mock_popen(exit_code=0)

    captured: dict[str, Any] = {}

    def capture(*args: Any, **kwargs: Any) -> MagicMock:
        captured.update(kwargs)
        return proc

    with patch("forge.aider_runner.subprocess.Popen", side_effect=capture):
        runner.run(_make_invocation(cwd=Path("/repo/here")))

    assert captured["cwd"] == Path("/repo/here")
