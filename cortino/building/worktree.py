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
    """The run's worktree and branch, without creating anything."""
    return Worktree(Path(run_dir) / "worktree", f"cortino/{slug}-{run_id[:8]}", Path(target_repo))


def create_worktree(target_repo: Path, run_dir: Path, slug: str, run_id: str) -> Worktree:
    wt = run_worktree(target_repo, run_dir, slug, run_id)
    path, branch = wt.path, wt.branch
    if path.exists():
        return wt
    path.parent.mkdir(parents=True, exist_ok=True)
    exists = bool(_git(target_repo, "branch", "--list", branch))
    if exists:
        _git(target_repo, "worktree", "add", str(path), branch)
    else:
        base = config.load("git")["base_branch"]  # not whatever the target repo has checked out
        _git(target_repo, "worktree", "add", str(path), "-b", branch, base)
    return wt


def commit_all(wt: Worktree, message: str) -> str:
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
