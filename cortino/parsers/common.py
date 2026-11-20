"""Helpers shared by all parsers (PBIP now; BIM/PBIT/Tableau later)."""

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cortino.spec.models import (
    FieldRef,
    Measure,
    Relationship,
    RlsRole,
    Source,
    Table,
    Unknown,
)

VISUAL_TYPES = {
    "card": "card",
    "clusteredBarChart": "bar",
    "lineChart": "line",
    "tableEx": "table",
    "slicer": "slicer",
}

# PBIR query-expression aggregation function codes.
AGGREGATIONS = {0: "sum", 1: "avg", 2: "count", 3: "min", 4: "max", 5: "distinctcount"}


# Slicer `mode` -> spec filter operator; Basic, Dropdown (lists) and no mode pick members.
SLICER_OPERATORS = {
    None: "in", "Basic": "in", "Dropdown": "in",
    "Between": "between", "Before": "lte", "After": "gte", "Relative": "relative_date",
}  # fmt: skip


def slicer_mode(visual: dict) -> str | None:
    """The slicer's `mode` literal from a PBIR `visual` (or legacy `singleVisual`) object."""
    try:
        value = visual["objects"]["data"][0]["properties"]["mode"]["expr"]["Literal"]["Value"]
    except (KeyError, IndexError, TypeError):
        return None
    return value.strip("'") if isinstance(value, str) else None


def normalize_visual_type(original: str) -> str:
    return VISUAL_TYPES.get(original, "unknown")


def field_ref(field: dict[str, Any]) -> FieldRef | None:
    """Turn a PBIR projection `field` into a FieldRef; None when the shape is not understood."""
    try:
        return _field_ref(field)
    except (KeyError, TypeError, AttributeError):
        return None


def _field_ref(field: dict[str, Any]) -> FieldRef | None:
    aggregation = None
    if "Aggregation" in field:
        agg = field["Aggregation"]
        aggregation = AGGREGATIONS.get(agg.get("Function"), str(agg.get("Function")))
        field = agg["Expression"]
    for kind in ("Column", "Measure"):
        if kind in field:
            body = field[kind]
            return FieldRef(
                table=body["Expression"]["SourceRef"]["Entity"],
                name=body["Property"],
                kind=kind.lower(),
                aggregation=aggregation,
            )
    return None


def read_json(path: Path) -> tuple[str, Any]:
    """Read a JSON file written as UTF-8 or UTF-16 (LE/BE), with or without a BOM."""
    raw = path.read_bytes()
    if raw.startswith(b"\xef\xbb\xbf"):
        text = raw[3:].decode("utf-8")
    elif raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        text = raw.decode("utf-16")
    elif len(raw) > 1 and raw[1:2] == b"\x00":
        text = raw.decode("utf-16-le")
    elif len(raw) > 1 and raw[0:1] == b"\x00":
        text = raw.decode("utf-16-be")
    else:
        text = raw.decode("utf-8")
    return text, json.loads(text)


SQL_DB = re.compile(r'Sql\.Database\(\s*"([^"]*)"\s*,\s*"([^"]*)"')
SQL_ITEM = re.compile(r'\{\[Schema\s*=\s*"([^"]*)"\s*,\s*Item\s*=\s*"([^"]*)"\]\}')
M_STEP = re.compile(r'^\s*(?:#"[^"]*"|\w+)\s*=', re.MULTILINE)
MEASURE_REF = re.compile(r"(?<![\w'\]])\[([^\]]+)\]")


@dataclass
class ModelPart:
    sources: list[Source] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    relationships: list[Relationship] = field(default_factory=list)
    measures: list[Measure] = field(default_factory=list)
    rls_roles: list[RlsRole] = field(default_factory=list)
    unknowns: list[Unknown] = field(default_factory=list)


def source_from_m(table: str, m_text: str) -> Source:
    db = SQL_DB.search(m_text)
    if not db:
        return Source(id=table, kind="other", query=m_text)
    item = SQL_ITEM.search(m_text)
    # Anything beyond Source + navigation (more steps, a Query option) keeps the raw M.
    plain = bool(item) and len(M_STEP.findall(m_text)) <= 2 and "Query" not in m_text
    return Source(
        id=table,
        kind="sqlserver" if plain else "other",
        server=db.group(1),
        database=db.group(2),
        schema=item.group(1) if plain else None,
        table=item.group(2) if plain else None,
        query=None if plain else m_text,
    )


def fill_depends_on(measures: list[Measure]) -> None:
    """depends_on = measures referenced as [Name], in order of first appearance."""
    names = {m.name for m in measures}
    for measure in measures:
        found = MEASURE_REF.findall(measure.expression)
        measure.depends_on = list(dict.fromkeys(n for n in found if n in names))
