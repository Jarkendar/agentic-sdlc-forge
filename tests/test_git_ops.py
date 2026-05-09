"""Git operations tests — realistic temp repos, no mocking of git itself.

Mocking git would mean hand-rolling its semantics (branch state, HEAD, merge
behavior). It's both more code and less faithful than just running git. Each
test gets its own tmp_path with a freshly initialized repo.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from forge.git_ops import (
    GitOpsError,
    OutOfScopeEdit,
    create_task_branch,
    current_head_sha,
    diff_files_since,
    ensure_clean_worktree,
    ensure_run_branch,
    has_new_commits_since,
    merge_task_into_run,
    run_branch_name,
    squash_task_commits,
    task_branch_name,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run a git command in `repo`, return stripped stdout, raise on failure."""
    result = subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_repo(tmp_path: Path, *, initial_branch: str = "main") -> Path:
    """Init a repo with one commit on `initial_branch`. Returns its path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", initial_branch)
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "commit", "--allow-empty", "-m", "initial")
    return repo


def _write(repo: Path, relpath: str, content: str) -> None:
    p = repo / relpath
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")


def _commit_all(repo: Path, message: str) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", message)


def _current_branch(repo: Path) -> str:
    return _git(repo, "rev-parse", "--abbrev-ref", "HEAD")


# ---------------------------------------------------------------------------
# Naming conventions
# ---------------------------------------------------------------------------


def test_run_branch_name_format() -> None:
    assert run_branch_name("20260101-120000-abcdef") == "forge/run/20260101-120000-abcdef"


def test_task_branch_name_format() -> None:
    name = task_branch_name("20260101-120000-abcdef", "task-001")
    assert name == "forge/task/20260101-120000-abcdef/task-001"


# ---------------------------------------------------------------------------
# ensure_clean_worktree
# ---------------------------------------------------------------------------


def test_ensure_clean_worktree_passes_on_clean_repo(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # Should not raise
    ensure_clean_worktree(repo)


def test_ensure_clean_worktree_raises_on_modified_file(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    _write(repo, "README.md", "tracked content")
    _commit_all(repo, "add readme")
    _write(repo, "README.md", "modified")  # working tree dirty

    with pytest.raises(GitOpsError, match="working tree is not clean"):
        ensure_clean_worktree(repo)


def test_ensure_clean_worktree_raises_on_untracked_file(tmp_path: Path) -> None:
    """Untracked files would be picked up by Aider — also unsafe to start."""
    repo = _make_repo(tmp_path)
    _write(repo, "untracked.txt", "x")

    with pytest.raises(GitOpsError, match="working tree is not clean"):
        ensure_clean_worktree(repo)


# ---------------------------------------------------------------------------
# current_head_sha
# ---------------------------------------------------------------------------


def test_current_head_sha_returns_full_sha(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    sha = current_head_sha(repo)
    # 40-char hex SHA
    assert len(sha) == 40
    assert all(c in "0123456789abcdef" for c in sha)


# ---------------------------------------------------------------------------
# ensure_run_branch
# ---------------------------------------------------------------------------


def test_ensure_run_branch_creates_from_head_when_missing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path, initial_branch="main")
    head_before = current_head_sha(repo)

    branch = ensure_run_branch(repo, "20260101-120000-abcdef")

    assert branch == "forge/run/20260101-120000-abcdef"
    assert _current_branch(repo) == branch
    # Branched from HEAD — same commit
    assert current_head_sha(repo) == head_before


def test_ensure_run_branch_is_idempotent_when_branch_exists(tmp_path: Path) -> None:
    """Second call when the branch exists must just checkout it, not fail."""
    repo = _make_repo(tmp_path)
    ensure_run_branch(repo, "rid")
    # Add a commit on the run branch
    _write(repo, "x.txt", "x")
    _commit_all(repo, "add x")
    head_after_commit = current_head_sha(repo)

    # Switch back to main, then re-ensure — must checkout existing branch, not recreate
    _git(repo, "checkout", "main")
    ensure_run_branch(repo, "rid")

    assert _current_branch(repo) == "forge/run/rid"
    # The branch wasn't reset — the commit we made is still there
    assert current_head_sha(repo) == head_after_commit


# ---------------------------------------------------------------------------
# create_task_branch
# ---------------------------------------------------------------------------


def test_create_task_branch_branches_from_run_branch(tmp_path: Path) -> None:
    """Task branch must branch from the run branch's tip, regardless of where HEAD is."""
    repo = _make_repo(tmp_path)
    ensure_run_branch(repo, "rid")
    _write(repo, "run-file.txt", "run")
    _commit_all(repo, "run commit")
    run_tip = current_head_sha(repo)

    # Pretend caller is on a different branch
    _git(repo, "checkout", "main")

    branch = create_task_branch(repo, "rid", "task-001")

    assert branch == "forge/task/rid/task-001"
    assert _current_branch(repo) == branch
    # Branched from run tip, not from main
    assert current_head_sha(repo) == run_tip


def test_create_task_branch_fails_if_run_branch_missing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    with pytest.raises(GitOpsError, match="run branch.*does not exist"):
        create_task_branch(repo, "rid", "task-001")


def test_create_task_branch_fails_if_task_branch_already_exists(tmp_path: Path) -> None:
    """A pre-existing task branch is a sign of a previous failed run — don't clobber."""
    repo = _make_repo(tmp_path)
    ensure_run_branch(repo, "rid")
    create_task_branch(repo, "rid", "task-001")

    _git(repo, "checkout", "forge/run/rid")
    with pytest.raises(GitOpsError, match="task branch.*already exists"):
        create_task_branch(repo, "rid", "task-001")


# ---------------------------------------------------------------------------
# has_new_commits_since / diff_files_since
# ---------------------------------------------------------------------------


def test_has_new_commits_since_false_when_no_commits(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base = current_head_sha(repo)
    assert has_new_commits_since(repo, base) is False


def test_has_new_commits_since_true_after_commit(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base = current_head_sha(repo)
    _write(repo, "x.txt", "x")
    _commit_all(repo, "x")
    assert has_new_commits_since(repo, base) is True


def test_diff_files_since_returns_committed_changes(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base = current_head_sha(repo)
    _write(repo, "src/a.py", "content a")
    _write(repo, "src/b.py", "content b")
    _commit_all(repo, "add a and b")

    files = diff_files_since(repo, base)
    assert sorted(str(f) for f in files) == ["src/a.py", "src/b.py"]


def test_diff_files_since_returns_empty_when_no_commits(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    base = current_head_sha(repo)
    assert diff_files_since(repo, base) == []


# ---------------------------------------------------------------------------
# squash_task_commits
# ---------------------------------------------------------------------------


def test_squash_collapses_multiple_commits_into_one(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ensure_run_branch(repo, "rid")
    create_task_branch(repo, "rid", "task-001")
    base = current_head_sha(repo)

    _write(repo, "a.py", "1")
    _commit_all(repo, "wip 1")
    _write(repo, "a.py", "2")
    _commit_all(repo, "wip 2")
    _write(repo, "b.py", "x")
    _commit_all(repo, "wip 3")

    squash_task_commits(
        repo,
        base_sha=base,
        message="feat: do the thing\n\nbody here\n\nforge-task-id: task-001",
    )

    # Now exactly one commit since base
    log = _git(repo, "log", f"{base}..HEAD", "--pretty=%s")
    assert log == "feat: do the thing"
    # Files preserved
    assert (repo / "a.py").read_text() == "2"
    assert (repo / "b.py").read_text() == "x"


def test_squash_with_single_commit_still_works(tmp_path: Path) -> None:
    """Squashing 1 commit must produce 1 commit with the new message."""
    repo = _make_repo(tmp_path)
    ensure_run_branch(repo, "rid")
    create_task_branch(repo, "rid", "task-001")
    base = current_head_sha(repo)

    _write(repo, "a.py", "1")
    _commit_all(repo, "raw aider commit")

    squash_task_commits(repo, base_sha=base, message="feat: clean message")

    subjects = _git(repo, "log", f"{base}..HEAD", "--pretty=%s").splitlines()
    assert subjects == ["feat: clean message"]


def test_squash_raises_when_no_commits_to_squash(tmp_path: Path) -> None:
    """Squash with no new commits is a usage bug — fail loudly."""
    repo = _make_repo(tmp_path)
    ensure_run_branch(repo, "rid")
    create_task_branch(repo, "rid", "task-001")
    base = current_head_sha(repo)

    with pytest.raises(GitOpsError, match="no commits to squash"):
        squash_task_commits(repo, base_sha=base, message="feat: nothing")


# ---------------------------------------------------------------------------
# merge_task_into_run
# ---------------------------------------------------------------------------


def test_merge_creates_no_ff_merge_commit(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ensure_run_branch(repo, "rid")
    run_base = current_head_sha(repo)

    create_task_branch(repo, "rid", "task-001")
    task_base = current_head_sha(repo)
    _write(repo, "a.py", "x")
    _commit_all(repo, "feat: add a")
    squash_task_commits(repo, base_sha=task_base, message="feat: add a")

    merge_task_into_run(repo, "rid", "task-001")

    # We are back on run branch
    assert _current_branch(repo) == "forge/run/rid"
    # The merge created a merge commit (--no-ff guarantees this even for FF cases)
    head_subject = _git(repo, "log", "-1", "--pretty=%s")
    assert head_subject == "merge: integrate task-001 into run rid"
    # Has 2 parents — proves --no-ff worked
    parents = _git(repo, "log", "-1", "--pretty=%P").split()
    assert len(parents) == 2
    # First parent is run branch tip before merge
    assert parents[0] == run_base
    # File from task is now on run branch
    assert (repo / "a.py").read_text() == "x"


def test_merge_fails_when_task_branch_missing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    ensure_run_branch(repo, "rid")
    with pytest.raises(GitOpsError, match="task branch.*does not exist"):
        merge_task_into_run(repo, "rid", "task-001")


def test_merge_fails_when_run_branch_missing(tmp_path: Path) -> None:
    repo = _make_repo(tmp_path)
    # Manually create a task branch with no run branch (artificial)
    _git(repo, "checkout", "-b", "forge/task/rid/task-001")
    with pytest.raises(GitOpsError, match="run branch.*does not exist"):
        merge_task_into_run(repo, "rid", "task-001")


# ---------------------------------------------------------------------------
# OutOfScopeEdit detection — exposed as a helper since executor uses it
# ---------------------------------------------------------------------------


def test_out_of_scope_returns_paths_outside_allowed(tmp_path: Path) -> None:
    edit = OutOfScopeEdit.detect(
        changed=[Path("src/a.py"), Path("src/b.py"), Path("docs/c.md")],
        allowed=[Path("src/a.py"), Path("src/b.py")],
    )
    assert edit.has_violations is True
    assert edit.offending == [Path("docs/c.md")]


def test_out_of_scope_no_violations_when_subset(tmp_path: Path) -> None:
    edit = OutOfScopeEdit.detect(
        changed=[Path("src/a.py")],
        allowed=[Path("src/a.py"), Path("src/b.py")],
    )
    assert edit.has_violations is False
    assert edit.offending == []


def test_out_of_scope_with_empty_allowed_treats_all_as_violations() -> None:
    """Task with empty file list: anything Aider creates is out of scope.

    Empty `task.files` was meant to allow *new* files (per executor.md), but
    in MVP we err on the side of strictness. The Planner should always list
    new files explicitly. If empty file list semantics change, update this test.
    """
    edit = OutOfScopeEdit.detect(
        changed=[Path("src/a.py")],
        allowed=[],
    )
    assert edit.has_violations is True
    assert edit.offending == [Path("src/a.py")]
