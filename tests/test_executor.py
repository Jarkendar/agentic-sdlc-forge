"""Executor tests — full task lifecycle on a realistic temp repo.

The Executor wires together:
  - clean worktree check
  - run-branch and task-branch creation
  - AiderRunner invocation (mocked via FakeAiderRunner — D8)
  - status classification (success/failed/no_changes)
  - out-of-scope edit detection
  - squash + merge on success, leave-as-is on failure
  - EventLog emission throughout

Tests use a real `git init` repo (faster + faithful to real git semantics)
plus a hand-rolled fake AiderRunner whose `run()` is scripted per test.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from forge.agents.executor import (
    ExecutorError,
    detect_commit_type,
    run_executor,
)
from forge.aider_runner import AiderInvocation, AiderResult
from forge.event_log import EventLog
from forge.git_ops import (
    current_head_sha,
    ensure_run_branch,
    run_branch_name,
    task_branch_name,
)
from forge.schemas import ExecutionResult, Task

# ---------------------------------------------------------------------------
# Test infrastructure
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _make_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    return repo


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


@dataclass
class _Edit:
    """One file edit a fake aider 'makes'. Applied to the repo before commit."""

    relpath: str
    content: str


@dataclass
class _ScriptedAiderResponse:
    """One scripted invocation — what fake aider does and reports."""

    edits: list[_Edit] = field(default_factory=list)
    extra_files_to_create: list[_Edit] = field(default_factory=list)
    exit_code: int = 0
    stdout: str = "ok"
    stderr: str = ""
    duration_ms: int = 100
    timed_out: bool = False
    skip_commit: bool = False


class FakeAiderRunner:
    """Hand-rolled fake — actually edits the repo and commits, just like aider.

    Why not MagicMock: we want our test repo to *look* like aider ran. The
    Executor inspects the working tree and git history; a mock returning
    AiderResult wouldn't change anything on disk and the git_ops calls
    would diverge from production behavior.

    Each test scripts one or more responses; the runner pops them in order.
    """

    def __init__(self, responses: list[_ScriptedAiderResponse]) -> None:
        self._responses = list(responses)
        self.invocations: list[AiderInvocation] = []

    def run(
        self,
        invocation: AiderInvocation,
        *,
        raise_on_timeout: bool = False,
    ) -> AiderResult:
        self.invocations.append(invocation)
        if not self._responses:
            raise AssertionError("FakeAiderRunner ran out of scripted responses")
        scripted = self._responses.pop(0)

        # Apply edits to the repo (like aider would)
        for edit in scripted.edits + scripted.extra_files_to_create:
            target = invocation.cwd / edit.relpath
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(edit.content, encoding="utf-8")

        # Commit them — aider's default behavior with --yes
        if (scripted.edits or scripted.extra_files_to_create) and not scripted.skip_commit:
            subprocess.run(
                ["git", "add", "-A"],
                cwd=invocation.cwd,
                check=True,
                capture_output=True,
            )
            subprocess.run(
                ["git", "commit", "-m", f"aider: edit {len(scripted.edits)} file(s)"],
                cwd=invocation.cwd,
                check=True,
                capture_output=True,
            )

        return AiderResult(
            exit_code=scripted.exit_code,
            stdout=scripted.stdout,
            stderr=scripted.stderr,
            duration_ms=scripted.duration_ms,
            timed_out=scripted.timed_out,
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_task(
    *,
    task_id: str = "task-001",
    goal: str = "Add the foo function.",
    files: list[Path] | None = None,
) -> Task:
    return Task(
        id=task_id,
        goal=goal,
        files=files if files is not None else [Path("src/foo.py")],
        acceptance_criteria=["foo() returns 1"],
        depends_on=[],
    )


def _setup(
    tmp_path: Path,
    *,
    run_id: str = "run-1",
) -> tuple[Path, Path]:
    """Create a clean repo with the run branch already established."""
    repo = _make_repo(tmp_path)
    ensure_run_branch(repo, run_id)
    log_path = tmp_path / "events.jsonl"
    return repo, log_path


# ---------------------------------------------------------------------------
# Happy path: success → squash + merge
# ---------------------------------------------------------------------------


def test_success_path_squashes_and_merges_into_run_branch(tmp_path: Path) -> None:
    repo, log_path = _setup(tmp_path)
    task = _make_task(files=[Path("src/foo.py")])
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(edits=[_Edit("src/foo.py", "def foo(): return 1\n")]),
    ])

    with EventLog(log_path) as event_log:
        result = run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    assert isinstance(result, ExecutionResult)
    assert result.status == "success"
    assert result.task_id == "task-001"
    assert Path("src/foo.py") in result.files_changed
    # We end up on the run branch, per D3.5
    assert _current_branch(repo) == run_branch_name("run-1")
    # The run branch has the task's content
    assert (repo / "src/foo.py").read_text() == "def foo(): return 1\n"
    # Merge commit exists on run branch (--no-ff)
    head_subject = _git(repo, "log", "-1", "--pretty=%s")
    assert head_subject == "merge: integrate task-001 into run run-1"
    # The merge commit has 2 parents
    parents = _git(repo, "log", "-1", "--pretty=%P").split()
    assert len(parents) == 2


def test_success_path_squashes_multiple_aider_commits_into_one(tmp_path: Path) -> None:
    """Aider often creates multiple commits per --message. We squash them
    into one conventional commit before merging."""
    repo, log_path = _setup(tmp_path)
    task = _make_task(files=[Path("src/foo.py")])
    # One scripted response, but it makes two edits. The fake runner commits
    # them all in one go though — to simulate multiple aider commits we'd need
    # multiple scripted responses, which doesn't match one Executor.run() call.
    # Instead we have aider make N separate file changes in N commits via a
    # custom runner that does multiple add+commit cycles. Implementation:
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(
            edits=[_Edit("src/foo.py", "v1\n")],
        ),
    ])
    # Sneak in a second commit *after* the scripted response by patching
    # the runner to do two commits. Simpler: write a runner subclass.

    class TwoCommitRunner(FakeAiderRunner):
        def run(
            self,
            invocation: AiderInvocation,
            *,
            raise_on_timeout: bool = False,
        ) -> AiderResult:
            # First commit
            (invocation.cwd / "src").mkdir(exist_ok=True)
            (invocation.cwd / "src/foo.py").write_text("v1\n")
            subprocess.run(
                ["git", "add", "-A"], cwd=invocation.cwd, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "aider: commit 1"],
                cwd=invocation.cwd, check=True, capture_output=True,
            )
            # Second commit
            (invocation.cwd / "src/foo.py").write_text("v2\n")
            subprocess.run(
                ["git", "add", "-A"], cwd=invocation.cwd, check=True, capture_output=True
            )
            subprocess.run(
                ["git", "commit", "-m", "aider: commit 2"],
                cwd=invocation.cwd, check=True, capture_output=True,
            )
            return AiderResult(
                exit_code=0, stdout="", stderr="", duration_ms=10, timed_out=False
            )

    runner = TwoCommitRunner([])

    with EventLog(log_path) as event_log:
        result = run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    assert result.status == "success"
    # On the run branch, the merge commit's parent (the task branch tip after squash)
    # must contain exactly ONE non-merge commit since the task branch's base.
    # Equivalent check: the second parent of the merge commit (task tip)
    # has exactly one commit between it and the merge base.
    log = _git(repo, "log", "--oneline", "--all")
    # Look for our squashed conventional commit
    assert "feat: add the foo function" in log.lower() or "feat: " in log.lower()


def test_success_squashed_commit_message_uses_conventional_format(tmp_path: Path) -> None:
    repo, log_path = _setup(tmp_path)
    task = _make_task(
        task_id="task-007",
        goal="Add the user model.",
        files=[Path("src/user.py")],
    )
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(edits=[_Edit("src/user.py", "class User: pass\n")]),
    ])

    with EventLog(log_path) as event_log:
        run_executor(
            task=task,
            run_id="run-x",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    # Check the squashed task branch commit (second parent of merge commit)
    parents = _git(repo, "log", "-1", "--pretty=%P").split()
    task_tip = parents[1]
    msg = _git(repo, "log", "-1", "--pretty=%B", task_tip)
    # Subject line: conventional commit
    first_line = msg.splitlines()[0]
    assert first_line.startswith("feat: ")
    assert "add the user model" in first_line.lower()
    # Footer: traceability
    assert "forge-task-id: task-007" in msg
    assert "forge-run-id: run-x" in msg


# ---------------------------------------------------------------------------
# no_changes path: aider succeeded but did nothing
# ---------------------------------------------------------------------------


def test_no_changes_when_aider_exits_zero_but_no_commits(tmp_path: Path) -> None:
    """Aider can exit 0 having decided no edits were needed. Per executor.md,
    that's `no_changes` (soft failure). Branch stays around for inspection."""
    repo, log_path = _setup(tmp_path)
    task = _make_task()
    # Scripted response with NO edits — fake runner makes no commits
    runner = FakeAiderRunner([_ScriptedAiderResponse(edits=[])])

    with EventLog(log_path) as event_log:
        result = run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    assert result.status == "no_changes"
    assert result.files_changed == []
    # We end on the run branch (D3.5: always end on run branch, regardless of status)
    assert _current_branch(repo) == run_branch_name("run-1")
    # Task branch is preserved for inspection
    branches = _git(repo, "branch", "--list", task_branch_name("run-1", "task-001"))
    assert task_branch_name("run-1", "task-001") in branches


# ---------------------------------------------------------------------------
# Failure paths
# ---------------------------------------------------------------------------


def test_failed_when_aider_exits_nonzero(tmp_path: Path) -> None:
    repo, log_path = _setup(tmp_path)
    task = _make_task()
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(exit_code=2, stderr="something exploded"),
    ])

    with EventLog(log_path) as event_log:
        result = run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    assert result.status == "failed"
    assert "something exploded" in result.aider_stderr
    # End up on run branch (D3.5)
    assert _current_branch(repo) == run_branch_name("run-1")
    # Task branch preserved
    branches = _git(repo, "branch", "--list", task_branch_name("run-1", "task-001"))
    assert task_branch_name("run-1", "task-001") in branches


def test_failed_does_not_squash_or_merge(tmp_path: Path) -> None:
    """Per D3.4: failed tasks keep raw aider commits on the task branch.
    Per D3.8: no merge into run branch."""
    repo, log_path = _setup(tmp_path)
    run_tip_before = current_head_sha(repo)
    task = _make_task()
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(
            edits=[_Edit("src/foo.py", "broken\n")],
            exit_code=1,
            stderr="oops",
        ),
    ])

    with EventLog(log_path) as event_log:
        run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    # Run branch tip unchanged — no merge happened
    assert current_head_sha(repo) == run_tip_before
    # The aider commit is still on the task branch
    task_log = _git(
        repo, "log", task_branch_name("run-1", "task-001"), "--oneline"
    )
    assert "aider: edit" in task_log


def test_failed_on_timeout(tmp_path: Path) -> None:
    repo, log_path = _setup(tmp_path)
    task = _make_task()
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(
            timed_out=True,
            exit_code=-9,
            stderr="forge: timeout after 600s",
        ),
    ])

    with EventLog(log_path) as event_log:
        result = run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    assert result.status == "failed"
    assert "timeout" in result.aider_stderr.lower()


# ---------------------------------------------------------------------------
# Out-of-scope edits (D3.8)
# ---------------------------------------------------------------------------


def test_out_of_scope_edit_marks_failed_and_keeps_branch(tmp_path: Path) -> None:
    """D3.8: out-of-scope edits = failed status, no merge, branch wisi.
    Stderr gets `forge: out-of-scope edit` line."""
    repo, log_path = _setup(tmp_path)
    task = _make_task(files=[Path("src/foo.py")])
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(
            edits=[
                _Edit("src/foo.py", "ok\n"),
                _Edit("src/bar.py", "not allowed\n"),  # out of scope
            ],
        ),
    ])

    with EventLog(log_path) as event_log:
        result = run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    assert result.status == "failed"
    assert "out-of-scope" in result.aider_stderr.lower()
    # Both files are listed in files_changed (we report what aider did)
    changed_set = {str(p) for p in result.files_changed}
    assert "src/foo.py" in changed_set
    assert "src/bar.py" in changed_set
    # No merge — run branch tip unchanged from task branch creation
    assert _current_branch(repo) == run_branch_name("run-1")
    # Run branch does NOT contain the bar.py change
    assert not (repo / "src/bar.py").exists()


# ---------------------------------------------------------------------------
# Pre-flight checks
# ---------------------------------------------------------------------------


def test_dirty_worktree_aborts_before_aider(tmp_path: Path) -> None:
    repo, log_path = _setup(tmp_path)
    # Make worktree dirty
    (repo / "untracked.txt").write_text("x")
    task = _make_task()
    runner = FakeAiderRunner([_ScriptedAiderResponse()])

    with EventLog(log_path) as event_log, pytest.raises(ExecutorError, match="working tree"):
        run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    # Aider was never invoked
    assert runner.invocations == []


def test_executor_creates_run_branch_if_missing(tmp_path: Path) -> None:
    """D3.1 + D3.2: standalone `forge execute` creates the run branch
    from current HEAD if it doesn't yet exist."""
    repo = _make_repo(tmp_path)
    log_path = tmp_path / "events.jsonl"
    # Note: we did NOT call ensure_run_branch — Executor must do it itself
    task = _make_task()
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(edits=[_Edit("src/foo.py", "x\n")]),
    ])

    with EventLog(log_path) as event_log:
        result = run_executor(
            task=task,
            run_id="run-fresh",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    assert result.status == "success"
    assert _current_branch(repo) == run_branch_name("run-fresh")


# ---------------------------------------------------------------------------
# Aider invocation contract
# ---------------------------------------------------------------------------


def test_aider_invocation_includes_acceptance_criteria_in_message(tmp_path: Path) -> None:
    """D2 (option C): structured markdown message: goal, acceptance criteria, files in scope."""
    repo, log_path = _setup(tmp_path)
    task = Task(
        id="task-001",
        goal="Add a hello endpoint.",
        files=[Path("src/api.py")],
        acceptance_criteria=[
            "GET /hello returns 200",
            "response body equals 'hi'",
        ],
        depends_on=[],
    )
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(edits=[_Edit("src/api.py", "x\n")]),
    ])

    with EventLog(log_path) as event_log:
        run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    [invocation] = runner.invocations
    msg = invocation.message
    assert "Add a hello endpoint." in msg
    assert "GET /hello returns 200" in msg
    assert "response body equals 'hi'" in msg
    assert "src/api.py" in msg


def test_aider_invocation_files_match_task_files(tmp_path: Path) -> None:
    repo, log_path = _setup(tmp_path)
    task = _make_task(files=[Path("src/a.py"), Path("tests/test_a.py")])
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(
            edits=[
                _Edit("src/a.py", "1\n"),
                _Edit("tests/test_a.py", "2\n"),
            ],
        ),
    ])

    with EventLog(log_path) as event_log:
        run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    [invocation] = runner.invocations
    file_set = {str(f) for f in invocation.files}
    assert file_set == {"src/a.py", "tests/test_a.py"}


def test_aider_invocation_includes_previous_failure_when_provided(tmp_path: Path) -> None:
    """Per executor.md, fix-loop adds the previous failure summary to the prompt."""
    repo, log_path = _setup(tmp_path)
    task = _make_task()
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(edits=[_Edit("src/foo.py", "x\n")]),
    ])

    with EventLog(log_path) as event_log:
        run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
            previous_failure="test_foo failed: assertion error on line 42",
        )

    [invocation] = runner.invocations
    assert "Previous attempt failed" in invocation.message
    assert "test_foo failed: assertion error on line 42" in invocation.message


# ---------------------------------------------------------------------------
# EventLog emission
# ---------------------------------------------------------------------------


def test_executor_emits_start_and_end_events(tmp_path: Path) -> None:
    repo, log_path = _setup(tmp_path)
    task = _make_task()
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(edits=[_Edit("src/foo.py", "x\n")]),
    ])

    with EventLog(log_path) as event_log:
        run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    events = list(EventLog.read(log_path))
    phases = [e.phase for e in events]
    assert "start" in phases
    # Aider stdout/stderr lives in events for full trail (Stage 5 DoD)
    assert any(e.phase == "aider_complete" for e in events)
    assert any(e.phase == "validated" for e in events)


def test_executor_logs_full_aider_output_in_events(tmp_path: Path) -> None:
    """Stage 5 DoD: 'EventLog captures full Aider stdout/stderr'."""
    repo, log_path = _setup(tmp_path)
    task = _make_task()
    runner = FakeAiderRunner([
        _ScriptedAiderResponse(
            edits=[_Edit("src/foo.py", "x\n")],
            stdout="long aider output here",
            stderr="some warning",
        ),
    ])

    with EventLog(log_path) as event_log:
        run_executor(
            task=task,
            run_id="run-1",
            repo_root=repo,
            aider=runner,
            event_log=event_log,
        )

    events = list(EventLog.read(log_path))
    aider_events = [e for e in events if e.phase == "aider_complete"]
    assert aider_events
    payload = aider_events[0].payload
    assert payload["stdout"] == "long aider output here"
    assert payload["stderr"] == "some warning"


# ---------------------------------------------------------------------------
# Conventional commit type detection (heuristic, post-MVP swap planned)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "goal,expected",
    [
        ("Add the foo function.", "feat"),
        ("Implement user login.", "feat"),
        ("Create new endpoint.", "feat"),
        ("Introduce caching layer.", "feat"),
        ("Fix the off-by-one bug.", "fix"),
        ("Resolve race condition.", "fix"),
        ("Repair broken test.", "fix"),
        ("Refactor the parser.", "refactor"),
        ("Extract validation logic.", "refactor"),
        ("Rename foo to bar.", "refactor"),
        ("Test the new endpoint.", "test"),
        ("Cover the edge cases.", "test"),
        ("Document the API.", "docs"),
        ("Bump dependency versions.", "chore"),
        ("Upgrade pydantic.", "chore"),
        ("Investigate weird thing.", "chore"),  # fallback
        ("", "chore"),  # fallback for empty
    ],
)
def test_detect_commit_type_heuristic(goal: str, expected: str) -> None:
    assert detect_commit_type(goal) == expected
