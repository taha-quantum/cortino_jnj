from pathlib import Path

from cortino.plan.models import Plan


def read_text(path: Path) -> str:
    """The file's text, or "" when it is missing or unreadable (checks report that themselves)."""
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def dataset_files(dashboard_dir: Path, plan: Plan) -> dict[str, Path]:
    """Dataset id to file: the plan's datasets plus any other datasets/*.sql on disk."""
    files = {d.id: dashboard_dir / "datasets" / f"{d.id}.sql" for d in plan.datasets}
    for p in sorted((dashboard_dir / "datasets").glob("*.sql")):
        files.setdefault(p.stem, p)
    return files
