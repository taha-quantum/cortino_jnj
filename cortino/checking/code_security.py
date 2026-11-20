import re
from pathlib import Path

import sqlglot
from pydantic import ValidationError
from sqlglot import exp

from cortino.checking import CheckResult, Finding
from cortino.checking.files import dataset_files, read_text
from cortino.plan.models import Plan
from cortino.platform.config_schema import DashboardConfig
from cortino.spec.models import DashboardSpec

# Python or Jinja format markers inside a string literal ('%s' only as a bare token, so LIKE '%south%' is fine).
_MARKER = re.compile(r"\{|%\(\w+\)s|%s(?!\w)")


def _building(tree: exp.Expression) -> str | None:
    """What in the parsed statement builds SQL by string handling, if anything."""
    for n in tree.find_all(exp.DPipe, exp.Add, exp.Concat):
        ops = [n.args.get("this"), n.args.get("expression"), *n.expressions]
        if any(o is not None and o.find(exp.Placeholder) for o in ops):
            return n.sql(dialect="duckdb")
    for st in tree.find_all(exp.Struct):  # duckdb reads `{x}` / `{{ x }}` as a struct of bare entries
        if any(isinstance(e, exp.Column | exp.Struct) for e in st.expressions):
            return st.sql(dialect="duckdb")
    for lit in tree.find_all(exp.Literal):
        if lit.is_string and (m := _MARKER.search(lit.name)):
            return m.group(0)
    return None


def _all_filters(spec: DashboardSpec):
    yield from spec.filters
    for p in spec.pages:
        yield from p.filters
        for v in p.visuals:
            yield from v.filters


def check_code_security(dashboard_dir: Path, spec: DashboardSpec, plan: Plan) -> CheckResult:
    """Datasets are single SELECTs whose filters are only declared bound parameters; views.sql
    holds only CREATE VIEW; config filters reference only declared parameters."""
    findings: list[Finding] = []
    visual_of = {d.id: (d.visual_ids or [None])[0] for d in plan.datasets}

    def add(owner, message, file, visual_id=None, **evidence):
        findings.append(Finding(check="code_security", owner=owner, visual_id=visual_id, message=message,
                                evidence={"file": file, **evidence}))  # fmt: skip

    def parse(text: str, file: str, vid: str | None = None) -> list[exp.Expression] | None:
        try:
            return [s for s in sqlglot.parse(text, read="duckdb") if s is not None and not isinstance(s, exp.Semicolon)]
        except sqlglot.errors.SqlglotError as e:
            add("data", f"{file} does not parse: {e}", file, vid)
            return None

    views = read_text(dashboard_dir / "views.sql")
    for s in (parse(views, "views.sql") or []) if views else []:
        if not (isinstance(s, exp.Create) and s.kind == "VIEW"):
            add("data", "views.sql must hold only CREATE VIEW statements", "views.sql")

    try:
        cfg = DashboardConfig.model_validate_json(read_text(dashboard_dir / "dashboard.json"))
    except ValidationError as e:
        cfg = None
        add("ui", f"dashboard.json is missing or invalid: {e}", "dashboard.json")
    declared = {d.id: {p.name for p in d.params} for d in (cfg.datasets if cfg else plan.datasets)}
    filter_values = {str(x) for f in _all_filters(spec) for x in f.values}

    for ds_id, path in dataset_files(dashboard_dir, plan).items():
        rel, vid = f"datasets/{path.name}", visual_of.get(ds_id)
        text = read_text(path)
        if not text:
            add("data", f"{rel} is missing or empty", rel, vid, dataset=ds_id)
            continue
        stmts = parse(text, rel, vid)
        if stmts is None:
            continue
        if len(stmts) != 1 or not isinstance(stmts[0], exp.Query):
            add("data", f"dataset '{ds_id}' must be a single SELECT", rel, vid, dataset=ds_id)
            continue
        used = {p.name for p in stmts[0].find_all(exp.Placeholder)}
        has = declared.get(ds_id, set())
        if used - has:
            add("data", f"dataset '{ds_id}' uses undeclared params {sorted(used - has)}", rel, vid, dataset=ds_id)
        if has - used:
            add("data", f"dataset '{ds_id}' declares params {sorted(has - used)} it never uses", rel, vid, dataset=ds_id)
        if near := _building(stmts[0]):
            add("data", f"dataset '{ds_id}' builds SQL by string handling near {near!r}", rel, vid, dataset=ds_id)
        for lit in stmts[0].find_all(exp.Literal):
            if lit.is_string and ("$" in lit.name or lit.name in filter_values):
                add("data", f"dataset '{ds_id}' hard-codes a filter value or parameter in the literal {lit.name!r}",
                    rel, vid, dataset=ds_id)  # fmt: skip

    if cfg:
        charts = {c.id: c for p in cfg.pages for c in p.charts}
        for f in cfg.filters:
            for cid in charts if f.applies_to == "all" else f.applies_to:
                ds = charts[cid].dataset if cid in charts else None
                if f.param not in declared.get(ds, set()):
                    add("ui", f"filter '{f.id}' uses param '{f.param}' not declared by the dataset of chart '{cid}'",
                        "dashboard.json", cid, filter=f.id)  # fmt: skip

    return CheckResult(name="code_security", passed=not findings, findings=findings)
