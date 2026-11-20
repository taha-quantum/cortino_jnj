import copy
import json

import pytest

from cortino import config
from cortino.llm import FakeLLM, FakeReply
from cortino.orchestrator.run_store import RunStore
from cortino.orchestrator.runner import Runner, StageResult
from cortino.orchestrator.stages import StageDeps, default_stages
from cortino.orchestrator.states import RunState as S
from cortino.plan.models import Plan
from cortino.planning.plan_md import render_plan_md
from cortino.planning.planner import PlanError, make_plan
from cortino.spec.models import Column, Measure, Table, Unknown, load_spec, save_spec
from cortino.tools.source_catalog import SourceCatalog
from cortino.tools.warehouse import DuckDBWarehouse

CHART_MAP = config.load("chart_map")["chart_map"]
SECTIONS = [
    "Source mapping", "Column mapping", "Measures", "Datasets", "Visuals", "Unsupported",
    "Questions",
]  # fmt: skip


@pytest.fixture
def spec(sales_fixture):
    return load_spec(sales_fixture / "golden_spec.json")


@pytest.fixture
def catalog(warehouse):
    wh = DuckDBWarehouse(warehouse, read_only=True)
    yield SourceCatalog(wh, config.load("sources")["mappings"])
    wh.close()


def _ds(ds_id, visual_id, sql):
    return {"id": ds_id, "visual_ids": [visual_id], "sql": sql,
            "params": [{"name": "region", "type": "string"}]}  # fmt: skip


WHERE = "WHERE ($region IS NULL OR c.region = $region)"
JOIN = "FROM analytics.sales s JOIN analytics.customers c ON s.customer_id = c.customer_id"


def reply(**over) -> dict:
    """A valid planner reply for the sales fixture (all three measures are simple)."""
    base = {
        "plan_version": "0.1",
        "datasets": [
            _ds("sales_by_region", "barSalesByRegion",
                f"SELECT c.region, SUM(s.amount) AS total_sales {JOIN} {WHERE} GROUP BY 1"),
            _ds("total_sales", "cardTotalSales",
                f"SELECT SUM(s.amount) AS total_sales {JOIN} {WHERE}"),
            _ds("sales_by_month", "lineSalesByMonth",
                f"SELECT d.month, SUM(s.amount) AS total_sales {JOIN} "
                f"JOIN analytics.dates d ON s.order_date = d.date {WHERE} GROUP BY 1"),
            _ds("by_category", "tableByCategory",
                f"SELECT p.category, SUM(s.amount) AS total_sales, COUNT(DISTINCT s.order_id) + "
                f"COALESCE(MAX(CASE WHEN s.order_id IS NULL THEN 1 ELSE 0 END), 0) AS order_count {JOIN} "
                f"JOIN analytics.products p ON s.product_id = p.product_id {WHERE} GROUP BY 1"),
        ],
        "visual_map": [
            {"visual_id": "barSalesByRegion", "component": "bar", "dataset_id": "sales_by_region"},
            {"visual_id": "cardTotalSales", "component": "card", "dataset_id": "total_sales"},
            {"visual_id": "lineSalesByMonth", "component": "line", "dataset_id": "sales_by_month"},
            {"visual_id": "tableByCategory", "component": "table", "dataset_id": "by_category"},
        ],
        "tasks": [{"owner": "data", "description": "write the four datasets"},
                  {"owner": "ui", "description": "lay out the page", "depends_on": ["data"]}],
    }
    base.update(over)
    return base


def plan_for(spec, catalog, replies, **kw):
    llm = FakeLLM({"planner": replies})
    return make_plan(spec, catalog, llm, CHART_MAP, **kw), llm


def test_plan_written_and_valid(tmp_path, spec, catalog):
    store = RunStore(tmp_path / "runs")
    rid = _parsed_run(store, spec)
    llm = FakeLLM({"planner": [FakeReply(json.dumps(reply()), 100, 20)]})
    runner = Runner(store, default_stages(_deps(llm, catalog)))
    assert runner.advance(rid) == S.AWAITING_PLAN_APPROVAL
    run_dir = store.path(rid)
    plan = Plan.model_validate_json((run_dir / "plan.json").read_text(encoding="utf-8"))
    assert {m.target_table for m in plan.source_map} == set(catalog.list_sources())
    assert [m.difficulty for m in plan.measures] == ["simple"] * 3
    assert len(plan.visual_map) == 4
    md = (run_dir / "plan.md").read_text(encoding="utf-8")
    assert "Source mapping" in md
    usage = [e for e in _events(store, rid) if e["kind"] == "llm_usage"]
    assert usage[0]["role"] == "planner" and usage[0]["input_tokens"] == 100
    assert {t.__name__ for t in llm.calls[0]["tools"]} == {
        "list_sources", "describe_table", "run_readonly_sql"
    }
    store.close()


def test_plan_md_has_every_section_hard_measures_first(spec, catalog):
    spec.measures.append(Measure(name="YTD Sales", table="Sales", language="dax",
                                 expression="TOTALYTD([Total Sales], 'Date'[date])"))  # fmt: skip
    hard = {"name": "YTD Sales", "original": "TOTALYTD([Total Sales], 'Date'[date])",
            "sql_expression": "SUM(amount) OVER (PARTITION BY year ORDER BY date)",
            "difficulty": "hard", "test_query": "SELECT 1"}  # fmt: skip
    plan, _ = plan_for(spec, catalog, [reply(measures=[hard])])
    md = render_plan_md(plan, spec)
    positions = [md.index(f"## {s}") for s in SECTIONS]
    assert positions == sorted(positions)
    measures = md[md.index("## Measures"):md.index("## Datasets")]
    assert measures.index("YTD Sales") < measures.index("Total Sales")
    assert "SELECT 1" in measures


def test_rls_and_custom_visual_listed_unsupported(spec, catalog):
    plan, _ = plan_for(spec, catalog, [reply()])
    items = {u.item: u for u in plan.unsupported}
    assert "rls:RegionManager" in items
    assert items["rls:RegionManager"].fallback == "needs platform decision — not rebuilt"
    assert "visual:customXyz" in items
    assert "customXyz" not in {v.visual_id for v in plan.visual_map}
    md = render_plan_md(plan, spec)
    assert "RegionManager" in md and "customXyz" in md


def test_unknowns_sharing_where_all_listed(spec, catalog):
    spec.unknowns += [Unknown(where="report", what="unparsed reportExtensions"),
                      Unknown(where="report", what="pageOrder entry without folder"),
                      Unknown(where="visual:customXyz", what="unparsed filterConfig")]  # fmt: skip
    plan, _ = plan_for(spec, catalog, [reply()])
    listed = {(u.item, u.reason) for u in plan.unsupported}
    assert {(u.where, u.what) for u in spec.unknowns} <= listed
    md = render_plan_md(plan, spec)
    assert all(u.what in md for u in spec.unknowns)


def _guessed_sales(spec):
    """Sales loses its explicit source and gains junk columns: overlap guesses analytics.sales at 0.5."""
    spec.tables[0].source_id = None
    spec.tables[0].columns += [Column(name=f"junk{i}", data_type="string") for i in range(6)]


DATES_ONLY = [{**d, "sql": "SELECT d.month FROM analytics.dates d"} for d in reply()["datasets"]]
HARD = [{"name": n, "original": "x", "sql_expression": "COUNT(*)", "difficulty": "hard",
         "test_query": "SELECT COUNT(*) FROM analytics.dates"}
        for n in ("Total Sales", "Order Count", "Avg Order")]  # fmt: skip
TO_DATES = [{"source_id": None, "spec_table": "Sales", "target_table": "analytics.dates",
             "confidence": 1.0, "method": "llm", "evidence": "remap"}]  # fmt: skip


def test_simple_measures_follow_llm_remap_of_guessed_table(spec, catalog):
    _guessed_sales(spec)
    plan, _ = plan_for(spec, catalog, [reply(source_map=TO_DATES, measures=HARD, datasets=DATES_ONLY)])
    assert {m.target_table for m in plan.source_map if m.spec_table == "Sales"} == {"analytics.dates"}
    assert not any("analytics.sales" in m.sql_expression for m in plan.measures)


def test_simple_measures_follow_previous_remap_on_change_request(spec, catalog):
    _guessed_sales(spec)
    first, _ = plan_for(spec, catalog, [reply(source_map=TO_DATES, measures=HARD, datasets=DATES_ONLY)])
    plan, _ = plan_for(spec, catalog, [reply(measures=HARD, datasets=DATES_ONLY)],
                       change_request="tweak", previous=first)  # fmt: skip
    assert not any("analytics.sales" in m.sql_expression for m in plan.measures)


def test_change_request_remapping_back_keeps_llm_measures_from_previous_plan(spec, catalog):
    _guessed_sales(spec)
    first, _ = plan_for(spec, catalog, [reply(source_map=TO_DATES, measures=HARD, datasets=DATES_ONLY)])
    to_sales = [{**TO_DATES[0], "target_table": "analytics.sales"}]
    again = reply(source_map=to_sales, measures=HARD)
    plan, _ = plan_for(spec, catalog, [again, again], change_request="Sales is analytics.sales", previous=first)
    assert {m.target_table for m in plan.source_map if m.spec_table == "Sales"} == {"analytics.sales"}
    assert all(m.difficulty == "simple" and "COUNT(*)" != m.sql_expression for m in plan.measures)


def test_map_visual_unsupported_with_table_fallback(spec, catalog):
    spec.pages[0].visuals[0].type = "map"
    over = copy.deepcopy(reply()["visual_map"])
    over[0]["component"] = "table"
    plan, _ = plan_for(spec, catalog, [reply(visual_map=over)])
    items = {u.item: u for u in plan.unsupported}
    assert items["visual:barSalesByRegion"].fallback == "closest standard chart: table"


def test_hard_measure_requires_test_query(spec, catalog):
    spec.measures.append(Measure(name="YTD Sales", table="Sales", language="dax",
                                 expression="TOTALYTD([Total Sales], 'Date'[date])"))  # fmt: skip
    hard = {"name": "YTD Sales", "original": "TOTALYTD([Total Sales], 'Date'[date])",
            "sql_expression": "SUM(amount)", "difficulty": "hard"}  # fmt: skip
    with pytest.raises(PlanError, match="test_query"):
        plan_for(spec, catalog, [reply(measures=[hard]), reply(measures=[hard])])
    good = {**hard, "test_query": "SELECT SUM(amount) FROM analytics.sales"}
    plan, llm = plan_for(spec, catalog, [reply(measures=[hard]), reply(measures=[good])])
    assert "test_query" in llm.calls[1]["prompt"]
    (ytd,) = [m for m in plan.measures if m.name == "YTD Sales"]
    assert ytd.flagged and ytd.difficulty == "hard"


@pytest.mark.parametrize(
    "test_query, error",
    [
        ("DELETE FROM analytics.sales", "single SELECT"),
        ("SELECT 1; SELECT 2", "single SELECT"),
        ("SELECT SUM(x) FROM analytics.secret", "analytics.secret"),
        ("SELECT 'abc FROM analytics.sales", "does not parse"),
    ],
)
def test_hard_measure_test_query_must_be_one_select_on_plan_tables(spec, catalog, test_query, error):
    spec.measures.append(Measure(name="YTD Sales", table="Sales", language="dax",
                                 expression="TOTALYTD([Total Sales], 'Date'[date])"))  # fmt: skip
    hard = {"name": "YTD Sales", "original": "TOTALYTD([Total Sales], 'Date'[date])",
            "sql_expression": "SUM(amount)", "difficulty": "hard", "test_query": test_query}  # fmt: skip
    with pytest.raises(PlanError, match=f"YTD Sales.*test_query.*{error}"):
        plan_for(spec, catalog, [reply(measures=[hard]), reply(measures=[hard])])


def test_planner_prompt_has_the_skill_for_the_source_language(spec, catalog):
    _, llm = plan_for(spec, catalog, [reply()])
    assert "TOTALYTD" in llm.calls[0]["system"] and "RUNNING_SUM" not in llm.calls[0]["system"]
    tableau = spec.model_copy(update={"source_format": "twb"})
    _, llm = plan_for(tableau, catalog, [reply()])
    assert "RUNNING_SUM" in llm.calls[0]["system"] and "TOTALYTD" not in llm.calls[0]["system"]


def test_llm_changing_explicit_mapping_rejected(spec, catalog):
    bad = [{"source_id": "Sales", "spec_table": "Sales", "target_table": "analytics.dates",
            "confidence": 1.0, "method": "llm", "evidence": "guess"}]  # fmt: skip
    with pytest.raises(PlanError, match="Sales"):
        plan_for(spec, catalog, [reply(source_map=bad), reply(source_map=bad)])


def test_llm_changing_simple_measure_rejected(spec, catalog):
    bad = [{"name": "Total Sales", "original": "SUM(Sales[amount])",
            "sql_expression": "MAX(amount)", "difficulty": "simple"}]  # fmt: skip
    with pytest.raises(PlanError, match="Total Sales"):
        plan_for(spec, catalog, [reply(measures=bad), reply(measures=bad)])


@pytest.mark.parametrize(
    "over, error",
    [
        ({"visual_map": reply()["visual_map"][1:]}, "barSalesByRegion"),
        ({"visual_map": [{**reply()["visual_map"][0], "component": "pie"},
                         *reply()["visual_map"][1:]]}, "pie"),
        ({"datasets": [{**reply()["datasets"][0], "sql": "SELECT * FROM analytics.secret"},
                       *reply()["datasets"][1:]]}, "analytics.secret"),
        ({"datasets": [{**reply()["datasets"][0], "sql": "DELETE FROM analytics.sales"},
                       *reply()["datasets"][1:]]}, "SELECT"),
        ({"datasets": [{**reply()["datasets"][0], "sql": "SELECT 'abc FROM analytics.sales"},
                       *reply()["datasets"][1:]]}, "does not parse"),
    ],
)  # fmt: skip
def test_semantic_errors_retried_once_then_raise(spec, catalog, over, error):
    with pytest.raises(PlanError, match=error):
        plan_for(spec, catalog, [reply(**over), reply(**over)])
    _, llm = plan_for(spec, catalog, [reply(**over), reply()])
    assert len(llm.calls) == 2 and error in llm.calls[1]["prompt"]


def test_auto_approve_when_all_high_confidence(tmp_path, spec, catalog):
    store = RunStore(tmp_path / "runs")
    rid = _parsed_run(store, spec)
    stages = default_stages(_deps(FakeLLM({"planner": [reply()]}), catalog, auto=True))
    stages[S.BUILDING] = lambda ctx: StageResult(S.FAILED, note="stop here")
    assert Runner(store, stages).advance(rid) == S.FAILED
    states = [e["to"] for e in _events(store, rid) if e["kind"] == "transition"]
    assert states[-3:] == ["PLANNED", "BUILDING", "FAILED"]
    store.close()


def test_change_request_without_source_change_skips_gate(tmp_path, spec, catalog):
    store = RunStore(tmp_path / "runs")
    rid = _parsed_run(store, spec)
    changed = reply(tasks=[{"owner": "ui", "description": "make the bar chart bigger"}])
    llm = FakeLLM({"planner": [reply(), changed]})
    runner = Runner(store, _through_preview(_deps(llm, catalog)))
    assert runner.advance(rid) == S.AWAITING_PLAN_APPROVAL
    assert runner.approve(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert runner.request_changes(rid, "bigger bar chart", "barSalesByRegion") == (
        S.AWAITING_PREVIEW_APPROVAL
    )
    run_dir = store.path(rid)
    assert (run_dir / "plan.v1.json").is_file()
    assert "bigger bar chart" in llm.calls[1]["prompt"]
    assert "write the four datasets" in llm.calls[1]["prompt"]  # previous plan included
    assert store.get(rid).meta["source_map_changed"] is False
    ev = [e for e in _events(store, rid) if e["kind"] == "plan_changed"]
    assert ev[0]["changed"] == ["tasks"]
    store.close()


def test_change_request_touching_source_map_goes_to_gate(tmp_path, spec, catalog):
    spec.tables.append(Table(name="Targets", columns=[Column(name="zzz_target", data_type="int64")]))
    store = RunStore(tmp_path / "runs")
    rid = _parsed_run(store, spec)
    remap = [{"source_id": None, "spec_table": "Targets", "target_table": "analytics.customers",
              "confidence": 1.0, "method": "user", "evidence": "user said so"}]  # fmt: skip
    llm = FakeLLM({"planner": [reply(), reply(source_map=remap)]})
    runner = Runner(store, _through_preview(_deps(llm, catalog)))
    runner.advance(rid)
    assert runner.approve(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert runner.request_changes(rid, "Targets live in analytics.customers") == (
        S.AWAITING_PLAN_APPROVAL
    )
    assert store.get(rid).meta["source_map_changed"] is True
    plan = Plan.model_validate_json((store.path(rid) / "plan.json").read_text(encoding="utf-8"))
    (targets,) = [m for m in plan.source_map if m.spec_table == "Targets"]
    assert targets.target_table == "analytics.customers"
    store.close()


def _deps(llm, catalog, auto=False):
    thresholds = {**config.load("thresholds"), "auto_approve_plan_when_all_high_confidence": auto}
    return StageDeps(llm=llm, catalog=catalog, chart_map=CHART_MAP, thresholds=thresholds)


def _through_preview(deps):
    stages = default_stages(deps)
    stages[S.BUILDING] = lambda ctx: StageResult(S.INTEGRATING)
    stages[S.INTEGRATING] = lambda ctx: StageResult(S.VERIFYING)
    stages[S.VERIFYING] = lambda ctx: StageResult(S.AWAITING_PREVIEW_APPROVAL)
    return stages


def _parsed_run(store, spec):
    rid = store.create("sales.zip", {}).id
    save_spec(spec, store.path(rid) / "spec.json")
    store.transition(rid, S.INTAKE)
    store.transition(rid, S.PARSED)
    return rid


def _events(store, rid):
    lines = (store.path(rid) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]

