import json
import re
from pathlib import Path

import sqlglot
from pydantic import BaseModel, ConfigDict
from sqlglot import exp

from cortino.building import ask_valid
from cortino.building.datasets import dashboard_dir, write_all
from cortino.checking import Finding
from cortino.llm import LLM
from cortino.plan.models import Plan
from cortino.spec.models import DashboardSpec, Visual
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


_POWER_BI = {"pbip_tmdl", "pbip_bim", "pbit", "pbix"}
_CATEGORY = {"bar", "column", "pie", "donut"}
_SERIES = {"line", "area", "scatter"}


def _snake(name: str) -> str:  # as the UI builder names dataset columns after spec fields
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _output_columns(query: exp.Query) -> list[tuple[str, bool]] | None:
    """(name, is_measure) per output column of the top-level SELECT (the first branch of a set
    operation); None when a column cannot be told apart, such as `SELECT *`."""
    select = query
    while isinstance(select, exp.SetOperation):
        select = select.this
    if not isinstance(select, exp.Select):
        return None
    cols = []
    for e in select.expressions:
        inner = e.unalias()
        if isinstance(inner, exp.Star) or (isinstance(inner, exp.Column) and isinstance(inner.this, exp.Star)):
            return None
        cols.append((e.alias_or_name, inner.find(exp.AggFunc, exp.Window) is not None))
    return cols


def with_default_order(sql: str, visual: Visual | None, source_format: str) -> str:
    """`sql` with an ORDER BY appended when its top-level query has none, so a chart's rows come
    back in the same order every time. A sort in the spec comes first; otherwise the original
    tool's default: Power BI bar, column, pie and donut visuals by their measure, largest first;
    line, area and scatter by their x axis; then every dimension column ascending as a tiebreak.
    Output columns are referenced by position, which no join can make ambiguous. A query that
    already orders, uses LIMIT/OFFSET, or cannot be analysed is returned unchanged."""
    try:
        stmts = _statements(sql)
    except sqlglot.errors.SqlglotError:
        return sql
    if len(stmts) != 1 or not isinstance(stmts[0], exp.Query):
        return sql
    query = stmts[0]
    if any(query.args.get(k) for k in ("order", "limit", "offset", "fetch")):
        return sql
    cols = _output_columns(query)
    if not cols or all(m for _, m in cols):  # aggregates only (a card): a single row, nothing to order
        return sql
    names = [_snake(n) for n, _ in cols]
    measures = [i for i, (_, m) in enumerate(cols) if m]
    dims = [i for i, (_, m) in enumerate(cols) if not m]

    def position(field_name: str) -> int | None:
        return names.index(_snake(field_name)) if _snake(field_name) in names else None

    keys: list[tuple[int, bool]] = []  # (column index, descending)
    if visual is not None:
        for srt in visual.sort:
            if (i := position(srt.field.name)) is not None:
                keys.append((i, srt.direction == "desc"))
                break
        if not keys:
            roles = {}
            for e in visual.encodings:
                roles.setdefault(e.role, e.field.name)
            if visual.type in _CATEGORY and source_format in _POWER_BI and measures:
                wanted = [position(roles[r]) for r in ("y", "values") if r in roles]
                i = next((i for i in wanted if i in measures), measures[0])
                keys.append((i, True))
            elif visual.type in _SERIES and "x" in roles and (i := position(roles["x"])) is not None:
                keys.append((i, False))
    keys += [(i, False) for i in dims if i not in {k for k, _ in keys}]
    if not keys:  # e.g. a card's single aggregate row
        return sql
    body = sql.rstrip().rstrip(";").rstrip()
    return body + "\nORDER BY " + ", ".join(f"{i + 1}{' DESC' if desc else ''}" for i, desc in keys) + "\n"


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
    visuals = {v.id: v for p in spec.pages for v in p.visuals}
    for d in plan.datasets:  # the plan's SQL and the fixer's alike come back in a stable order
        rel = f"datasets/{d.id}.sql"
        visual = next((visuals[v] for v in d.visual_ids if v in visuals), None)
        files[rel] = with_default_order(files[rel], visual, spec.source_format)
    return write_all(tools, base, files)
