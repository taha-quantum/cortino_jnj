from dataclasses import dataclass, field
from pathlib import Path

from pydantic import ValidationError

from cortino.building.data_builder import dataset_errors, views_errors
from cortino.checking import Finding
from cortino.plan.models import Plan
from cortino.platform.config_schema import DashboardConfig
from cortino.tools.repo_tools import RepoTools
from cortino.tools.warehouse import Warehouse, WarehouseError


@dataclass
class IntegrateResult:
    ok: bool
    failures: dict[str, list[Finding]] = field(default_factory=dict)


def integrate(wt: Path, plan: Plan, throwaway: Warehouse, dashboard_dir: str) -> IntegrateResult:
    """Parse every SQL file, apply views.sql to the throwaway copy, run each dataset with NULL
    params and LIMIT 1, and validate dashboard.json; findings go to their owner."""
    tools = RepoTools(wt)
    found: list[Finding] = []

    def add(owner, check, message, visual_id=None, **evidence):
        found.append(Finding(check=check, owner=owner, visual_id=visual_id, message=message, evidence=evidence))

    def read(rel: str) -> str | None:
        try:
            return tools.read_file(f"{dashboard_dir}/{rel}")
        except FileNotFoundError:
            return None

    views = read("views.sql")
    if views is None:
        add("data", "missing_file", "views.sql is missing", file="views.sql")
    else:
        for e in views_errors(views):
            add("data", "sql_parse", e, file="views.sql")
    sqls = {d.id: read(f"datasets/{d.id}.sql") for d in plan.datasets}
    for d in plan.datasets:
        vid, rel = (d.visual_ids or [None])[0], f"datasets/{d.id}.sql"
        if sqls[d.id] is None:
            add("data", "missing_file", f"{rel} is missing", vid, dataset=d.id, file=rel)
            continue
        for e in dataset_errors(d.id, sqls[d.id], [p.name for p in d.params]):
            add("data", "sql_parse", e, vid, dataset=d.id, file=rel)

    if not found and views.strip():
        try:
            throwaway.query(views)
        except WarehouseError as e:
            add("data", "views_apply", f"views.sql failed: {e}", file="views.sql")
    columns: dict[str, list[str]] = {}
    if not found:
        for d in plan.datasets:
            params = {p.name: None for p in d.params}
            try:
                throwaway.query(sqls[d.id], params, limit=1)
                columns[d.id] = [r["column_name"] for r in throwaway.query(f"DESCRIBE {sqls[d.id]}", params)]
            except WarehouseError as e:
                add("data", "dataset_run", f"dataset '{d.id}' failed: {e}", (d.visual_ids or [None])[0],
                    dataset=d.id, file=f"datasets/{d.id}.sql")  # fmt: skip

    text = read("dashboard.json")
    if text is None:
        add("ui", "config", "dashboard.json is missing", file="dashboard.json")
    else:
        try:
            cfg = DashboardConfig.model_validate_json(text)
        except ValidationError as e:
            add("ui", "config", f"dashboard.json is invalid: {e}", file="dashboard.json")
        else:
            got, wanted = sorted(d.id for d in cfg.datasets), sorted(d.id for d in plan.datasets)
            if got != wanted:
                add("ui", "config", f"dashboard.json datasets {got} do not match the plan's {wanted}",
                    file="dashboard.json")  # fmt: skip
            for c in (c for p in cfg.pages for c in p.charts if c.dataset in columns):
                e = c.encodings
                lost = [x for x in (e.x, e.series, e.value, *(e.y or [])) if x and x not in columns[c.dataset]]
                if lost:
                    add("data", "encoding_column",
                        f"chart '{c.id}' encodes {lost} but dataset '{c.dataset}' returns {columns[c.dataset]}",
                        c.id, dataset=c.dataset, file=f"datasets/{c.dataset}.sql")  # fmt: skip

    failures: dict[str, list[Finding]] = {}
    for f in found:
        failures.setdefault(f.owner, []).append(f)
    return IntegrateResult(ok=not found, failures=failures)
