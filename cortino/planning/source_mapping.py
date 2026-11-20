import re
from itertools import pairwise

from cortino import config
from cortino.plan.models import ColumnMapping, SourceMapping
from cortino.spec.models import Column, DashboardSpec
from cortino.tools.source_catalog import SourceCatalog

_FAMILIES = {
    "numeric": {
        "int", "int8", "int16", "int32", "int64", "tinyint", "smallint", "integer", "bigint",
        "hugeint", "decimal", "numeric", "double", "float", "real", "number", "currency",
    },
    "text": {"string", "varchar", "nvarchar", "char", "nchar", "bpchar", "text"},
    "temporal": {"datetime", "datetime2", "date", "timestamp", "timestamptz", "time"},
    "boolean": {"boolean", "bool"},
}  # fmt: skip


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", name.lower())


def _family(type_name: str) -> str | None:
    base = re.sub(r"\(.*\)", "", type_name).strip().lower()
    for family, names in _FAMILIES.items():
        if base in names:
            return family
    return None


def _spec_cols(columns: list[Column]) -> set[str]:
    return {_norm(c.source_column or c.name) for c in columns}


def _jaccard(a: set, b: set) -> float:
    return len(a & b) / len(a | b) if a | b else 0.0


def _name_similarity(a: str, b: str) -> float:
    a, b = _norm(a), _norm(b)
    return _jaccard(set(pairwise(a)), set(pairwise(b)))


def map_sources(
    spec: DashboardSpec, catalog: SourceCatalog, high_confidence: float | None = None
) -> tuple[list[SourceMapping], list[str]]:
    if high_confidence is None:
        high_confidence = config.load("thresholds")["high_confidence"]
    sources = {s.id: s for s in spec.sources}
    candidates = {
        t: {_norm(n) for n, _ in catalog.describe_table(t).columns}
        for t in catalog.list_sources()
    }
    mappings: list[SourceMapping] = []
    questions: list[str] = []
    for table in spec.tables:
        source = sources.get(table.source_id) if table.source_id else None
        explicit = catalog.explicit_mapping(source) if source else None
        if explicit:
            mappings.append(SourceMapping(
                source_id=table.source_id, spec_table=table.name, target_table=explicit,
                confidence=1.0, method="explicit", evidence="configured source mapping",
            ))
            continue
        cols = _spec_cols(table.columns)
        scored = [
            (_jaccard(cols, c), _name_similarity(table.name, t), t)
            for t, c in candidates.items()
        ]
        scored = [s for s in scored if s[0] > 0]
        if not scored:
            mappings.append(SourceMapping(
                source_id=table.source_id, spec_table=table.name, target_table=None,
                confidence=0.0, method="overlap", evidence="no catalog table shares columns",
            ))
            questions.append(f"Which warehouse table holds '{table.name}'? No match was found.")
            continue
        score, _, best = max(scored)
        mappings.append(SourceMapping(
            source_id=table.source_id, spec_table=table.name, target_table=best,
            confidence=score, method="overlap",
            evidence=f"column-name Jaccard {score:.2f} with {best}",
        ))
        if score < high_confidence:
            questions.append(
                f"Is '{best}' the right table for '{table.name}'? Confidence is {score:.2f}."
            )
    return mappings, questions


def map_columns(
    spec: DashboardSpec, source_map: list[SourceMapping], catalog: SourceCatalog
) -> list[ColumnMapping]:
    by_table = {t.name: t for t in spec.tables}
    result: list[ColumnMapping] = []
    for m in source_map:
        if m.target_table is None or m.spec_table not in by_table:
            continue
        target = {_norm(n): (n, ty) for n, ty in catalog.describe_table(m.target_table).columns}
        for col in by_table[m.spec_table].columns:
            hit = target.get(_norm(col.source_column or col.name))
            if hit is None:
                continue
            fam = _family(col.data_type)
            result.append(ColumnMapping(
                spec_table=m.spec_table, spec_column=col.name,
                target_table=m.target_table, target_column=hit[0],
                types_compatible=fam is not None and fam == _family(hit[1]),
            ))
    return result
