from pathlib import Path

_EXTENDED = "\\\\?\\"  # Windows extended-length prefix: \\?\


def _real(path: Path) -> Path:
    """`path.resolve()` without the extended-length prefix that Windows realpath leaves on a path
    whose folder another thread is creating meanwhile (the data and UI builders run in parallel)."""
    p = str(path.resolve())
    return Path(p[len(_EXTENDED):]) if p.startswith(_EXTENDED) and not p.startswith(_EXTENDED + "UNC\\") else Path(p)


class PathOutsideWorktree(Exception):
    """A path resolved outside the worktree root."""


class RepoTools:
    """File tool that refuses any path outside `root` (builders' only write path)."""

    def __init__(self, root: Path):
        self.root = _real(Path(root))

    def _resolve(self, rel: str) -> Path:
        p = _real(self.root / rel)  # absolute rel replaces root; symlinks followed
        if p != self.root and self.root not in p.parents:
            raise PathOutsideWorktree(f"{rel!r} is outside the worktree")
        return p

    def read_file(self, rel: str) -> str:
        return self._resolve(rel).read_text(encoding="utf-8")

    def write_file(self, rel: str, text: str) -> None:
        p = self._resolve(rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")

    def list_files(self, rel: str = ".") -> list[str]:
        base = self._resolve(rel)
        return sorted(
            f.relative_to(self.root).as_posix()
            for f in base.rglob("*")
            if f.is_file() and ".git" not in f.relative_to(self.root).parts
        )
