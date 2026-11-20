import json
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path

from cortino import config as cortino_config
from cortino.building.datasets import dashboard_dir
from cortino.building.worktree import create_worktree, slugify
from cortino.checking import CheckResult, Finding
from cortino.checking.chart_alignment import check_chart_alignment
from cortino.checking.code_security import check_code_security
from cortino.checking.data_values import check_data_values
from cortino.checking.files import read_text
from cortino.checking.functional import check_functional
from cortino.checking.independent_query import write_independent_query
from cortino.checking.lineage import check_lineage
from cortino.checking.visual import check_visual
from cortino.llm import LLM
from cortino.orchestrator.runner import RunContext
from cortino.plan.models import Plan
from cortino.platform.config_schema import DashboardConfig
from cortino.platform.preview_app import create_preview_app
from cortino.platform.serving import serve_app, throwaway_warehouse_with_views
from cortino.spec.models import DashboardSpec, load_spec
from cortino.tools.browser import PlaywrightBrowser
from cortino.tools.source_catalog import SourceCatalog
from cortino.tools.warehouse import Warehouse

CHECKS = ("data_values", "lineage", "chart_alignment", "code_security", "visual", "functional")
BROWSER_CHECKS = ("visual", "functional")  # costly: re-run only when they did not pass last time
BROWSER_UNAVAILABLE = "browser unavailable"


@dataclass
class VerifyReport:
    passed: bool
    checks: list[CheckResult]
    by_owner: dict[str, list[Finding]]
    skipped: dict[str, dict] = field(default_factory=dict)  # check name -> evidence; neither passed nor failed


class _ExpectedFailed(Exception):
    """expected_fn could not produce the independent rows."""


def _functional(url, cfg: DashboardConfig, spec: DashboardSpec, plan: Plan, db: Warehouse, browser,
                catalog: SourceCatalog, llm: LLM) -> CheckResult:  # fmt: skip
    """The functional check against independent queries with the filter's param bound. A query
    that cannot be written or run, for any reason, is a failing finding, not a crash."""
    visuals = {v.id: v for p in spec.pages for v in p.visuals}
    tables: dict[str, str] = {}
    for m in plan.column_map:  # the first mapping wins, as in functional.pick_values
        tables.setdefault(m.target_column, m.target_table)
    columns = {f.param: f"{tables[f.field]}.{f.field}" if f.field in tables else f.field for f in cfg.filters}
    limit = cortino_config.load("thresholds")["readonly_sql_limit"]
    queries: dict[tuple[str, str], str] = {}

    def expected(chart_id: str, param: str, value: str) -> list[dict]:
        try:
            if (chart_id, param) not in queries:
                queries[chart_id, param] = write_independent_query(
                    visuals[chart_id], spec, plan.source_map, plan.column_map, spec.measures, catalog, llm,
                    bound=(columns[param], param),
                )  # fmt: skip
            return db.query(queries[chart_id, param], {param: value}, limit=limit)
        except Exception as e:
            raise _ExpectedFailed(f"{type(e).__name__}: {e}") from e

    try:
        return check_functional(url, cfg, plan, db, browser, expected)
    except _ExpectedFailed as e:
        f = Finding(check="functional", owner="data", visual_id=None, fixable=False,
                    message=f"independent query for the functional check failed: {e}")  # fmt: skip
        return CheckResult(name="functional", passed=False, findings=[f])


def _browser():
    """A PlaywrightBrowser, or the launch error as text."""
    try:
        return PlaywrightBrowser(), None
    except Exception as e:  # noqa: BLE001 - any launch failure means no browser here
        return None, f"{type(e).__name__}: {str(e).splitlines()[0] if str(e) else ''}"


def _entry(name: str, results: dict[str, CheckResult], skipped: dict[str, dict]) -> dict:
    if name in skipped:
        return {"name": name, "passed": False, "skipped": True, "findings": [], "evidence": skipped[name]}
    return results[name].model_dump()


def run_verify(
    ctx: RunContext, previous_report: dict | None, catalog: SourceCatalog, llm: LLM
) -> VerifyReport:
    """Run the checks on a throwaway copy of the warehouse with views.sql applied and the preview
    app served from it, write verify/report.json and tear everything down. data_values always
    runs, and so do the cheap code checks; visual and functional run only when they did not pass in
    `previous_report` (or there is none), else their result is carried with `carried_from_loop`."""
    meta = ctx.store.get(ctx.run.id).meta
    spec = load_spec(ctx.path / "spec.json")
    plan = Plan.model_validate_json((ctx.path / "plan.json").read_text(encoding="utf-8"))
    wt = create_worktree(Path(meta["target_repo"]), ctx.path, slugify(meta["name"]), ctx.run.id).path
    dash = wt / dashboard_dir(spec)
    out = ctx.path / "verify"
    (out / "screenshots").mkdir(parents=True, exist_ok=True)
    previous = {c["name"]: c for c in previous_report["checks"]} if previous_report else {}
    rerun = [n for n in CHECKS if n not in BROWSER_CHECKS or not previous.get(n, {}).get("passed")]
    results = {}
    for n in (n for n in CHECKS if n not in rerun):
        carried = CheckResult.model_validate(previous[n])
        carried.evidence.setdefault("carried_from_loop", previous_report["loop"])
        results[n] = carried
    skipped: dict[str, dict] = {}

    def note(problem: str) -> None:
        ctx.store.event(ctx.run.id, "verify_teardown", problem=problem)

    expected_dir = ctx.path / "expected"
    references = ctx.path / "input" / "references"

    with ExitStack() as stack:
        db, cleanup = throwaway_warehouse_with_views(catalog.warehouse, dash, ctx.path / "verify_db", note)
        stack.callback(cleanup)
        results["data_values"] = check_data_values(
            wt, spec, plan, db, expected_dir if expected_dir.is_dir() else None, catalog, llm
        )
        for name, check in (("lineage", check_lineage), ("chart_alignment", check_chart_alignment),
                            ("code_security", check_code_security)):  # fmt: skip
            if name in rerun:
                results[name] = check(dash, spec, plan)
        if browser_checks := [n for n in BROWSER_CHECKS if n in rerun]:
            browser, error = _browser()
            if browser is None:
                ctx.store.event(ctx.run.id, "browser_unavailable", error=error)
                skipped = {n: {"reason": BROWSER_UNAVAILABLE, "error": error} for n in browser_checks}
            else:
                stack.callback(browser.close)
                cfg = DashboardConfig.model_validate_json(read_text(dash / "dashboard.json"))
                url = stack.enter_context(serve_app(create_preview_app(dash, db, references, plan=plan), note))
                if "visual" in rerun:
                    results["visual"] = check_visual(
                        url, cfg, spec, browser, out / "screenshots" / "dashboard.png", references, llm
                    )
                if "functional" in rerun:
                    results["functional"] = _functional(url, cfg, spec, plan, db, browser, catalog, llm)

    checks = [results[n] for n in CHECKS if n in results]
    by_owner: dict[str, list[Finding]] = {}
    for f in (f for c in checks for f in c.findings):
        by_owner.setdefault(f.owner, []).append(f)
    passed = all(c.passed for c in checks)
    report = {
        "loop": ctx.run.verify_loops + 1,
        "passed": passed,
        "checks": [_entry(n, results, skipped) for n in CHECKS],
        "by_owner": {o: [f.model_dump() for f in fs] for o, fs in by_owner.items()},
        "unfixable": [f.model_dump() for fs in by_owner.values() for f in fs if not f.fixable],
    }
    (out / "report.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    return VerifyReport(passed, checks, by_owner, skipped)
