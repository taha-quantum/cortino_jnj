import os
import subprocess

import pytest

from cortino.building.worktree import (
    commit_all,
    create_worktree,
    remove_worktree,
    slugify,
)
from cortino.tools.repo_tools import PathOutsideWorktree, RepoTools


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


def test_slugify():
    assert slugify("Sales  Overview!") == "sales-overview"
    assert slugify("???") == "dashboard"


def test_branch_name_format(tmp_git_repo, tmp_path):
    wt = create_worktree(tmp_git_repo, tmp_path / "run", "sales", "abcdef1234567890")
    assert wt.branch == "cortino/sales-abcdef12"
    assert wt.path == tmp_path / "run" / "worktree"
    assert _git(wt.path, "branch", "--show-current") == wt.branch


def test_create_is_idempotent_and_reattaches(tmp_git_repo, tmp_path):
    run = tmp_path / "run"
    wt = create_worktree(tmp_git_repo, run, "sales", "abcdef1234567890")
    assert create_worktree(tmp_git_repo, run, "sales", "abcdef1234567890") == wt
    remove_worktree(wt, delete_branch=False)
    assert not wt.path.exists()
    again = create_worktree(tmp_git_repo, run, "sales", "abcdef1234567890")
    assert again.path.exists()


def test_commit_all_and_remove(tmp_git_repo, tmp_path):
    wt = create_worktree(tmp_git_repo, tmp_path / "run", "s", "12345678aaaa")
    head = _git(wt.path, "rev-parse", "HEAD")
    assert commit_all(wt, "nothing") == head
    RepoTools(wt.path).write_file("dashboards/s/dashboard.json", "{}")
    sha = commit_all(wt, "add")
    assert len(sha) == 40 and sha != head
    remove_worktree(wt, delete_branch=True)
    assert wt.branch not in _git(tmp_git_repo, "branch", "--list")


def test_git_failure_has_stderr(tmp_path):
    with pytest.raises(RuntimeError, match="git"):
        create_worktree(tmp_path / "missing", tmp_path / "run", "s", "12345678")


def test_write_outside_worktree_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    tools = RepoTools(root)
    with pytest.raises(PathOutsideWorktree):
        tools.write_file("../x", "no")
    with pytest.raises(PathOutsideWorktree):
        tools.write_file(str(tmp_path / "abs.txt"), "no")
    assert not (tmp_path / "x").exists()


def test_symlink_escape_rejected(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    try:
        (root / "link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("symlinks unavailable")
    with pytest.raises(PathOutsideWorktree):
        RepoTools(root).write_file("link/x.txt", "no")


def test_read_write_list(tmp_path):
    t = RepoTools(tmp_path)
    t.write_file("a/b.txt", "hi")
    assert t.read_file("a/b.txt") == "hi"
    assert t.list_files() == ["a/b.txt"]
    assert t.list_files("a") == ["a/b.txt"]


@pytest.mark.skipif(os.name != "nt", reason="Windows extended-length paths")
def test_extended_length_prefix_is_still_inside(tmp_path, monkeypatch):
    r"""Windows realpath can return `\\?\C:\...` for a file whose folder another thread is creating
    (the data and UI builders write in parallel); that is the same path, not an escape."""
    real = os.path.realpath
    monkeypatch.setattr(os.path, "realpath",
                        lambda p, **kw: "\\\\?\\" + real(p, **kw) if str(p).endswith("v.sql") else real(p, **kw))  # fmt: skip
    t = RepoTools(tmp_path)
    t.write_file("d/v.sql", "SELECT 1")
    assert t.read_file("d/v.sql") == "SELECT 1"
    with pytest.raises(PathOutsideWorktree):
        t.write_file("../v.sql", "no")


def test_worktree_starts_from_the_configured_base_branch(tmp_git_repo, tmp_path):
    """Not from whatever the target repo has checked out (config/git.yaml base_branch: main)."""
    main = _git(tmp_git_repo, "rev-parse", "main")
    _git(tmp_git_repo, "checkout", "-q", "-b", "feature")
    (tmp_git_repo / "wip.txt").write_text("wip")
    _git(tmp_git_repo, "add", "-A")
    _git(tmp_git_repo, "commit", "-qm", "wip")
    wt = create_worktree(tmp_git_repo, tmp_path / "run", "sales", "abcdef1234567890")
    assert _git(wt.path, "rev-parse", "HEAD") == main
    assert not (wt.path / "wip.txt").exists()


def test_relative_paths_never_commit_to_the_enclosing_repo(tmp_git_repo, tmp_path, monkeypatch):
    """Cortino's process runs inside its own git repo with a relative runs/ folder and a relative
    target repo. The worktree must land in runs/, belong to the target repo, and commits must never
    reach the repository that encloses the working directory."""
    home = tmp_path / "cortino_home"
    home.mkdir()
    _git(home, "init", "-b", "main")
    _git(home, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "--allow-empty", "-m", "home")
    (home / "tracked.txt").write_text("uncommitted work", encoding="utf-8")
    monkeypatch.chdir(home)
    target = os.path.relpath(tmp_git_repo, home)

    wt = create_worktree(target, os.path.join("runs", "r1"), "sales", "abcdef1234567890")
    assert wt.path == (home / "runs" / "r1" / "worktree").resolve()
    assert wt.path.is_absolute() and wt.repo.is_absolute()
    assert not (tmp_git_repo / "runs").exists()  # not created inside the target repo
    (wt.path / "dashboard.json").write_text("{}", encoding="utf-8")
    commit_all(wt, "cortino: build v1")

    assert _git(home, "log", "--format=%s") == "home"  # the enclosing repo is untouched
    assert "dashboard.json" in _git(tmp_git_repo, "ls-tree", "-r", "--name-only", wt.branch)


def test_commit_refuses_a_folder_that_is_not_its_own_worktree(tmp_git_repo, tmp_path):
    wt = create_worktree(tmp_git_repo, tmp_path / "run", "sales", "abcdef1234567890")
    stray = type(wt)(tmp_git_repo / "sub", wt.branch, wt.repo)  # inside a repo, but not a worktree root
    stray.path.mkdir()
    with pytest.raises(RuntimeError, match="not a worktree"):
        commit_all(stray, "nope")
