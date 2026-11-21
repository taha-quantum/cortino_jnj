import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

from cortino import config


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    repo: Path


def _git(cwd: Path, *args: str) -> str:
    try:
        r = subprocess.run(
            ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True
        )
    except subprocess.CalledProcessError as e:
        raise RuntimeError(f"git {' '.join(args)} failed: {e.stderr.strip()}") from e
    return r.stdout.strip()


def slugify(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "dashboard"


def run_worktree(target_repo: Path, run_dir: Path, slug: str, run_id: str) -> Worktree:
    """The run's worktree and branch, without creating anything. Both paths are made absolute:
    `git -C <repo> worktree add <relative path>` resolves the path against the repo, not against
    the process's working directory, so a relative run_dir would put the worktree inside the
    target repo while Cortino writes (and commits) somewhere else."""
    return Worktree(
        Path(run_dir).resolve() / "worktree", f"cortino/{slug}-{run_id[:8]}", Path(target_repo).resolve()
    )


def _assert_own_worktree(wt: Worktree) -> None:
    """Refuse to touch git unless wt.path is itself the top of a git checkout. Otherwise git walks up
    to whatever repository encloses the folder (such as Cortino's own) and commits there."""
    try:
        top = _git(wt.path, "rev-parse", "--show-toplevel")
    except RuntimeError as e:
        raise RuntimeError(f"{wt.path} is not a git worktree: {e}") from e
    if Path(top).resolve() != wt.path.resolve():
        raise RuntimeError(f"{wt.path} is not a worktree of {wt.repo}: git resolves it to the repository at {top}")


def create_worktree(target_repo: Path, run_dir: Path, slug: str, run_id: str) -> Worktree:
    wt = run_worktree(target_repo, run_dir, slug, run_id)
    path, branch, repo = wt.path, wt.branch, wt.repo
    if path.exists():  # readers (verify, preview) only need the path; commit_all checks before writing
        return wt
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = bool(_git(repo, "branch", "--list", branch))
    if exists:
        _git(repo, "worktree", "add", str(path), branch)
    else:
        base = config.load("git")["base_branch"]  # not whatever the target repo has checked out
        _git(repo, "worktree", "add", str(path), "-b", branch, base)
    _assert_own_worktree(wt)
    return wt


def commit_all(wt: Worktree, message: str) -> str:
    _assert_own_worktree(wt)
    _git(wt.path, "add", "-A")
    if _git(wt.path, "status", "--porcelain"):
        _git(wt.path, "commit", "-m", message)
    return _git(wt.path, "rev-parse", "HEAD")


def remove_worktree(wt: Worktree, delete_branch: bool) -> None:
    """Remove the worktree (or prune its record when the folder is already gone) and optionally
    its branch, if the branch still exists."""
    if wt.path.exists():
        _git(wt.repo, "worktree", "remove", "--force", str(wt.path))
    else:
        _git(wt.repo, "worktree", "prune")
    if delete_branch and _git(wt.repo, "branch", "--list", wt.branch):
        _git(wt.repo, "branch", "-D", wt.branch)
