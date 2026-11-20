import re
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from cortino.building.worktree import Worktree


@dataclass(frozen=True)
class PrInfo:
    url: str
    number: int


class GitHost(Protocol):
    """Where the branch goes. There is deliberately no merge: a human merges."""

    def push(self, wt: Worktree) -> None: ...

    def reviewers(self, wt: Worktree) -> list[str]: ...

    def open_pr(
        self, wt: Worktree, title: str, body: str, base: str,
        labels: list[str], reviewers: list[str],
    ) -> PrInfo: ...  # fmt: skip


class LocalGitHost:
    """No remote, no network: writes the PR text next to the worktree."""

    def push(self, wt: Worktree) -> None:
        pass

    def reviewers(self, wt: Worktree) -> list[str]:
        return []

    def open_pr(self, wt, title, body, base, labels, reviewers) -> PrInfo:
        pr_md = wt.path.parent / "pr.md"
        pr_md.write_text(f"# {title}\n\n{body}", encoding="utf-8")
        return PrInfo(pr_md.resolve().as_uri(), 0)


def _codeowners(root: Path) -> list[str]:
    """Owners of the catch-all and dashboards/ rules in the first CODEOWNERS file found."""
    for rel in ("CODEOWNERS", ".github/CODEOWNERS", "docs/CODEOWNERS"):
        file = root / rel
        if file.is_file():
            owners: list[str] = []
            for line in file.read_text(encoding="utf-8").splitlines():
                parts = line.split("#", 1)[0].split()
                if len(parts) > 1 and parts[0] in ("*", "dashboards/", "/dashboards/"):
                    owners += [o.lstrip("@") for o in parts[1:] if o.startswith("@")]
            return list(dict.fromkeys(owners))
    return []


class GhCliGitHost:
    """Pushes with git and opens the PR with the gh CLI. `run` is injectable for tests."""

    def __init__(self, run: Callable = subprocess.run):
        self._run = run

    def _exec(self, argv: list[str], cwd: Path) -> str:
        r = self._run(argv, cwd=cwd, capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(argv[:2])} failed: {r.stderr.strip()}")
        return r.stdout

    def reviewers(self, wt: Worktree) -> list[str]:
        return _codeowners(wt.path)

    def push(self, wt: Worktree) -> None:
        self._exec(["git", "push", "-u", "origin", wt.branch], wt.path)

    def open_pr(self, wt, title, body, base, labels, reviewers) -> PrInfo:
        with tempfile.TemporaryDirectory() as tmp:
            body_file = Path(tmp) / "body.md"
            body_file.write_text(body, encoding="utf-8")
            argv = ["gh", "pr", "create", "--title", title, "--body-file", str(body_file),
                    "--base", base]  # fmt: skip
            for label in labels:
                argv += ["--label", label]
            for reviewer in reviewers:
                argv += ["--reviewer", reviewer]
            out = self._exec(argv, wt.path)
        m = re.search(r"https?://\S+/pull/(\d+)", out)
        if not m:
            raise RuntimeError(f"gh pr create printed no PR URL: {out.strip()}")
        return PrInfo(m.group(0), int(m.group(1)))
