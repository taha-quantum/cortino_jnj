import csv
import datetime
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path

from cortino import config
from cortino.building.datasets import dashboard_dir
from cortino.building.ui_builder import _snake
from cortino.checking import CheckResult, Finding
from cortino.checking.independent_query import (
    IndependentQueryError,
    write_independent_query,
)
from cortino.llm import LLM, LLMError
from cortino.plan.models import Plan
from cortino.spec.models import DashboardSpec, Visual
from cortino.tools.source_catalog import SourceCatalog
from cortino.tools.warehouse import Warehouse, WarehouseError, _strip_semicolon

NO_GROUND_TRUTH = "none — independent query only"
MAX_ROWS = 100_000  # above this, compare bounded totals instead of every row
MAX_DIFFS = 20


@dataclass
class Comparison:
    passed: bool
    missing_keys: list[dict] = field(default_factory=list)
    extra_keys: list[dict] = field(default_factory=list)
    value_diffs: list[dict] = field(default_factory=list)


def _number(v) -> Decimal | None:
    if isinstance(v, bool):
        return None
    if isinstance(v, (int, float, Decimal)):
        d = Decimal(str(v))
    elif isinstance(v, str):
        try:
            d = Decimal(v.strip())
        except InvalidOperation:
            return None
    else:
        return None
    return d if d.is_finite() else None


def _temporal(v) -> str:
    """One ISO form for dates and datetimes: a midnight datetime is the same key as its date."""
    if not isinstance(v, datetime.datetime):
        v = datetime.datetime(v.year, v.month, v.day)
    return v.date().isoformat() if v.time() == datetime.time() and v.tzinfo is None else v.isoformat()


def _key_value(v):
    """Keys: an empty CSV cell is BLANK (None), numbers are Decimal, ISO date strings are dates."""
    if isinstance(v, datetime.date):
        return _temporal(v)
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return None
    n = _number(v)
    if n is not None:
        return n
    if isinstance(v, str):
        try:
            return _temporal(datetime.datetime.fromisoformat(v))
        except ValueError:
            pass
    return v


def _value(v):
    """None and an empty CSV cell are BLANK; numbers become Decimal; strings are stripped."""
    if v is None or (isinstance(v, str) and not v.strip()):
        return None
    n = _number(v)
    return n if n is not None else _key_value(v)


def _index(rows: list[dict], keys: list[str]) -> tuple[dict, list[tuple]]:
    out, dupes = {}, []
    for row in rows:
        row = {_snake(k): v for k, v in row.items()}
        k = tuple(_key_value(row.get(c)) for c in keys)
        if k in out:
            dupes.append(k)
        out[k] = row
    return out, dupes


def compare(actual: list[dict], expected: list[dict], keys: list[str], tol_abs, tol_rel) -> Comparison:
    """Match rows on `keys` and compare every other expected column by (snake-cased) name."""
    keys = [_snake(k) for k in keys]
    tol_abs, tol_rel = Decimal(str(tol_abs)), Decimal(str(tol_rel))
    got, got_dupes = _index(actual, keys)
    want, want_dupes = _index(expected, keys)

    def named(k: tuple) -> dict:
        return dict(zip(keys, k))

    diffs = [{"key": named(k), "problem": "duplicate key in actual"} for k in got_dupes]
    diffs += [{"key": named(k), "problem": "duplicate key in expected"} for k in want_dupes]
    columns = sorted({c for row in want.values() for c in row} - set(keys))
    for k in (k for k in want if k in got):
        for c in columns:
            a, e = _value(got[k].get(c)), _value(want[k].get(c))
            if isinstance(a, Decimal) and isinstance(e, Decimal):
                same = abs(a - e) <= tol_abs or abs(a - e) <= tol_rel * abs(e)
            else:
                same = a == e
            if not same:
                diffs.append({"key": named(k), "column": c, "actual": got[k].get(c), "expected": want[k].get(c)})
    missing = [named(k) for k in want if k not in got]
    extra = [named(k) for k in got if k not in want]
    return Comparison(
        passed=not (diffs or missing or extra),
        missing_keys=missing[:MAX_DIFFS], extra_keys=extra[:MAX_DIFFS], value_diffs=diffs[:MAX_DIFFS],
    )  # fmt: skip


def _roles(visual: Visual) -> tuple[list[str], list[str]]:
    """Key columns (unaggregated column fields) and value columns (measures, aggregated columns)."""
    keys, values = [], []
    for e in visual.encodings:
        name = _snake(e.field.name)
        target = keys if e.field.kind == "column" and e.field.aggregation is None else values
        if name not in keys + values:
            target.append(name)
    return keys, values


def _totals_sql(sql: str, rows: list[dict], columns: list[str]) -> str:
    """Row count and the sum of each (snake-cased) value column, over the query's full result."""
    raw = {_snake(c): c for c in (rows[0] if rows else {})}
    sums = "".join(f', sum("{raw.get(c, c)}") AS "{c}"' for c in columns)
    return f"SELECT count(*) AS row_count{sums} FROM (\n{_strip_semicolon(sql)}\n)"


def _totals(rows: list[dict], columns: list[str]) -> list[dict]:
    rows = [{_snake(k): v for k, v in r.items()} for r in rows]
    return [{"row_count": len(rows), **{c: sum(_number(r.get(c)) or 0 for r in rows) for c in columns}}]


def check_data_values(
    wt_dir: Path,
    spec: DashboardSpec,
    plan: Plan,
    throwaway: Warehouse,
    ground_truth_dir: Path | None,
    catalog: SourceCatalog,
    llm: LLM,
) -> CheckResult:
    """Run each visual's dataset (NULL params) on the throwaway copy, which already has the views
    applied, and compare it with ground truth when present and always with an independent query."""
    thresholds = config.load("thresholds")
    tol = {"tol_abs": thresholds["tolerance_abs"], "tol_rel": thresholds["tolerance_rel"]}
    visuals = {v.id: v for p in spec.pages for v in p.visuals}
    datasets = {d.id: d for d in plan.datasets}
    findings: list[Finding] = []
    evidence: dict[str, dict] = {}

    for vm in plan.visual_map:
        vid, visual, ds = vm.visual_id, visuals[vm.visual_id], datasets[vm.dataset_id]
        info = evidence[vid] = {"dataset": ds.id}

        def fail(message, vid=vid, fixable=True, **ev):
            findings.append(Finding(check="data_values", owner="data", visual_id=vid, message=message, evidence=ev,
                                    fixable=fixable))  # fmt: skip

        params = {p.name: None for p in ds.params}
        try:
            sql = (Path(wt_dir) / dashboard_dir(spec) / "datasets" / f"{ds.id}.sql").read_text(encoding="utf-8")
            actual = throwaway.query(sql, params, limit=MAX_ROWS + 1)
        except (OSError, WarehouseError) as e:
            fail(f"dataset '{ds.id}' could not be run: {e}", dataset=ds.id)
            continue

        keys, values = _roles(visual)
        present = {_snake(c) for c in actual[0]} if actual else set(values)
        compared = [c for c in values if c in present]
        if missing := [c for c in values if c not in present]:
            fail(f"dataset '{ds.id}' lacks {', '.join(repr(c) for c in missing)} for '{vid}'",
                 dataset=ds.id, missing_columns=missing)  # fmt: skip

        expected: dict[str, list[dict]] = {}
        gt_path = ground_truth_dir / f"{vid}.csv" if ground_truth_dir else None
        if gt_path and gt_path.exists():
            with gt_path.open(newline="", encoding="utf-8") as f:
                expected["ground_truth"] = list(csv.DictReader(f))
            info["ground_truth"] = gt_path.name
        else:
            info["ground_truth"] = NO_GROUND_TRUTH
        independent = None
        try:
            independent = write_independent_query(
                visual, spec, plan.source_map, plan.column_map, spec.measures, catalog, llm
            )
            expected["independent_query"] = throwaway.query(independent, limit=MAX_ROWS + 1)
            info["independent_query"] = independent
        except (IndependentQueryError, LLMError, WarehouseError) as e:
            info["independent_query"] = f"unavailable: {e}"
            if "ground_truth" not in expected:
                fail(f"independent query unavailable for '{vid}': {e}", fixable=False,
                     note="independent query unavailable", independent_query=independent)  # fmt: skip

        if len(actual) > MAX_ROWS or len(expected.get("independent_query", [])) > MAX_ROWS:
            info["comparison"] = "bounded comparison"
            try:
                actual = throwaway.query(_totals_sql(sql, actual, compared), params)
                if "independent_query" in expected:
                    ind = expected["independent_query"]
                    expected["independent_query"] = throwaway.query(_totals_sql(independent, ind, compared))
            except WarehouseError as e:
                fail(f"bounded totals for '{vid}' could not be run: {e}", dataset=ds.id)
                continue
            if "ground_truth" in expected:
                expected["ground_truth"] = _totals(expected["ground_truth"], compared)
            keys, compared = [], ["row_count", *compared]

        for against, rows in expected.items():
            rows = [{_snake(k): v for k, v in r.items()} for r in rows]
            rows = [{c: r.get(c) for c in keys + compared} for r in rows]
            result = compare(actual, rows, keys, **tol)
            if not result.passed:
                fail(f"dataset '{ds.id}' values for '{vid}' differ from the {against.replace('_', ' ')}",
                     against=against, missing_keys=result.missing_keys, extra_keys=result.extra_keys,
                     value_diffs=result.value_diffs)  # fmt: skip

    return CheckResult(name="data_values", passed=not findings, findings=findings, evidence={"visuals": evidence})
