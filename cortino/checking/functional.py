from collections.abc import Callable

from cortino import config as cortino_config
from cortino.checking import CheckResult, Finding
from cortino.checking.data_values import compare
from cortino.plan.models import Plan
from cortino.platform.config_schema import DashboardConfig
from cortino.tools.browser import Browser, BrowserError
from cortino.tools.warehouse import Warehouse

# expected_fn(chart_id, param, value) -> the rows the chart should show with `param` bound to
# `value`, from an independent query (never the builder's dataset SQL).
ExpectedFn = Callable[[str, str, str], list[dict]]

# How many of a field's values to try when looking for two that have data.
CANDIDATES = 20


def _quote(identifier: str) -> str:
    return ".".join('"' + part.replace('"', '""') + '"' for part in identifier.split("."))


def pick_values(warehouse: Warehouse, plan: Plan, field: str, limit: int = 2) -> list[str]:
    """Distinct non-null values of the warehouse column the plan maps to `field`, sorted, as the
    strings the preview offers as filter options and binds as the dataset param."""
    table = next((m.target_table for m in plan.column_map if m.target_column == field), None)
    if table is None:
        return []
    col = _quote(field)
    rows = warehouse.query(f"SELECT DISTINCT {col} AS v FROM {_quote(table)} WHERE {col} IS NOT NULL ORDER BY 1",
                           limit=limit)  # fmt: skip
    return [str(r["v"]) for r in rows]


def check_functional(
    url: str,
    config: DashboardConfig,
    plan: Plan,
    warehouse: Warehouse,
    browser: Browser,
    expected_fn: ExpectedFn,
) -> CheckResult:
    """For each filter: set two values from the data in the preview, read each chart it applies
    to, and require that the chart changes (else a `ui` wiring failure) and matches the
    independent rows for that value (else a `data` failure). Every filter is cleared first and
    each is reset afterwards, so only the filter under test is bound. Values whose independent
    rows hold data are preferred."""
    thresholds = cortino_config.load("thresholds")
    tol = {"tol_abs": thresholds["tolerance_abs"], "tol_rel": thresholds["tolerance_rel"]}
    charts = {c.id: c for p in config.pages for c in p.charts}
    findings: list[Finding] = []

    def fail(owner, message, visual_id=None, **ev):  # ui wiring is computed in code: not fixable
        findings.append(Finding(check="functional", owner=owner, visual_id=visual_id, message=message, evidence=ev,
                                fixable=owner != "ui"))  # fmt: skip

    def has_data(f, targets, value):
        rows = (r for cid in targets for r in expected_fn(cid, f.param, value))
        return any(v is not None for r in rows for v in r.values())

    def reset(f) -> bool:
        try:
            browser.set_filter(url, f.id, "")
            return True
        except BrowserError as e:
            fail("ui", f"filter '{f.id}': could not reset: {e}", filter=f.id)
            return False

    ready = {f.id for f in config.filters if reset(f)}
    for f in config.filters:
        if f.id not in ready:
            continue
        targets = list(charts) if f.applies_to == "all" else f.applies_to
        candidates = pick_values(warehouse, plan, f.field, limit=CANDIDATES)
        values = [v for v in candidates if has_data(f, targets, v)][:2]
        values += [v for v in candidates if v not in values][: 2 - len(values)]
        if len(values) < 2:
            fail("ui", f"filter '{f.id}': could not pick two values of '{f.field}' from the data", filter=f.id)
            continue
        seen: dict[str, list[list[dict]]] = {cid: [] for cid in targets}
        try:
            for value in values:
                browser.set_filter(url, f.id, value)
                for cid in targets:
                    seen[cid].append(browser.read_chart_data(url, cid))
        except BrowserError as e:
            fail("ui", f"filter '{f.id}': {e}", filter=f.id)
            continue
        finally:
            reset(f)
        for cid, (first, second) in seen.items():
            if first == second:
                fail("ui", f"filter '{f.id}' does not change chart '{cid}'", cid, filter=f.id, values=values)
                continue
            chart = charts[cid]
            keys = [k for k in (chart.encodings.x, chart.encodings.series) if k]
            for value, rows in zip(values, (first, second)):
                result = compare(rows, expected_fn(cid, f.param, value), keys, **tol)
                if not result.passed:
                    fail("data", f"chart '{cid}' with filter '{f.id}' = {value!r} differs from the independent query",
                         cid, filter=f.id, value=value, missing_keys=result.missing_keys,
                         extra_keys=result.extra_keys, value_diffs=result.value_diffs)  # fmt: skip
    return CheckResult(name="functional", passed=not findings, findings=findings)
