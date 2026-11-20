import re
from pathlib import Path

import sqlglot
from pydantic import ValidationError
from sqlglot import exp
from sqlglot.lineage import lineage

from cortino import config
from cortino.building.ui_builder import ui_skipped
from cortino.checking import CheckResult, Finding
from cortino.checking.files import dataset_files, read_text
from cortino.plan.models import Plan
from cortino.platform.config_schema import Chart, DashboardConfig
from cortino.spec.models import DashboardSpec, Visual

_AGGS = {
    "sum": exp.Sum, "avg": exp.Avg, "average": exp.Avg, "mean": exp.Avg, "min": exp.Min,
    "max": exp.Max, "count": exp.Count, "distinctcount": exp.Count, "count_distinct": exp.Count,
}  # fmt: skip
_DISTINCT = {"distinctcount", "count_distinct"}


def _snake(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


Agg = tuple[type, bool, frozenset[str] | None]  # function class, DISTINCT, argument columns (None: any)


def _aggs(expression: exp.Expression) -> set[Agg]:
    return {(type(n), isinstance(n.this, exp.Distinct), frozenset(c.name.lower() for c in n.find_all(exp.Column)))
            for n in expression.find_all(exp.AggFunc)}  # fmt: skip


def _satisfied(required: Agg, got: set[Agg]) -> bool:
    return any(g[:2] == required[:2] and (required[2] is None or g[2] == required[2]) for g in got)


def _outer_aggs(expression: exp.Expression) -> set[type]:
    """Classes of the outermost aggregations of one SELECT expression (not those nested inside
    another aggregation or a subquery)."""
    out = set()
    for n in expression.find_all(exp.AggFunc):
        p = n.parent
        while p is not None and p is not expression and not isinstance(p, exp.AggFunc | exp.Subquery | exp.Select):
            p = p.parent
        if p is None or p is expression:
            out.add(type(n))
    return out


def _traced(column: str, tree: exp.Expression, select: exp.Expression) -> tuple[set[Agg], set[type]]:
    """Aggregations behind an output column: in its SELECT expression and, through subqueries and
    CTEs (a FIXED LOD joined back), in the expressions it reads. Also the outermost aggregation
    classes at each of those levels, so a re-aggregation (MAX of SUMs) can be checked."""
    try:
        nodes = [n.expression for n in lineage(column, tree, None, dialect="duckdb").walk()]
    except sqlglot.errors.SqlglotError:
        nodes = []
    levels = [select, *nodes]
    return set().union(*(_aggs(n) for n in levels)), set().union(*(_outer_aggs(n) for n in levels))


def _parse(sql: str) -> exp.Expression | None:
    try:
        return sqlglot.parse_one(sql, read="duckdb")
    except sqlglot.errors.SqlglotError:
        return None


def check_chart_alignment(dashboard_dir: Path, spec: DashboardSpec, plan: Plan) -> CheckResult:
    """Each mapped visual's chart must match the spec: component, encodings, aggregation, sort,
    top_n and filters. Deviations the plan lists as unsupported are allowed."""
    findings: list[Finding] = []

    def add(owner, vid, message, fixable=True, **evidence):
        findings.append(Finding(check="chart_alignment", owner=owner, visual_id=vid, message=message,
                                evidence=evidence, fixable=fixable))  # fmt: skip

    try:
        cfg = DashboardConfig.model_validate_json(read_text(dashboard_dir / "dashboard.json"))
    except ValidationError as e:
        add("ui", None, f"dashboard.json is missing or invalid: {e}", file="dashboard.json")
        return CheckResult(name="chart_alignment", passed=False, findings=findings)

    chart_map = config.load("chart_map")["chart_map"]
    visuals = {v.id: v for p in spec.pages for v in p.visuals}
    charts: dict[str, Chart] = {c.id: c for p in cfg.pages for c in p.charts}
    measures = {m.name: m for m in plan.measures}
    targets = {(m.spec_table, m.spec_column): m.target_column for m in plan.column_map}
    allowed = {u.item for u in plan.unsupported} | {i["item"] for i in ui_skipped(plan, spec)}  # skips build_report records
    files = dataset_files(dashboard_dir, plan)

    for m in plan.visual_map:
        v: Visual | None = visuals.get(m.visual_id)
        chart = charts.get(m.visual_id)
        if v is None or chart is None:
            add("ui", m.visual_id, f"no chart for visual '{m.visual_id}' in dashboard.json", file="dashboard.json")
            continue
        expected = chart_map.get(v.type, "unsupported")
        expected = "table" if expected == "unsupported" else expected
        waived = any(u.item == f"visual:{v.id}" and chart.component.lower() in u.fallback.lower() for u in plan.unsupported)
        if chart.component != expected and not waived:
            add("ui", v.id, f"chart '{v.id}' is a {chart.component}, the spec's {v.type} maps to {expected}",
                False, file="dashboard.json", expected=expected, got=chart.component)  # fmt: skip

        tree = _parse(read_text(files[chart.dataset])) if chart.dataset in files else None
        selects = {s.alias_or_name: s for s in tree.selects} if isinstance(tree, exp.Query) else {}
        columns = list(selects)
        enc = chart.encodings
        used = {c for c in (enc.x, enc.series, enc.value, *(enc.y or [])) if c}
        for e in v.encodings:
            col = _snake(e.field.name)
            if e.role not in ("x", "y", "series", "values", "category"):
                continue
            if f"encoding:{v.id}.{e.field.name}" in allowed:
                continue
            if col not in columns:
                add("data", v.id, f"dataset '{chart.dataset}' returns no column '{col}' for the spec's {e.role} '{e.field.name}'",
                    file=f"datasets/{chart.dataset}.sql", dataset=chart.dataset, column=col)  # fmt: skip
                continue
            if (col != enc.series) if e.role == "series" else (col not in used):
                add("ui", v.id, f"chart '{v.id}' does not encode '{col}' as the spec's {e.role}",
                    False, file="dashboard.json", column=col, role=e.role)  # fmt: skip
            required: set[Agg] = set()
            if e.field.kind == "measure" and e.field.name in measures:
                expr = _parse(measures[e.field.name].sql_expression)
                required = _aggs(expr) if expr is not None else set()
            elif e.field.aggregation and e.field.aggregation.lower() in _AGGS:
                agg = e.field.aggregation.lower()
                target = targets.get((e.field.table, e.field.name))
                required = {(_AGGS[agg], agg in _DISTINCT, frozenset({target.lower()}) if target else None)}
            got, outer = _traced(col, tree, selects[col])
            shelf = _AGGS.get((e.field.aggregation or "").lower())
            reaggregate = {r[0] for r in required} | ({shelf} if shelf else set())
            if not all(_satisfied(r, got) for r in required) or (required and outer - reaggregate):
                add("data", v.id, f"dataset '{chart.dataset}' column '{col}' does not aggregate as '{e.field.name}' requires",
                    file=f"datasets/{chart.dataset}.sql", dataset=chart.dataset, column=col)  # fmt: skip

        sort = next((f"{_snake(s.field.name)} {s.direction}" for s in v.sort if _snake(s.field.name) in columns), None)
        if columns and chart.sort != sort:
            add("ui", v.id, f"chart '{v.id}' sort is {chart.sort!r}, the spec's is {sort!r}", False, file="dashboard.json")
        if chart.top_n != v.top_n:
            add("ui", v.id, f"chart '{v.id}' top_n is {chart.top_n}, the spec's is {v.top_n}", False, file="dashboard.json")

        where = {c.name for w in tree.find_all(exp.Where) for c in w.find_all(exp.Column)} if tree else set()
        for f in v.filters:
            col = targets.get((f.field.table, f.field.name))
            if col is None or f"filter:{f.field.table}.{f.field.name}" in allowed:
                continue
            controls = [c for c in cfg.filters if c.field == col and (c.applies_to == "all" or v.id in c.applies_to)]
            if not controls and col not in where:
                add("ui", v.id, f"visual filter on '{f.field.name}' reaches chart '{v.id}' neither as a control nor in its dataset",
                    False, file="dashboard.json", column=col)  # fmt: skip

    return CheckResult(name="chart_alignment", passed=not findings, findings=findings)
