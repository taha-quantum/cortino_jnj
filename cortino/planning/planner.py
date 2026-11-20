import json
from dataclasses import asdict
from pathlib import Path

import sqlglot
from sqlglot import exp

from cortino import config
from cortino.llm import LLM
from cortino.plan.models import MeasureTranslation, Plan, Unsupported
from cortino.planning.simple_measures import translate_simple
from cortino.planning.source_mapping import map_columns, map_sources
from cortino.spec.models import DashboardSpec
from cortino.tools.source_catalog import SourceCatalog

_PKG = Path(__file__).resolve().parent.parent
_SKILLS = ("chart_mapping", "platform_conventions")
_TABLEAU = ("twb", "twbx")
RLS_FALLBACK = "needs platform decision — not rebuilt"
UNKNOWN_FALLBACK = "not rebuilt — review manually"
VISUAL_FALLBACK = "closest standard chart: table"


class PlanError(Exception):
    pass


def _system(spec: DashboardSpec) -> str:
    """The planner prompt plus the skills: the calculation skill of the source's language only."""
    calcs = "tableau_calc_to_sql" if spec.source_format in _TABLEAU else "dax_to_sql"
    parts = [(_PKG / "prompts" / "planner.md").read_text(encoding="utf-8")]
    parts += [(_PKG / "skills" / f"{s}.md").read_text(encoding="utf-8") for s in (calcs, *_SKILLS)]
    return "\n\n".join(parts)


def _tools(catalog: SourceCatalog) -> list:
    from strands import tool

    @tool
    def list_sources() -> list[str]:
        """List the warehouse tables as schema.table."""
        return catalog.list_sources()

    @tool
    def describe_table(table: str) -> str:
        """Columns, row count and five sample rows of one warehouse table (schema.table), as JSON."""
        return json.dumps(asdict(catalog.describe_table(table)), default=str)

    @tool
    def run_readonly_sql(sql: str) -> str:
        """Run one read-only SELECT on the warehouse (row limit and timeout apply); rows as JSON."""
        return json.dumps(catalog.run_readonly_sql(sql), default=str)

    return [list_sources, describe_table, run_readonly_sql]


def _code_unsupported(spec: DashboardSpec, chart_map: dict) -> list[Unsupported]:
    """Unknowns, RLS roles and visuals with no standard chart: these never vanish."""
    items = [Unsupported(item=u.where, reason=u.what, fallback=UNKNOWN_FALLBACK) for u in spec.unknowns]
    items += [
        Unsupported(item=f"rls:{r.name}", reason=f"row-level security on {r.table}: {r.filter_expression}",
                    fallback=RLS_FALLBACK)
        for r in spec.rls_roles
    ]  # fmt: skip
    seen = {u.item for u in items}  # a visual already listed as an unknown keeps that entry
    for p in spec.pages:
        for v in p.visuals:
            if chart_map.get(v.type) == "unsupported" and f"visual:{v.id}" not in seen:
                items.append(Unsupported(item=f"visual:{v.id}", fallback=VISUAL_FALLBACK,
                                         reason=f"no standard chart for {v.original_type or v.type}"))  # fmt: skip
    return items


def _sql_errors(where: str, sql: str, allowed: set[str]) -> list[str]:
    try:
        stmts = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None and not isinstance(s, exp.Semicolon)]
    except sqlglot.errors.SqlglotError as e:
        return [f"{where}: SQL does not parse: {e}"]
    if len(stmts) != 1 or not isinstance(stmts[0], exp.Query):
        return [f"{where}: SQL must be a single SELECT"]
    ctes = {c.alias_or_name.lower() for c in stmts[0].find_all(exp.CTE)}
    refs = {".".join(p for p in (t.catalog, t.db, t.name) if p).lower() for t in stmts[0].find_all(exp.Table)}
    bad = sorted(refs - ctes - allowed)
    return [f"{where}: SQL reads tables outside the plan: {', '.join(bad)}"] if bad else []


def _simple(spec: DashboardSpec, column_map) -> dict[str, MeasureTranslation]:
    return {m.name: t for m in spec.measures if (t := translate_simple(m, column_map, spec.measures))}


def _build(spec, catalog, chart_map, reply: Plan, base_map, fixed, questions, drafted) -> tuple[Plan, list[str]]:
    """Merge the LLM reply onto the fixed parts and return (plan, semantic errors)."""
    errors: list[str] = []
    tables = set(catalog.list_sources())
    by_table = {m.spec_table: m for m in base_map}
    for m in reply.source_map:
        if m.spec_table not in by_table:
            errors.append(f"source_map: '{m.spec_table}' is not a spec table")
        elif m.spec_table in fixed:
            if m.target_table != by_table[m.spec_table].target_table:
                errors.append(f"source_map: the mapping for '{m.spec_table}' is fixed and must not change")
        elif m.target_table is not None and m.target_table not in tables:
            errors.append(f"source_map: '{m.target_table}' is not a warehouse table")
        else:
            by_table[m.spec_table] = m
    source_map = list(by_table.values())
    column_map = map_columns(spec, source_map, catalog)
    simple = _simple(spec, column_map)  # follows the final source_map, so measures never read a stale table

    measures: dict[str, MeasureTranslation] = dict(simple)
    names = [m.name for m in spec.measures]
    for t in reply.measures:
        if t.name in simple:  # the code translation wins; only one the LLM was shown is an error to change
            shown = drafted.get(t.name)
            if shown and shown.sql_expression == simple[t.name].sql_expression != t.sql_expression:
                errors.append(f"measure '{t.name}' is a fixed simple translation and must not change")
        elif t.name not in names:
            errors.append(f"measure '{t.name}' is not a spec measure")
        else:
            if not t.test_query:
                errors.append(f"measure '{t.name}': a hard measure needs a test_query")
            measures[t.name] = t.model_copy(update={"difficulty": "hard", "flagged": True})
    errors += [f"measure '{n}' has no translation" for n in names if n not in measures]

    mapped = {m.target_table.lower() for m in source_map if m.target_table}
    allowed = mapped | {v.name.lower() for v in reply.views}
    for v in reply.views:
        errors += _sql_errors(f"view '{v.name}'", v.sql, allowed)
    for d in reply.datasets:
        errors += _sql_errors(f"dataset '{d.id}'", d.sql, allowed)
    for m in measures.values():  # a test query runs on the warehouse itself: no views
        if m.difficulty == "hard" and m.test_query:
            errors += _sql_errors(f"measure '{m.name}' test_query", m.test_query, mapped)

    dataset_ids = {d.id for d in reply.datasets}
    visuals = {v.id: v for p in spec.pages for v in p.visuals}
    for e in reply.visual_map:
        if e.visual_id not in visuals:
            errors.append(f"visual_map: '{e.visual_id}' is not a spec visual")
    for v in visuals.values():
        if v.type in ("slicer", "unknown"):
            continue
        expected = chart_map[v.type] if chart_map[v.type] != "unsupported" else "table"
        entries = [e for e in reply.visual_map if e.visual_id == v.id]
        if len(entries) != 1:
            errors.append(f"visual '{v.id}' needs exactly one visual_map entry, found {len(entries)}")
            continue
        if entries[0].component != expected:
            errors.append(f"visual '{v.id}': component '{entries[0].component}' should be '{expected}'")
        if entries[0].dataset_id not in dataset_ids:
            errors.append(f"visual '{v.id}': unknown dataset '{entries[0].dataset_id}'")

    unsupported = _code_unsupported(spec, chart_map)
    unsupported += [u for u in reply.unsupported if u.item not in {x.item for x in unsupported}]
    plan = Plan(
        plan_version="0.1",
        source_map=source_map,
        column_map=column_map,
        measures=[measures[n] for n in names if n in measures],
        views=reply.views,
        datasets=reply.datasets,
        visual_map=reply.visual_map,
        unsupported=unsupported,
        questions=questions + [q for q in reply.questions if q not in questions],
        tasks=reply.tasks,
    )
    return plan, errors


def make_plan(
    spec: DashboardSpec,
    catalog: SourceCatalog,
    llm: LLM,
    chart_map: dict,
    change_request: str | None = None,
    previous: Plan | None = None,
    high_confidence: float | None = None,
) -> Plan:
    if high_confidence is None:
        high_confidence = config.load("thresholds")["high_confidence"]
    base_map, questions = map_sources(spec, catalog, high_confidence)
    fixed = {m.spec_table for m in base_map if m.method == "explicit" or m.confidence >= high_confidence}
    if previous is not None:  # keep earlier non-fixed choices unless the reply changes them
        earlier = {m.spec_table: m for m in previous.source_map if m.spec_table not in fixed}
        base_map = [earlier.get(m.spec_table, m) for m in base_map]
    column_map = map_columns(spec, base_map, catalog)
    simple = _simple(spec, column_map)
    draft = Plan(
        plan_version="0.1", source_map=base_map, column_map=column_map,
        measures=list(simple.values()), unsupported=_code_unsupported(spec, chart_map),
        questions=questions,
    )  # fmt: skip
    prompt = (
        f"Dashboard spec:\n{spec.model_dump_json(by_alias=True)}\n\n"
        f"Draft plan (the fixed parts are filled in):\n{draft.model_dump_json()}"
    )
    if change_request is not None and previous is not None:
        prompt += f"\n\nPrevious plan:\n{previous.model_dump_json()}\n\nChange request:\n{change_request}"
    system, tools = _system(spec), _tools(catalog)
    current = prompt
    for _ in range(2):
        reply = llm.structured("planner", system, current, Plan, tools)
        plan, errors = _build(spec, catalog, chart_map, reply, base_map, fixed, questions, simple)
        if not errors:
            return plan
        listed = "\n".join(f"- {e}" for e in errors)
        current = f"{prompt}\n\nYour previous plan was rejected:\n{listed}\nFix these and reply with the complete plan."
    raise PlanError(f"planner: plan failed validation twice: {'; '.join(errors)}")
