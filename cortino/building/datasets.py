from pathlib import Path

from cortino.building.worktree import slugify
from cortino.plan.models import Plan
from cortino.platform.config_schema import Dataset, DatasetParam
from cortino.spec.models import DashboardSpec
from cortino.tools.repo_tools import PathOutsideWorktree, RepoTools


def dashboard_dir(spec: DashboardSpec) -> str:
    return f"dashboards/{slugify(spec.name)}"


def write_all(tools: RepoTools, base: str, files: dict[str, str]) -> list[Path]:
    """Write each relative path under `base`; if any resolves outside `base`, nothing is written."""
    root = (tools.root / base).resolve()
    targets = {rel: (root / rel).resolve() for rel in files}
    for rel, target in targets.items():
        if root not in target.parents:
            raise PathOutsideWorktree(f"{rel!r} is outside {base}/")
    for rel, target in targets.items():
        tools.write_file(target.relative_to(tools.root).as_posix(), files[rel])
    return list(targets.values())


def register_datasets(plan: Plan) -> list[Dataset]:
    """The config `datasets` section, straight from the plan (assumption A3)."""
    return [
        Dataset(id=d.id, file=f"datasets/{d.id}.sql",
                params=[DatasetParam(name=p.name, type=p.type) for p in d.params])
        for d in plan.datasets
    ]  # fmt: skip
