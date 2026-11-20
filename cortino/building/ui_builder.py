import json
import re
from pathlib import Path

import sqlglot
from pydantic import BaseModel, ConfigDict, ValidationError
from sqlglot import exp

from cortino.building import ask_valid
from cortino.building.datasets import dashboard_dir, register_datasets, write_all
from cortino.checking import Finding
from cortino.llm import LLM
from cortino.plan.models import Dataset, Plan
from cortino.platform.config_schema import (
    Chart,
    DashboardConfig,
    Encodings,
    Filter,
    Page,
)
from cortino.spec.models import DashboardSpec, Visual
from cortino.tools.repo_tools import RepoTools

_PROMPT = Path(__file__).resolve().parent.parent / "prompts" / "ui_builder.md"
# Spec pages carry no size, so positions are taken on the default page as they are.
PAGE_W, PAGE_H = 1280, 720
RANGE_DOWNGRADE = "range filter rendered as a single-date filter"
_NUMERIC = re.compile(r"int|decimal|double|real|float|numeric|number", re.IGNORECASE)


class ChartText(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    number_format: str | None = None


class UiText(BaseModel):
    """The UI builder's reply: only titles and number formats; everything else is code."""

    model_config = ConfigDict(extra="forbid")
    charts: list[ChartText]


def _snake(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")


def _columns(d: Dataset) -> list[str]:
    if d.columns:
        return [name for name, _ in d.columns]
    return list(sqlglot.parse_one(d.sql, read="duckdb").named_selects)


def _encodings(v: Visual, component: str, d: Dataset) -> tuple[Encodings, list[dict]]:
    """The chart's encodings, and a skipped entry for every spec field the dataset lacks."""
    columns, enc, missing = _columns(d), {}, []
    for e in v.encodings:
        col = _snake(e.field.name)
        if col not in columns:
            if e.role in ("x", "y", "series", "values", "category"):
                missing.append({"item": f"encoding:{v.id}.{e.field.name}",
                                "reason": f"dataset {d.id} returns no column '{col}'",
                                "fallback": "left out of the chart — review manually"})  # fmt: skip
            continue
        if e.role == "series":
            enc["series"] = col
        elif e.role in ("x", "category") or (e.role == "values" and e.field.kind == "column"):
            enc.setdefault("x", col)
        elif e.role in ("y", "values"):
            if component == "card":
                enc.setdefault("value", col)
            else:
                enc.setdefault("y", []).append(col)
    return Encodings(**enc), missing


def _sort(v: Visual, columns: list[str]) -> str | None:
    for s in v.sort:
        if _snake(s.field.name) in columns:
            return f"{_snake(s.field.name)} {s.direction}"
    return None


def _charts(plan: Plan, spec: DashboardSpec) -> tuple[dict[str, list[Chart]], list[dict]]:
    """Charts per spec page id, with layout and encodings from the spec; titles left blank.
    Also the encodings left out because the dataset has no column for them."""
    visuals = {v.id: v for p in spec.pages for v in p.visuals}
    datasets = {d.id: d for d in plan.datasets}
    pages: dict[str, list[Chart]] = {p.id: [] for p in spec.pages}
    skipped: list[dict] = []
    for m in plan.visual_map:
        v = visuals[m.visual_id]
        pos = v.position
        x, y, w, h = (pos.x, pos.y, pos.w, pos.h) if pos else (0, 0, PAGE_W, PAGE_H // 3)
        d = datasets[m.dataset_id]
        encodings, missing = _encodings(v, m.component, d)
        skipped += missing
        pages[v.page].append(Chart(
            id=v.id, component=m.component, dataset=m.dataset_id,
            x=round(x), y=round(y), w=round(w), h=round(h), title=v.title or "",
            encodings=encodings, sort=_sort(v, _columns(d)), top_n=v.top_n,
            number_format=v.number_format,
        ))  # fmt: skip
    return pages, skipped


_PREDICATES = (exp.EQ, exp.In, exp.Between, exp.GT, exp.GTE, exp.LT, exp.LTE)


def _param_columns(sql: str) -> dict[str, set[tuple[str | None, str]]]:
    """Each $param mapped to the (table, column) pairs its predicates compare it with; the
    table is None for an unqualified column."""
    tree = sqlglot.parse_one(sql, read="duckdb")
    tables = {t.alias_or_name: ".".join(x for x in (t.db, t.name) if x) for t in tree.find_all(exp.Table)}
    out: dict[str, set] = {}
    for ph in tree.find_all(exp.Placeholder):
        if isinstance(ph.parent, _PREDICATES):
            for col in ph.parent.find_all(exp.Column):
                out.setdefault(ph.name, set()).add((tables.get(col.table) if col.table else None, col.name))
    return out


def _and(a: list, b: list) -> list:
    """The values of two `in` filters on one field that both apply (empty means no filter)."""
    return list(b) if not a else list(a) if not b else [x for x in a if x in b]


def _filters(plan: Plan, spec: DashboardSpec, charts: list[Chart]) -> tuple[list[Filter], list[dict]]:
    """Slicers and report, page and visual filters become controls scoped as in the source
    (report: every chart, page: the charts on that page, visual: that chart), bound to the
    dataset parameter whose SQL predicate reads the field's mapped column. Where scopes overlap
    on one parameter the narrowest control wins and takes the wider values too. Also returns a
    skipped entry for every source that does not reach all the charts in its scope."""
    ids = [c.id for c in charts]
    page_of = {v.id: v.page for p in spec.pages for v in p.visuals}
    sources = [("report", f.field, f.values, ids, f.operator) for f in spec.filters]
    for p in spec.pages:
        here, scope = [i for i in ids if page_of[i] == p.id], f"page {p.name}"
        sources += [(scope, e.field, [], here, "in") for v in p.visuals if v.type == "slicer" for e in v.encodings]
        sources += [(scope, f.field, f.values, here, f.operator) for f in p.filters]
        sources += [(f"visual {v.id}", f.field, f.values, [v.id], f.operator)
                    for v in p.visuals if v.id in ids for f in v.filters]  # fmt: skip
    targets = {(m.spec_table, m.spec_column): (m.target_table, m.target_column) for m in plan.column_map}
    known = {t for t, _ in targets.values()}
    binds = {d.id: _param_columns(d.sql) for d in plan.datasets}
    declared = {d.id: {p.name for p in d.params} for d in plan.datasets}
    dataset_of = {c.id: c.dataset for c in charts}
    types = {(t.name, c.name): c.data_type for t in spec.tables for c in t.columns}
    groups: dict[tuple[str, str], dict] = {}  # (scope, param) -> field, column, values
    owner: dict[tuple[str, str], tuple[str, str]] = {}  # (chart, param) -> its narrowest group
    skipped: list[dict] = []
    for scope, field, values, scope_charts, operator in sources:
        name, target = f"{field.table}.{field.name}", targets.get((field.table, field.name))
        hits, misses = {}, []
        for cid in scope_charts:
            ds = dataset_of[cid]
            found = sorted(p for p, cols in binds[ds].items() if p in declared[ds] and target and any(
                c == target[1] and (t is None or t == target[0] or t not in known) for t, c in cols))  # fmt: skip
            if len(found) == 1:
                hits[cid] = found[0]
            else:
                misses.append(cid)
        if misses:
            reason = (f"no dataset parameter filters {name}" if not hits
                      else f"no single dataset parameter filters {name} for {misses}")  # fmt: skip
            entry = {"item": f"filter:{name} ({scope})", "reason": reason, "fallback": "not applied — review manually"}
            if entry not in skipped:  # a slicer is both a visual and a page filter: report it once
                skipped.append(entry)
        # The controls compare by equality (the date control holds one date): any other operator,
        # and eq on a number (offered as text options), is a downgrade.
        numeric = bool(_NUMERIC.search(types.get((field.table, field.name), ""))) or any(
            isinstance(x, int | float) and not isinstance(x, bool) for x in values)  # fmt: skip
        if hits and (operator not in ("in", "eq") or (operator == "eq" and numeric)):
            reason = RANGE_DOWNGRADE if operator == "between" else f"'{operator}' filter rendered as an equality control"
            entry = {"item": f"filter:{name} ({scope})", "reason": reason, "fallback": "review manually"}
            if entry not in skipped:
                skipped.append(entry)
        for param in dict.fromkeys(hits.values()):
            g = groups.setdefault((scope, param), {"field": field, "column": target[1], "values": list(values)})
            g["values"] = _and(g["values"], values)
        for cid, param in hits.items():
            prev, key = owner.get((cid, param)), (scope, param)
            if prev and prev != key:
                groups[key]["values"] = _and(groups[key]["values"], groups[prev]["values"])
            owner[(cid, param)] = key
    out: list[Filter] = []
    for (scope, param), g in groups.items():
        mine = [i for i in ids if owner.get((i, param)) == (scope, param)]
        if not mine:
            continue
        fid = param if param not in {f.id for f in out} else f"{param}_{_snake(scope)}"
        is_date = "date" in types.get((g["field"].table, g["field"].name), "").lower()
        out.append(Filter(
            id=fid, label=g["field"].name, field=g["column"], param=param,
            control="date_range" if is_date else "multi_select",
            applies_to="all" if len(mine) == len(ids) else mine,
            default=[str(x) for x in g["values"]] or None,
        ))  # fmt: skip
    return out, skipped


def ui_skipped(plan: Plan, spec: DashboardSpec) -> list[dict]:
    """Encodings, slicers and filters the built dashboard leaves out, for build_report.json."""
    pages, skipped = _charts(plan, spec)
    return skipped + _filters(plan, spec, [c for cs in pages.values() for c in cs])[1]


def build_ui(plan: Plan, spec: DashboardSpec, tools: RepoTools, llm: LLM, fix: list[Finding] | None) -> Path:
    """Write dashboard.json. Layout, encodings, filters and datasets are computed in code; the
    LLM fills titles and number formats the spec lacks, and redoes them all on `fix`."""
    base = dashboard_dir(spec)
    pages, _ = _charts(plan, spec)
    charts = [c for cs in pages.values() for c in cs]
    filters, _ = _filters(plan, spec, charts)

    def assemble(texts: dict[str, ChartText]) -> DashboardConfig:
        def text(c: Chart) -> Chart:
            t = texts.get(c.id)
            if t is None:
                return c
            return c.model_copy(update={"title": c.title or t.title, "number_format": c.number_format or t.number_format})

        return DashboardConfig.model_validate({
            "title": spec.name,
            "pages": [Page(name=p.name, width=PAGE_W, height=PAGE_H, charts=[text(c) for c in pages[p.id]])
                      for p in spec.pages],
            "filters": filters,
            "datasets": register_datasets(plan),
        })  # fmt: skip

    need = [c for c in charts if fix or not c.title or c.number_format is None]
    texts: dict[str, ChartText] = {}
    if need:
        prompt = "Charts:\n" + json.dumps(
            [{"id": c.id, "component": c.component, "encodings": c.encodings.model_dump(exclude_none=True),
              "spec_title": c.title or None, "spec_number_format": c.number_format} for c in need],
            indent=2,
        )  # fmt: skip
        if fix:
            current = tools.read_file(f"{base}/dashboard.json") if (tools.root / base / "dashboard.json").is_file() else "(missing)"
            prompt += f"\n\nFindings to fix:\n{json.dumps([f.model_dump() for f in fix], indent=2)}"
            prompt += f"\n\nCurrent dashboard.json:\n{current}"

        def check(reply: UiText) -> list[str]:
            got = [t.id for t in reply.charts]
            wanted = [c.id for c in need]
            if sorted(got) != sorted(wanted):
                return [f"reply must cover exactly the charts {sorted(wanted)}, got {sorted(got)}"]
            empty = [t.id for t in reply.charts if not t.title.strip()]
            if empty:
                return [f"empty titles for {empty}"]
            try:
                assemble({t.id: t for t in reply.charts})
            except ValidationError as e:
                return [str(e)]
            return []

        reply = ask_valid(llm, _PROMPT.read_text(encoding="utf-8"), prompt, UiText, check)
        texts = {t.id: t for t in reply.charts}
    (path,) = write_all(tools, base, {"dashboard.json": assemble(texts).model_dump_json(indent=2)})
    return path
