"""Git operations — single home for all branch/merge logic.

Stage 5 introduces a per-task branch model:

    forge/run/<run_id>                  — trunk branch for one full run
    forge/task/<run_id>/<task_id>       — short-lived branch per task

Lifecycle for one task (executed by the Executor):

    1. ensure_clean_worktree(repo)               # fail early on dirty repo
    2. ensure_run_branch(repo, run_id)           # idempotent, branched from HEAD
    3. base_sha = current_head_sha(repo)         # remember start of task
    4. create_task_branch(repo, run_id, task_id) # checkout task branch
    5. <Aider runs and may create N commits>
    6. if success:
           squash_task_commits(repo, base_sha, message)
           merge_task_into_run(repo, run_id, task_id)
       else:
           # leave task branch as-is, with raw Aider commits, for inspection
           # (out-of-scope edits are also classified as failures — D3.8)
           pass

Why a dedicated module?
- Stage 7's Orchestrator uses the same primitives. Putting them in agents/executor
  would force Stage 7 to import from agents/.
- Tests are realistic (real `git init` in tmp_path) and gain from being together.
- Branch name conventions live in one place, so any future rename is one edit.

Why direct subprocess calls instead of GitPython / pygit2?
- Zero new dependencies. Aider already requires git on PATH; relying on the same
  `git` binary keeps surface area small.
- Behavior matches what the user sees with `git log`, no library quirks.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

# ---------------------------------------------------------------------------
# Naming conventions
# ---------------------------------------------------------------------------

#: Prefix for all branches created by forge. Mass cleanup:
#: `git branch -D $(git branch --list 'forge/*')`.
_FORGE_BRANCH_PREFIX = "forge"


def run_branch_name(run_id: str) -> str:
    """Return the conventional branch name for a run.

    Format: ``forge/run/<run_id>``. Slashes are git's ref naming convention,
    not directory hierarchy — this is one branch, not three.
    """
    return f"{_FORGE_BRANCH_PREFIX}/run/{run_id}"


def task_branch_name(run_id: str, task_id: str) -> str:
    """Return the conventional branch name for a task within a run.

    Format: ``forge/task/<run_id>/<task_id>``. Each task gets its own short-lived
    branch; on success it's squashed and merged into the run branch (--no-ff),
    on failure it stays around for inspection.
    """
    return f"{_FORGE_BRANCH_PREFIX}/task/{run_id}/{task_id}"


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class GitOpsError(Exception):
    """Raised when a git operation fails or hits an invariant.

    Distinguished from generic subprocess errors so callers can catch the
    high-level intent (e.g. "couldn't merge — show user, don't crash") without
    swallowing real bugs.
    """


# ---------------------------------------------------------------------------
# Out-of-scope edit detection
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutOfScopeEdit:
    """Result of comparing files actually changed vs files allowed by the task.

    Frozen because it's a snapshot of one Aider run; mutating it after the
    fact would be a bug. Use the `detect` classmethod to construct.
    """

    offending: list[Path]

    @property
    def has_violations(self) -> bool:
        return bool(self.offending)

    @classmethod
    def detect(
        cls,
        *,
        changed: list[Path],
        allowed: list[Path],
    ) -> OutOfScopeEdit:
        """Return any paths in `changed` that aren't in `allowed`.

        Comparison is by string equality on the path. Both lists are expected
        to be repo-relative — git already gives us that shape via `--name-only`.
        """
        allowed_set = {str(p) for p in allowed}
        offending = [p for p in changed if str(p) not in allowed_set]
        # Stable order = order of first appearance in `changed`. Sorting would
        # be friendlier for diffs but harder to correlate with the diff output.
        return cls(offending=offending)


# ---------------------------------------------------------------------------
# Subprocess helpers
# ---------------------------------------------------------------------------


def _git(
    repo: Path,
    *args: str,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    """Run a git command in `repo` and return the completed process.

    `check=True` raises CalledProcessError on non-zero exit; callers wrap that
    in GitOpsError when the failure is expected and should be surfaced clearly.
    """
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _branch_exists(repo: Path, branch: str) -> bool:
    """True if `branch` exists locally. Avoids parsing `git branch` output."""
    result = _git(
        repo,
        "show-ref",
        "--verify",
        "--quiet",
        f"refs/heads/{branch}",
        check=False,
    )
    return result.returncode == 0


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def ensure_clean_worktree(repo: Path) -> None:
    """Raise GitOpsError if the working tree has uncommitted or untracked files.

    Untracked files matter as much as modified ones: Aider would happily edit
    or commit them, leaking unrelated state into our task branch. The check is
    `git status --porcelain` because it's the canonical "is the tree clean?"
    interface — same one humans use.
    """
    result = _git(repo, "status", "--porcelain")
    if result.stdout.strip():
        raise GitOpsError(
            f"working tree is not clean — commit or stash changes first.\n"
            f"`git status` in {repo}:\n{result.stdout}"
        )


def current_head_sha(repo: Path) -> str:
    """Return the full 40-char SHA of HEAD."""
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def ensure_run_branch(repo: Path, run_id: str) -> str:
    """Create-and-checkout or just-checkout the run branch. Idempotent.

    If the branch exists, this is a plain checkout — the branch's tip is not
    reset. Stage 5 calls this once per `forge execute`; Stage 7's Orchestrator
    will call it once at the start of a run. Both paths must be safe.

    Returns the branch name for convenience.
    """
    branch = run_branch_name(run_id)
    if _branch_exists(repo, branch):
        _git(repo, "checkout", branch)
    else:
        _git(repo, "checkout", "-b", branch)
    return branch


def create_task_branch(repo: Path, run_id: str, task_id: str) -> str:
    """Create-and-checkout a task branch off the *run branch tip*.

    This is intentionally not idempotent: a pre-existing task branch is a sign
    of a previous failed run that we kept around for inspection (D3.8). We
    refuse to clobber it; the user must delete it explicitly.

    Defensive checkout of the run branch first means callers don't have to
    track HEAD state — `create_task_branch` works regardless of where HEAD
    happened to be.
    """
    run_branch = run_branch_name(run_id)
    task_branch = task_branch_name(run_id, task_id)

    if not _branch_exists(repo, run_branch):
        raise GitOpsError(
            f"run branch {run_branch!r} does not exist — call ensure_run_branch first."
        )
    if _branch_exists(repo, task_branch):
        raise GitOpsError(
            f"task branch {task_branch!r} already exists — leftover from a previous "
            f"failed run? Inspect it and `git branch -D {task_branch}` to retry."
        )

    # Defensive: anchor to the run branch's tip regardless of current HEAD.
    _git(repo, "checkout", run_branch)
    _git(repo, "checkout", "-b", task_branch)
    return task_branch


def has_new_commits_since(repo: Path, base_sha: str) -> bool:
    """True if HEAD has any commits not reachable from `base_sha`.

    Used by Executor to detect Aider's `no_changes` case: if Aider exited 0
    but HEAD == base_sha, it didn't actually do anything.
    """
    result = _git(repo, "rev-list", "--count", f"{base_sha}..HEAD")
    return int(result.stdout.strip()) > 0


def diff_files_since(repo: Path, base_sha: str) -> list[Path]:
    """Return repo-relative paths of files changed in commits since base_sha.

    Includes adds/modifies/deletes/renames — anything `git diff --name-only`
    would show. Returns [] if no commits since base.
    """
    if not has_new_commits_since(repo, base_sha):
        return []
    result = _git(repo, "diff", "--name-only", f"{base_sha}..HEAD")
    return [Path(line) for line in result.stdout.splitlines() if line.strip()]


def squash_task_commits(repo: Path, *, base_sha: str, message: str) -> None:
    """Collapse all commits made since base_sha into a single commit.

    The mechanism is `git reset --soft <base>` (keeps the tree, drops the
    history) followed by `git commit -m <message>`. Working tree is unchanged
    by design — only commit history is rewritten. Safe because the task branch
    has not been pushed and is not shared.

    Raises GitOpsError if there's nothing to squash (caller bug — Executor
    should only call this on success path, after confirming new commits exist).
    """
    if not has_new_commits_since(repo, base_sha):
        raise GitOpsError(
            f"no commits to squash since {base_sha} — caller invoked squash on a "
            f"branch with no work. This is a usage bug, not a git error."
        )
    _git(repo, "reset", "--soft", base_sha)
    _git(repo, "commit", "-m", message)


def merge_task_into_run(repo: Path, run_id: str, task_id: str) -> None:
    """Merge the task branch into the run branch with --no-ff, leave HEAD on run.

    --no-ff guarantees a merge commit even when fast-forward would be possible.
    The merge commit is the visible "task X integrated into run Y" marker —
    valuable for review and for future Documentalist parsing.

    On any merge conflict (which shouldn't happen in MVP — sequential tasks,
    each on its own branch off the run tip) we let the CalledProcessError
    propagate via GitOpsError. Conflict resolution belongs to Stage 9 parallel.
    """
    run_branch = run_branch_name(run_id)
    task_branch = task_branch_name(run_id, task_id)

    if not _branch_exists(repo, run_branch):
        raise GitOpsError(f"run branch {run_branch!r} does not exist")
    if not _branch_exists(repo, task_branch):
        raise GitOpsError(f"task branch {task_branch!r} does not exist")

    _git(repo, "checkout", run_branch)
    try:
        _git(
            repo,
            "merge",
            "--no-ff",
            "-m",
            f"merge: integrate {task_id} into run {run_id}",
            task_branch,
        )
    except subprocess.CalledProcessError as e:
        raise GitOpsError(
            f"failed to merge {task_branch!r} into {run_branch!r}: "
            f"{e.stderr or e.stdout}"
        ) from e
