from pathlib import Path

import sqlglot
from sqlglot import exp

from cortino.checking import CheckResult, Finding
from cortino.checking.files import dataset_files, read_text
from cortino.plan.models import Plan
from cortino.spec.models import DashboardSpec


def _statements(sql: str) -> list[exp.Expression]:
    return [s for s in sqlglot.parse(sql, read="duckdb") if s is not None and not isinstance(s, exp.Semicolon)]


def _name(t: exp.Table) -> str:
    return ".".join(p.name for p in (t.args.get("catalog"), t.args.get("db"), t.this) if p).lower()


def _visible_ctes(t: exp.Table) -> set[str]:
    """CTE names a table reference can see: those of enclosing WITH clauses, and for a reference
    inside a CTE body only the CTEs defined before it (a CTE reading its own name reads the table)."""
    names: set[str] = set()
    child, node = t, t.parent
    while node is not None:
        for v in node.args.values():
            if isinstance(v, exp.With) and v is not child:
                names |= {c.alias.lower() for c in v.expressions}
        if isinstance(node, exp.With):
            before = node.expressions[: node.expressions.index(child)] if child in node.expressions else []
            names |= {c.alias.lower() for c in before}
        child, node = node, node.parent
    return names


def _refs(tree: exp.Expression) -> tuple[set[str], list[str]]:
    """The physical tables a statement reads, and a description of every source that is not a
    plain named table (a table function, a file path)."""
    tables, odd = set(), []
    for t in tree.find_all(exp.Table):
        if not isinstance(t.this, exp.Identifier):
            odd.append(t.this.sql(dialect="duckdb"))
        elif any("." in p.name or "/" in p.name or "\\" in p.name for p in t.parts):
            odd.append(t.sql(dialect="duckdb"))
        elif _name(t) not in _visible_ctes(t):
            tables.add(_name(t))
    return tables, odd


def check_lineage(dashboard_dir: Path, spec: DashboardSpec, plan: Plan) -> CheckResult:
    """Every table the views and datasets read must resolve (through views) to a mapped table."""
    allowed = {m.target_table.lower() for m in plan.source_map if m.target_table}
    visual_of = {d.id: (d.visual_ids or [None])[0] for d in plan.datasets}
    findings: list[Finding] = []

    def add(message: str, file: str, visual_id: str | None = None, **evidence):
        findings.append(Finding(check="lineage", owner="data", visual_id=visual_id, message=message,
                                evidence={"file": file, **evidence}))  # fmt: skip

    def parse(text: str, file: str, visual_id: str | None = None) -> list[exp.Expression]:
        try:
            return _statements(text)
        except sqlglot.errors.SqlglotError as e:
            add(f"{file} does not parse: {e}", file, visual_id)
            return []

    views: dict[str, tuple[set[str], list[str]]] = {}
    views_text = read_text(dashboard_dir / "views.sql")
    for s in parse(views_text, "views.sql") if views_text else []:
        if isinstance(s, exp.Create) and s.kind == "VIEW":
            target = s.this.this if isinstance(s.this, exp.Schema) else s.this
            if isinstance(target, exp.Table):
                views[_name(target)] = _refs(s.expression)
            else:
                add("views.sql holds a CREATE VIEW whose name cannot be resolved", "views.sql")
    for name, (_, odd) in views.items():
        for o in odd:
            add(f"view {name} reads a non-table source {o}", "views.sql", source=o)

    def resolve(ref: str, seen: frozenset[str] = frozenset()) -> set[str]:
        """The base tables behind a reference; an unmatched name stays as it is."""
        hit = [v for v in views if v == ref or (("." not in ref) and v.rsplit(".", 1)[-1] == ref)]
        if hit:
            if hit[0] in seen:
                return set()
            return {b for r in views[hit[0]][0] for b in resolve(r, seen | {hit[0]})}
        if "." not in ref:
            same = [a for a in allowed if a.rsplit(".", 1)[-1] == ref]
            return set(same) if len(same) == 1 else {ref}
        return {ref}

    for ref in {r for refs, _ in views.values() for r in refs}:
        for base in resolve(ref):
            if base not in allowed:
                add(f"views.sql reads table {base}, which is not in the source map", "views.sql", table=base)

    for ds_id, path in dataset_files(dashboard_dir, plan).items():
        rel, vid = f"datasets/{path.name}", visual_of.get(ds_id)
        text = read_text(path)
        for s in parse(text, rel, vid) if text else []:
            refs, odd = _refs(s)
            for o in odd:
                add(f"{rel} reads a non-table source {o}", rel, vid, table=o)
            for base in sorted({b for r in refs for b in resolve(r)} - allowed):
                add(f"{rel} reads table {base}, which is not in the source map", rel, vid, table=base)

    return CheckResult(name="lineage", passed=not findings, findings=findings, evidence={"allowed": sorted(allowed)})
