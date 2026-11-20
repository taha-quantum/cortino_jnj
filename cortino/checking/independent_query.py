import json
import re
from pathlib import Path

import sqlglot
from sqlglot import exp

from cortino.llm import LLM
from cortino.plan.models import ColumnMapping, SourceMapping
from cortino.planning.planner import _tools
from cortino.spec.models import DashboardSpec, Measure, Visual
from cortino.tools.source_catalog import SourceCatalog

_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "independent_query.md"
_FENCE = re.compile(r"```(?:sql)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


class IndependentQueryError(Exception):
    pass


def _select_error(sql: str, param: str | None) -> str | None:
    try:
        stmts = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None and not isinstance(s, exp.Semicolon)]
    except sqlglot.errors.SqlglotError as e:
        return f"the SQL does not parse: {e}"
    if len(stmts) != 1 or not isinstance(stmts[0], exp.Query):
        return "the reply must be a single SELECT"
    used = {p.name for p in stmts[0].find_all(exp.Placeholder)}
    if param and used != {param}:
        return f"the SQL must use exactly the parameter ${param}, uses {sorted(used)}"
    return None


def write_independent_query(
    visual: Visual,
    spec: DashboardSpec,
    source_map: list[SourceMapping],
    column_map: list[ColumnMapping],
    measures_original: list[Measure],
    catalog: SourceCatalog,
    llm: LLM,
    bound: tuple[str, str] | None = None,
) -> str:
    """One SELECT for what `visual` shows, written by the `checker` role from the original
    dashboard only: never the plan's SQL, views or any builder file. With `bound` (a warehouse
    column, a parameter name) the query keeps only rows where that column equals `$param`."""
    parts = [
        f"Visual:\n{visual.model_dump_json(include={'id', 'type', 'encodings', 'sort', 'top_n', 'filters'})}",
        f"Measures (original expressions):\n{json.dumps([m.model_dump() for m in measures_original])}",
        f"Relationships:\n{json.dumps([r.model_dump() for r in spec.relationships])}",
        "Table map:\n" + json.dumps([{"spec_table": m.spec_table, "target_table": m.target_table} for m in source_map]),
        f"Column map:\n{json.dumps([m.model_dump() for m in column_map])}",
    ]  # fmt: skip
    if bound:
        parts.append(f"Filter: keep only rows where the warehouse column {bound[0]} equals the parameter ${bound[1]}.")
    prompt = "\n\n".join(parts)
    system, tools = _PROMPT.read_text(encoding="utf-8"), _tools(catalog)
    current = prompt
    for _ in range(2):
        reply = llm.text("checker", system, current, tools)
        fenced = _FENCE.search(reply)
        sql = (fenced.group(1) if fenced else reply).strip()
        error = _select_error(sql, bound[1] if bound else None)
        if error is None:
            return sql
        current = f"{prompt}\n\nYour previous reply was rejected: {error}\nReply with a single SELECT only."
    raise IndependentQueryError(f"independent query rejected twice: {error}")
