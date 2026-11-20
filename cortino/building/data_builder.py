import json
from pathlib import Path

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlglot import exp

from cortino.building import ask_valid
from cortino.building.datasets import dashboard_dir, write_all
from cortino.checking import Finding
from cortino.llm import LLM
from cortino.plan.models import Plan
from cortino.spec.models import DashboardSpec
from cortino.tools.repo_tools import RepoTools

_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "data_builder.md"


class DataFiles(BaseModel):
    """The data builder's fix reply: the full text of views.sql and of every dataset file."""

    model_config = ConfigDict(extra="forbid")
    views_sql: str
    datasets: dict[str, str]


def _statements(sql: str) -> list[exp.Expression]:
    return [s for s in sqlglot.parse(sql, read="duckdb") if s is not None and not isinstance(s, exp.Semicolon)]


def views_errors(sql: str) -> list[str]:
    try:
        stmts = _statements(sql)
    except sqlglot.errors.SqlglotError as e:
        return [f"views.sql does not parse: {e}"]
    if not all(isinstance(s, exp.Create) and s.kind == "VIEW" for s in stmts):
        return ["views.sql must hold only CREATE VIEW statements"]
    return []


def dataset_errors(ds_id: str, sql: str, params: list[str]) -> list[str]:
    try:
        stmts = _statements(sql)
    except sqlglot.errors.SqlglotError as e:
        return [f"dataset '{ds_id}' does not parse: {e}"]
    if len(stmts) != 1 or not isinstance(stmts[0], exp.Query):
        return [f"dataset '{ds_id}' must be a single SELECT"]
    used = {p.name for p in stmts[0].find_all(exp.Placeholder)}
    if used != set(params):
        return [f"dataset '{ds_id}' must use exactly its declared params {sorted(params)}, uses {sorted(used)}"]
    return []


def render_views(plan: Plan) -> str:
    out = []
    for v in plan.views:
        body = v.sql.strip().rstrip(";").rstrip()
        end = "\n;" if "--" in body.splitlines()[-1] else ";"  # a trailing line comment
        out.append(f"CREATE OR REPLACE VIEW {v.name} AS\n{body}{end}\n")
    return "\n".join(out)


def _fix(plan: Plan, tools: RepoTools, base: str, llm: LLM, fix: list[Finding]) -> dict[str, str]:
    current = {}
    for rel in ["views.sql"] + [f"datasets/{d.id}.sql" for d in plan.datasets]:
        try:
            current[rel] = tools.read_file(f"{base}/{rel}")
        except FileNotFoundError:
            current[rel] = "(missing)"
    prompt = (
        f"Findings to fix:\n{json.dumps([f.model_dump() for f in fix], indent=2)}\n\n"
        f"Plan:\n{plan.model_dump_json()}\n\n"
        "Current files:\n" + "\n".join(f"--- {rel} ---\n{text}" for rel, text in current.items())
    )

    def check(reply: DataFiles) -> list[str]:
        ids = [d.id for d in plan.datasets]
        if sorted(reply.datasets) != sorted(ids):
            return [f"datasets must be exactly {sorted(ids)}, got {sorted(reply.datasets)}"]
        errors = views_errors(reply.views_sql)
        for d in plan.datasets:
            errors += dataset_errors(d.id, reply.datasets[d.id], [p.name for p in d.params])
        return errors

    reply = ask_valid(llm, _PROMPT.read_text(encoding="utf-8"), prompt, DataFiles, check)
    return {"views.sql": reply.views_sql} | {f"datasets/{k}.sql": v for k, v in reply.datasets.items()}


def build_data(plan: Plan, spec: DashboardSpec, tools: RepoTools, llm: LLM, fix: list[Finding] | None) -> list[Path]:
    """Write views.sql and datasets/<id>.sql. The first build writes the plan's validated SQL;
    the LLM is asked only to fix findings."""
    base = dashboard_dir(spec)
    if fix:
        files = _fix(plan, tools, base, llm, fix)
    else:
        files = {"views.sql": render_views(plan)} | {f"datasets/{d.id}.sql": d.sql for d in plan.datasets}
    return write_all(tools, base, files)
