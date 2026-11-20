import json

import pytest

from cortino.building import BuildError
from cortino.building.data_builder import build_data
from cortino.building.datasets import register_datasets
from cortino.building.ui_builder import build_ui, ui_skipped
from cortino.checking import Finding
from cortino.llm import FakeLLM
from cortino.plan.models import Param, ViewDef
from cortino.platform.config_schema import DashboardConfig
from cortino.spec.models import FieldRef
from cortino.spec.models import Filter as SpecFilter
from cortino.spec.models import Page as SpecPage
from cortino.tools.repo_tools import PathOutsideWorktree, RepoTools

BUILT = ["barSalesByRegion", "cardTotalSales", "lineSalesByMonth", "tableByCategory"]


def titles(ids=BUILT, fmt="#,##0.00"):
    return {"charts": [{"id": i, "title": f"Title {i}", "number_format": fmt} for i in ids]}


def data_finding():
    return Finding(check="dataset_run", owner="data", visual_id="barSalesByRegion",
                   message="column nope not found", evidence={"dataset": "sales_by_region"})  # fmt: skip


def ui_finding():
    return Finding(check="config", owner="ui", visual_id=None, message="bad config", evidence={})


def _files(tmp_path):
    return sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())


def test_data_builder_writes_views_and_datasets(tmp_path, spec, plan):
    plan.views = [ViewDef(name="analytics_sales_v", sql="SELECT * FROM analytics.sales;", grain="order line")]
    llm = FakeLLM({})
    paths = build_data(plan, spec, RepoTools(tmp_path), llm, None)
    assert llm.calls == []  # the first build writes the plan's validated SQL, no LLM
    base = tmp_path / "dashboards" / "sales"
    assert sorted(paths) == sorted([base / "views.sql"] + [base / "datasets" / f"{d.id}.sql" for d in plan.datasets])
    views = (base / "views.sql").read_text(encoding="utf-8")
    assert views == "CREATE OR REPLACE VIEW analytics_sales_v AS\nSELECT * FROM analytics.sales;\n"
    for d in plan.datasets:
        assert (base / "datasets" / f"{d.id}.sql").read_text(encoding="utf-8") == d.sql
    assert all(f.startswith("dashboards/sales/") for f in _files(tmp_path))


def test_data_fix_asks_llm_and_validates_reply(tmp_path, spec, plan):
    tools = RepoTools(tmp_path)
    build_data(plan, spec, tools, FakeLLM({}), None)
    fixed = {d.id: d.sql for d in plan.datasets}
    fixed["sales_by_region"] = fixed["sales_by_region"].replace("c.region,", "c.region AS region,")
    bad = {**fixed, "sales_by_region": "DELETE FROM analytics.sales"}
    unbound = {**fixed, "total_sales": "SELECT SUM(amount) AS total_sales FROM analytics.sales"}
    llm = FakeLLM({"builder": [{"views_sql": "", "datasets": bad},
                               {"views_sql": "", "datasets": fixed}]})  # fmt: skip
    build_data(plan, spec, tools, llm, [data_finding()])
    assert "column nope not found" in llm.calls[0]["prompt"]
    assert plan.datasets[0].sql in llm.calls[0]["prompt"]  # current file contents
    assert "single SELECT" in llm.calls[1]["prompt"]
    assert tools.read_file("dashboards/sales/datasets/sales_by_region.sql") == fixed["sales_by_region"]
    llm = FakeLLM({"builder": [{"views_sql": "", "datasets": unbound}] * 2})
    with pytest.raises(BuildError, match="region"):
        build_data(plan, spec, tools, llm, [data_finding()])


def test_builders_never_write_outside_dashboard_dir(tmp_path, spec, plan):
    root = tmp_path / "wt"
    root.mkdir()
    plan.datasets[0].id = "../../escape"
    with pytest.raises(PathOutsideWorktree):
        build_data(plan, spec, RepoTools(root), FakeLLM({}), None)
    assert not list(tmp_path.glob("escape*")) and not (root / "dashboards" / "escape.sql").exists()


def test_ui_config_validates_and_layout_from_spec(tmp_path, spec, plan):
    llm = FakeLLM({"builder": [titles()]})
    path = build_ui(plan, spec, RepoTools(tmp_path), llm, None)
    assert path == tmp_path / "dashboards" / "sales" / "dashboard.json"
    cfg = DashboardConfig.model_validate(json.loads(path.read_text(encoding="utf-8")))
    assert cfg.title == "Sales"
    (page,) = cfg.pages
    assert (page.name, page.width, page.height) == ("Overview", 1280, 720)
    charts = {c.id: c for c in page.charts}
    assert sorted(charts) == BUILT  # no slicer, no custom visual
    bar = charts["barSalesByRegion"]
    assert (bar.x, bar.y, bar.w, bar.h) == (20, 160, 610, 260)  # spec position
    assert (bar.component, bar.dataset, bar.title) == ("bar", "sales_by_region", "Title barSalesByRegion")
    assert bar.encodings.x == "region" and bar.encodings.y == ["total_sales"]
    assert charts["cardTotalSales"].encodings.value == "total_sales"
    assert charts["tableByCategory"].encodings.x == "category"
    assert bar.number_format == "#,##0.00"
    assert cfg.datasets == register_datasets(plan)
    assert [d.file for d in cfg.datasets] == [f"datasets/{d.id}.sql" for d in plan.datasets]
    prompt = llm.calls[0]["prompt"]
    assert "barSalesByRegion" in prompt and "customXyz" not in prompt


def test_slicer_becomes_filter_bound_to_dataset_param(tmp_path, spec, plan):
    path = build_ui(plan, spec, RepoTools(tmp_path), FakeLLM({"builder": [titles()]}), None)
    cfg = DashboardConfig.model_validate_json(path.read_text(encoding="utf-8"))
    (f,) = cfg.filters
    assert (f.field, f.control, f.param, f.applies_to) == ("region", "multi_select", "region", "all")
    plan.datasets[1].params = []
    plan.datasets[1].sql = "SELECT SUM(s.amount) AS total_sales FROM analytics.sales s"
    plan.datasets[2].params.append(Param(name="order_date", type="date"))
    plan.datasets[2].sql = plan.datasets[2].sql.replace("WHERE (", "WHERE ($order_date IS NULL OR s.order_date = $order_date) AND (")
    spec.pages[0].filters[0].field.table, spec.pages[0].filters[0].field.name = "Sales", "order_date"
    path = build_ui(plan, spec, RepoTools(tmp_path), FakeLLM({"builder": [titles()]}), None)
    cfg = DashboardConfig.model_validate_json(path.read_text(encoding="utf-8"))
    by_param = {f.param: f for f in cfg.filters}
    assert by_param["region"].applies_to == ["barSalesByRegion", "lineSalesByMonth", "tableByCategory"]
    assert (by_param["order_date"].control, by_param["order_date"].applies_to) == (
        "date_range", ["lineSalesByMonth"]
    )


REGION = FieldRef(table="Customer", name="region", kind="column")
CATEGORY = FieldRef(table="Product", name="category", kind="column")


def _filters(plan, spec, tmp_path):
    path = build_ui(plan, spec, RepoTools(tmp_path), FakeLLM({"builder": [titles()]}), None)
    return DashboardConfig.model_validate_json(path.read_text(encoding="utf-8")).filters


def _skipped(plan, spec):
    return {s["item"]: s["reason"] for s in ui_skipped(plan, spec)}


def test_filter_binds_to_the_param_whose_predicate_reads_the_field(tmp_path, spec, plan):
    for d in plan.datasets:
        d.sql = d.sql.replace("$region", "$customer_region")
        d.params = [Param(name="customer_region", type="string")]
    (f,) = _filters(plan, spec, tmp_path)
    assert (f.field, f.param, f.applies_to) == ("region", "customer_region", "all")
    spec.pages[0].filters.append(SpecFilter(level="page", field=CATEGORY, operator="in", values=["Bikes"]))
    assert len(_filters(plan, spec, tmp_path)) == 1
    assert "no dataset parameter filters Product.category" in _skipped(plan, spec)["filter:Product.category (page Overview)"]


def test_page_filter_applies_only_to_its_page_and_merges_with_report_filter(tmp_path, spec, plan):
    line = next(v for v in spec.pages[0].visuals if v.id == "lineSalesByMonth")
    spec.pages[0].visuals.remove(line)
    line.page = "two"
    west = SpecFilter(level="page", field=REGION, operator="in", values=["West"])
    spec.pages.append(SpecPage(id="two", name="Two", visuals=[line], filters=[west]))
    by_charts = {str(f.applies_to): f for f in _filters(plan, spec, tmp_path)}
    assert by_charts[str(["barSalesByRegion", "cardTotalSales", "tableByCategory"])].default is None
    assert by_charts[str(["lineSalesByMonth"])].default == ["West"]
    spec.filters = [SpecFilter(level="report", field=REGION, operator="in", values=["West", "East"])]
    by_charts = {str(f.applies_to): f for f in _filters(plan, spec, tmp_path)}
    assert len(by_charts) == 2  # the report filter is folded into each page's control
    assert by_charts[str(["barSalesByRegion", "cardTotalSales", "tableByCategory"])].default == ["West", "East"]
    assert by_charts[str(["lineSalesByMonth"])].default == ["West"]


def test_visual_level_filter_becomes_a_control_for_that_chart(tmp_path, spec, plan):
    bar = next(v for v in spec.pages[0].visuals if v.id == "barSalesByRegion")
    bar.filters = [SpecFilter(level="visual", field=REGION, operator="in", values=["East"]),
                   SpecFilter(level="visual", field=CATEGORY, operator="in", values=["Bikes"])]  # fmt: skip
    by_charts = {str(f.applies_to): f for f in _filters(plan, spec, tmp_path)}
    assert by_charts[str(["barSalesByRegion"])].default == ["East"]
    assert by_charts[str(["cardTotalSales", "lineSalesByMonth", "tableByCategory"])].default is None
    assert "filter:Product.category (visual barSalesByRegion)" in _skipped(plan, spec)


def test_encoding_without_a_dataset_column_is_recorded(spec, plan):
    plan.datasets[3].sql = plan.datasets[3].sql.replace("AS order_count", "AS orders")
    assert "no column 'order_count'" in _skipped(plan, spec)["encoding:tableByCategory.Order Count"]
    plan.datasets[0].sql = plan.datasets[0].sql.replace("AS total_sales", "AS sales")
    assert "encoding:barSalesByRegion.Total Sales" in _skipped(plan, spec)


def test_invalid_config_from_llm_retried_then_failed(tmp_path, spec, plan):
    tools = RepoTools(tmp_path)
    build_ui(plan, spec, tools, FakeLLM({"builder": [titles()]}), None)
    before = tools.read_file("dashboards/sales/dashboard.json")
    missing = titles(BUILT[1:])
    llm = FakeLLM({"builder": [missing, missing]})
    with pytest.raises(BuildError, match="barSalesByRegion"):
        build_ui(plan, spec, tools, llm, [ui_finding()])
    assert len(llm.calls) == 2
    assert "bad config" in llm.calls[0]["prompt"] and before in llm.calls[0]["prompt"]
    assert "barSalesByRegion" in llm.calls[1]["prompt"]  # the validation error is sent back
    assert tools.read_file("dashboards/sales/dashboard.json") == before  # never a partial config
    llm = FakeLLM({"builder": [missing, titles()]})
    build_ui(plan, spec, tools, llm, [ui_finding()])
    assert len(llm.calls) == 2


@pytest.mark.parametrize(("operator", "values", "data_type"), [
    ("gte", ["West"], "string"), ("lte", ["West"], "string"), ("relative_date", ["last 3 months"], "string"),
    ("between", ["A", "M"], "string"), ("eq", [5], "string"), ("eq", ["5"], "int64"),
])  # fmt: skip
def test_non_in_operator_on_a_bound_filter_is_recorded_as_downgraded(spec, plan, operator, values, data_type):
    """The controls hold values compared by equality: anything else is a downgrade, never silent."""
    next(c for t in spec.tables if t.name == "Customer" for c in t.columns if c.name == "region").data_type = data_type
    spec.filters = [SpecFilter(level="report", field=REGION, operator=operator, values=values)]
    reason = _skipped(plan, spec)["filter:Customer.region (report)"]
    assert operator in reason or "range" in reason


@pytest.mark.parametrize("operator", ["in", "eq"])
def test_in_and_eq_on_text_are_not_downgrades(spec, plan, operator):
    spec.filters = [SpecFilter(level="report", field=REGION, operator=operator, values=["West"])]
    assert "filter:Customer.region (report)" not in _skipped(plan, spec)
