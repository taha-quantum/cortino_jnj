"""Hard calculations, slicers and filters at parity: the sales_hard fixture (Power BI time
intelligence and % of total; Tableau FIXED LOD and RUNNING_SUM; a date-range slicer in both)
through the real stages with scripted LLM replies that follow the skill patterns."""

import csv
import json
import os
import shutil
from decimal import Decimal

import duckdb
import pytest
import sqlglot
from pipeline import assert_verified, create_run, report, runner, zip_dir
from scripted_hard import (
    HARD,
    PBIP_REPLY,
    PBIP_VISUALS,
    TWB_REPLY,
    TWB_VISUALS,
    hard_llm,
    titles,
)
from sqlglot import exp

from cortino import config
from cortino.building.data_builder import build_data
from cortino.building.datasets import dashboard_dir
from cortino.building.ui_builder import build_ui, ui_skipped
from cortino.checking.data_values import _roles, compare
from cortino.llm import FakeLLM, StrandsLLM
from cortino.orchestrator.states import RunState as S
from cortino.planning.planner import PlanError, make_plan
from cortino.platform.config_schema import DashboardConfig
from cortino.spec.models import load_spec
from cortino.tools.repo_tools import RepoTools
from cortino.tools.warehouse import DuckDBWarehouse, throwaway_copy

pytestmark = pytest.mark.browser
CHART_MAP = config.load("chart_map")["chart_map"]
CASES = {  # source -> golden spec, scripted planner reply, hard measures
    "pbip": ("golden_spec.json", PBIP_REPLY, {"Sales YTD", "Sales LY", "% of Total"}),
    "twb": ("golden_spec_twb.json", TWB_REPLY, {"Region Sales", "Running Sales"}),
}
RANGE_DOWNGRADE = "range filter rendered as a single-date filter"
SKILL_TITLES = {"pbip": "# DAX to SQL", "twb": "# Tableau calculations to SQL"}


def _plan(source: str, catalog):
    golden, reply, _ = CASES[source]
    spec = load_spec(HARD / golden)
    llm = FakeLLM({"planner": [reply]})
    return spec, make_plan(spec, catalog, llm, CHART_MAP), llm


def _upload(source: str, tmp_path):
    if source == "pbip":
        return zip_dir(HARD / "pbip", tmp_path / "SalesHard.zip")
    return shutil.copy(HARD / "twb" / "SalesHard.twb", tmp_path / "SalesHard.twb")


@pytest.mark.parametrize("source", ["pbip", "twb"])
def test_hard_measures_flagged_with_test_query(source, catalog):
    _, plan, llm = _plan(source, catalog)
    hard = CASES[source][2]
    assert {m.name for m in plan.measures if m.flagged} == hard
    for m in plan.measures:
        if m.name not in hard:  # only the plain SUM in the Power BI model is simple
            assert (m.name, m.difficulty, m.test_query) == ("Total Sales", "simple", None)
            continue
        assert m.difficulty == "hard" and m.test_query
        (stmt,) = sqlglot.parse(m.test_query, read="duckdb")
        assert isinstance(stmt, exp.Query)
        assert catalog.run_readonly_sql(m.test_query)  # it runs on the warehouse and returns rows
    system = llm.calls[0]["system"]
    assert SKILL_TITLES[source] in system  # the skill for this source's calculation language ...
    assert all(t not in system for s, t in SKILL_TITLES.items() if s != source)  # ... and not the other


def test_sales_ly_keeps_the_leap_day_at_month_grain():
    """SAMEPERIODLASTYEAR at month grain is the whole earlier month: a 2024-02-29 sale is in
    February 2025's LY (a `date - INTERVAL 1 YEAR` join drops it: 2025 has no 29 February)."""
    con = duckdb.connect()
    con.execute("CREATE SCHEMA analytics")
    con.execute("CREATE TABLE analytics.dates AS SELECT d::DATE AS date, year(d) AS year, month(d) AS month "
                "FROM range(DATE '2024-01-01', DATE '2026-01-01', INTERVAL 1 DAY) t(d)")  # fmt: skip
    con.execute("CREATE TABLE analytics.sales (order_date DATE, amount DECIMAL(12, 2))")
    con.execute("INSERT INTO analytics.sales VALUES ('2024-02-28', 10), ('2024-02-29', 5)")
    (ly,) = [d["sql"] for d in PBIP_REPLY["datasets"] if d["id"] == "sales_ly"]
    rows = con.execute(f"SELECT month, year, sales_ly FROM ({ly}) WHERE sales_ly IS NOT NULL").fetchall()
    assert rows == [(2, 2025, Decimal("15.00"))]
    (measure,) = [m for m in PBIP_REPLY["measures"] if m["name"] == "Sales LY"]
    assert con.execute(measure["test_query"]).fetchone()[0] == Decimal("15.00")


def test_date_range_slicer_maps_to_date_range_control(tmp_path, catalog):
    expected = {  # source -> (param, charts it reaches, charts it cannot reach)
        "pbip": ("date", ["barShareByRegion"], ["lineSalesLy", "lineSalesYtd"]),
        "twb": ("order_date", "all", []),
    }
    for source, (param, reached, skipped) in expected.items():
        spec, plan, _ = _plan(source, catalog)
        (slicer,) = [f for p in spec.pages for f in p.filters if f.is_slicer]
        assert slicer.operator == "between"
        tools = RepoTools(tmp_path / source)
        visuals = [m.visual_id for m in plan.visual_map]
        path = build_ui(plan, spec, tools, FakeLLM({"builder": [titles(visuals)]}), None)
        cfg = DashboardConfig.model_validate_json(path.read_text(encoding="utf-8"))
        (control,) = cfg.filters
        assert (control.control, control.param, control.field) == ("date_range", param, param)
        assert control.applies_to == reached and control.default is None
        missed = [s for s in ui_skipped(plan, spec) if s["item"].startswith("filter:")]
        # The range is shown as one date until the platform has a two-date control: recorded, not silent.
        (downgrade,) = [s for s in missed if s["reason"] == RANGE_DOWNGRADE]
        assert downgrade["item"] == f"filter:{slicer.field.table}.{slicer.field.name} (page Overview)"
        unbound = [s for s in missed if s is not downgrade]
        assert [all(c in s["reason"] for c in skipped) for s in unbound] == ([True] if skipped else [])


def test_hard_fixture_data_check_passes(store, catalog, repo, tmp_path, chromium):
    llm = hard_llm("pbip")
    run = runner(store, catalog, llm)
    rid = create_run(store, _upload("pbip", tmp_path), repo, HARD / "expected", "Sales Hard")
    assert run.advance(rid) == S.AWAITING_PLAN_APPROVAL
    assert run.approve(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert store.get(rid).verify_loops == 0 and llm.unused() == {}
    checks = assert_verified(report(store, rid), chromium, PBIP_VISUALS)
    assert checks["data_values"]["findings"] == []
    plan_md = (store.path(rid) / "plan.md").read_text(encoding="utf-8")
    assert all(t in plan_md for t in ("Sales YTD", "Test query:"))
    build = json.loads((store.path(rid) / "build_report.json").read_text(encoding="utf-8"))
    assert [s["item"] for s in build["skipped"]] == ["filter:Date.date (page Overview)"] * 2
    assert RANGE_DOWNGRADE in {s["reason"] for s in build["skipped"]}


def test_tableau_hard_fixture_reaches_preview(store, catalog, repo, tmp_path, chromium):
    llm = hard_llm("twb")
    run = runner(store, catalog, llm)
    rid = create_run(store, _upload("twb", tmp_path), repo, HARD / "expected", "Sales Hard")
    assert run.advance(rid) == S.AWAITING_PLAN_APPROVAL
    assert store.get(rid).meta["source_type"] == "twb"
    spec = load_spec(store.path(rid) / "spec.json")
    assert [(m.name, m.language) for m in spec.measures] == [("Region Sales", "tableau"), ("Running Sales", "tableau")]
    assert run.approve(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert store.get(rid).verify_loops == 0 and llm.unused() == {}
    checks = assert_verified(report(store, rid), chromium, TWB_VISUALS)
    if chromium:
        assert checks["functional"]["passed"]  # the date filter changes both charts as the checker expects
    for c in (c for c in llm.calls if c["role"] == "checker"):
        assert "region_sales_by_customer" not in c["prompt"] and "OVER (" not in c["prompt"]


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
@pytest.mark.parametrize("source", ["pbip", "twb"])
def test_hard_fixture_live_planner(source, catalog, warehouse, tmp_path, record_property):
    """The real planner on the hard fixture. How well it translates hard calculations is reported
    (per visual: does its dataset match the expected CSV?), not asserted."""
    spec = load_spec(HARD / CASES[source][0])
    llm = StrandsLLM(config.load("models"), config.load("thresholds"))
    try:
        plan = make_plan(spec, catalog, llm, CHART_MAP)
    except PlanError as e:
        record_property("hard_calc_results", f"plan failed: {e}")
        print(f"[{source}] plan failed: {e}")
        return
    tools = RepoTools(tmp_path / "wt")
    build_data(plan, spec, tools, FakeLLM({}), None)
    source_db = DuckDBWarehouse(warehouse, read_only=True)
    db = throwaway_copy(source_db, tmp_path / "db")
    source_db.close()
    base = tools.root / dashboard_dir(spec)
    results = {}
    try:
        if (views := (base / "views.sql").read_text(encoding="utf-8")).strip():
            db.query(views)
        visuals = {v.id: v for p in spec.pages for v in p.visuals}
        datasets = {d.id: d for d in plan.datasets}
        for m in plan.visual_map:
            ds = datasets[m.dataset_id]
            try:
                rows = db.query((base / "datasets" / f"{ds.id}.sql").read_text(encoding="utf-8"),
                                {p.name: None for p in ds.params})  # fmt: skip
            except Exception as e:  # noqa: BLE001 - reported, not asserted
                results[m.visual_id] = f"error: {e}"
                continue
            with open(HARD / "expected" / f"{m.visual_id}.csv", newline="", encoding="utf-8") as f:
                expected = list(csv.DictReader(f))
            keys, _ = _roles(visuals[m.visual_id])
            ok = compare(rows, expected, keys, config.load("thresholds")["tolerance_abs"],
                         config.load("thresholds")["tolerance_rel"]).passed  # fmt: skip
            results[m.visual_id] = "matches expected" if ok else "differs from expected"
    finally:
        db.close()
    record_property("hard_calc_results", json.dumps(results))
    print(f"[{source}] hard calc results: {json.dumps(results, indent=2)}")
