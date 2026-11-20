import re

from cortino.plan.models import ColumnMapping, MeasureTranslation
from cortino.spec.models import Measure

_CALL = re.compile(r"^\s*([A-Za-z]+)\s*\((.*)\)\s*$", re.DOTALL)
_COLUMN = re.compile(r"^\s*(?:'([^']+)'|([A-Za-z_][\w ]*))\s*\[([^\]]+)\]\s*$")
_TABLE = re.compile(r"^\s*(?:'([^']+)'|([A-Za-z_]\w*))\s*$")
_MEASURE = re.compile(r"^\s*\[([^\]]+)\]\s*$")
_AGG = {
    "SUM": "SUM({})", "COUNT": "COUNT({})", "AVERAGE": "AVG({})",
    "MIN": "MIN({})", "MAX": "MAX({})", "DISTINCTCOUNT": "COUNT(DISTINCT {})",
}  # fmt: skip


def _split_args(text: str) -> list[str]:
    args, depth, cur = [], 0, ""
    for ch in text:
        if ch in "([":
            depth += 1
        elif ch in ")]":
            depth -= 1
        if ch == "," and depth == 0:
            args.append(cur)
            cur = ""
        else:
            cur += ch
    return [*args, cur]


def _target(table: str, column_map: list[ColumnMapping]) -> str | None:
    for c in column_map:
        if c.spec_table.lower() == table.lower():
            return c.target_table
    return None


def _column(arg: str, column_map: list[ColumnMapping]) -> tuple[str, str] | None:
    """Return (`target_table.target_column`, target_table) for a column reference."""
    m = _COLUMN.match(arg)
    if not m:
        return None
    table, col = (m.group(1) or m.group(2)).lower(), m.group(3).lower()
    for c in column_map:
        if c.spec_table.lower() == table and c.spec_column.lower() == col:
            return f"{c.target_table}.{c.target_column}", c.target_table
    return None


def _distinct(col: str) -> str:
    # DAX DISTINCTCOUNT counts BLANK as one value; SQL COUNT(DISTINCT) ignores NULL.
    return f"COUNT(DISTINCT {col}) + COALESCE(MAX(CASE WHEN {col} IS NULL THEN 1 ELSE 0 END), 0)"


def _sql(
    expr: str, home: str, column_map, all_measures, seen: frozenset
) -> tuple[str, str] | None:
    """Return (sql, target_table the expression aggregates) or None when not simple."""
    call = _CALL.match(expr)
    if not call:
        return None
    fn, inner = call.group(1).upper(), call.group(2)
    if fn in _AGG or fn == "DISTINCTCOUNTNOBLANK":
        ref = _column(inner, column_map)
        if ref is None:
            return None
        col, table = ref
        if fn == "DISTINCTCOUNT":
            return _distinct(col), table
        return _AGG.get(fn, "COUNT(DISTINCT {})").format(col), table
    if fn == "COUNTROWS":
        m = _TABLE.match(inner)
        name = (m.group(1) or m.group(2)) if m else None
        # Bare COUNT(*) has no table in it, so only the measure's home table is safe.
        if name is None or name.lower() != home.lower():
            return None
        table = _target(name, column_map)
        return ("COUNT(*)", table) if table else None
    if fn == "DIVIDE":
        args = _split_args(inner)
        if len(args) != 2:
            return None
        parts = []
        for arg in args:
            ref = _MEASURE.match(arg)
            if not ref or ref.group(1) in seen:
                return None
            dep = next((m for m in all_measures if m.name == ref.group(1)), None)
            if dep is None:
                return None
            r = _sql(dep.expression, dep.table, column_map, all_measures, seen | {dep.name})
            if r is None:
                return None
            parts.append(r)
        if parts[0][1] != parts[1][1]:
            return None
        return f"({parts[0][0]}) / NULLIF({parts[1][0]}, 0)", parts[0][1]
    return None


def translate_simple(
    measure: Measure, column_map: list[ColumnMapping], all_measures: list[Measure]
) -> MeasureTranslation | None:
    if measure.language != "dax":
        return None
    result = _sql(
        measure.expression, measure.table, column_map, all_measures, frozenset({measure.name})
    )
    if result is None:
        return None
    sql = result[0]
    return MeasureTranslation(
        name=measure.name, original=measure.expression, sql_expression=sql,
        difficulty="simple", test_query=None, flagged=False,
    )
