import json

import pytest

from cortino.building.data_builder import build_data
from cortino.building.ui_builder import build_ui
from cortino.checking import CheckResult
from cortino.checking.chart_alignment import check_chart_alignment
from cortino.checking.code_security import check_code_security
from cortino.checking.lineage import check_lineage
from cortino.llm import FakeLLM
from cortino.plan.models import Unsupported
from cortino.spec.models import Filter
from cortino.tools.repo_tools import RepoTools

BUILT = ["barSalesByRegion", "cardTotalSales", "lineSalesByMonth", "tableByCategory"]
TITLES = {"charts": [{"id": i, "title": i, "number_format": None} for i in BUILT]}
CHECKS = [check_lineage, check_chart_alignment, check_code_security]


@pytest.fixture
def dash(tmp_path, spec, plan):
    tools = RepoTools(tmp_path / "wt")
    build_data(plan, spec, tools, FakeLLM({}), None)
    build_ui(plan, spec, tools, FakeLLM({"builder": [TITLES]}), None)
    return tools.root / "dashboards" / "sales"


def _owners(r: CheckResult) -> set[str]:
    return {f.owner for f in r.findings}


def _edit_config(dash, fn):
    path = dash / "dashboard.json"
    cfg = json.loads(path.read_text(encoding="utf-8"))
    fn(cfg)
    path.write_text(json.dumps(cfg), encoding="utf-8")


def _chart(cfg, cid):
    return next(c for p in cfg["pages"] for c in p["charts"] if c["id"] == cid)


def _dataset(dash, name, sql):
    (dash / "datasets" / f"{name}.sql").write_text(sql, encoding="utf-8")


@pytest.mark.parametrize("check", CHECKS)
def test_real_build_passes(check, dash, spec, plan):
    r = check(dash, spec, plan)
    assert r.passed and r.findings == [], r.findings


def test_unmapped_table_fails_lineage(dash, spec, plan):
    _dataset(dash, "total_sales", "SELECT SUM(x) AS total_sales FROM analytics.secret")
    r = check_lineage(dash, spec, plan)
    assert not r.passed and _owners(r) == {"data"}
    (f,) = r.findings
    assert f.check == "lineage" and f.evidence["table"] == "analytics.secret"
    assert f.evidence["file"] == "datasets/total_sales.sql" and f.visual_id == "cardTotalSales"


def test_view_resolved_to_base_table(dash, spec, plan):
    (dash / "views.sql").write_text("CREATE OR REPLACE VIEW v_sales AS SELECT * FROM analytics.sales;", encoding="utf-8")
    _dataset(dash, "total_sales", "SELECT SUM(amount) AS total_sales FROM v_sales")
    assert check_lineage(dash, spec, plan).passed


def test_nested_views_resolve_and_expose_unmapped_base(dash, spec, plan):
    ok = "CREATE VIEW v1 AS SELECT * FROM analytics.sales;\nCREATE VIEW v2 AS SELECT * FROM v1;\n"
    (dash / "views.sql").write_text(ok, encoding="utf-8")
    _dataset(dash, "total_sales", "SELECT SUM(amount) AS total_sales FROM v2")
    assert check_lineage(dash, spec, plan).passed
    bad = "CREATE VIEW v3 AS SELECT * FROM analytics.hidden;\nCREATE VIEW v4 AS SELECT * FROM v3;\n"
    (dash / "views.sql").write_text(bad, encoding="utf-8")
    _dataset(dash, "total_sales", "SELECT SUM(amount) AS total_sales FROM v4")
    r = check_lineage(dash, spec, plan)
    assert {f.evidence["table"] for f in r.findings} == {"analytics.hidden"}
    assert {f.evidence["file"] for f in r.findings} == {"views.sql", "datasets/total_sales.sql"}


def test_cte_name_does_not_hide_a_table_read_in_another_scope(dash, spec, plan):
    sql = "SELECT (WITH x AS (SELECT 1) SELECT 1) AS a, SUM(amount) AS total_sales FROM x"
    _dataset(dash, "total_sales", sql)
    assert {f.evidence["table"] for f in check_lineage(dash, spec, plan).findings} == {"x"}


def test_cte_named_after_the_table_it_reads_does_not_hide_it(dash, spec, plan):
    _dataset(dash, "total_sales", "WITH secret AS (SELECT * FROM secret) SELECT SUM(x) AS total_sales FROM secret")
    assert {f.evidence["table"] for f in check_lineage(dash, spec, plan).findings} == {"secret"}


def test_later_cte_may_read_an_earlier_one(dash, spec, plan):
    sql = "WITH a AS (SELECT amount FROM analytics.sales), b AS (SELECT amount FROM a) SELECT SUM(amount) AS total_sales FROM b"
    _dataset(dash, "total_sales", sql)
    assert check_lineage(dash, spec, plan).passed


def test_view_with_column_list_is_checked(dash, spec, plan):
    (dash / "views.sql").write_text("CREATE VIEW analytics.leak(a) AS SELECT * FROM read_csv('C:/secrets.csv');", encoding="utf-8")
    assert not check_lineage(dash, spec, plan).passed
    (dash / "views.sql").write_text("CREATE VIEW v_s(a) AS SELECT amount FROM analytics.sales;", encoding="utf-8")
    _dataset(dash, "total_sales", "SELECT SUM(a) AS total_sales FROM v_s")
    assert check_lineage(dash, spec, plan).passed


def test_cte_names_are_ignored(dash, spec, plan):
    _dataset(dash, "total_sales", "WITH t AS (SELECT amount FROM analytics.sales) SELECT SUM(amount) AS total_sales FROM t")
    assert check_lineage(dash, spec, plan).passed


@pytest.mark.parametrize("src", ["read_csv('a.csv')", "'a.csv'", "analytics.sales, read_parquet('p.parquet')"])
def test_file_sources_fail_lineage(dash, spec, plan, src):
    _dataset(dash, "total_sales", f"SELECT 1 AS total_sales FROM {src}")
    r = check_lineage(dash, spec, plan)
    assert not r.passed and _owners(r) == {"data"}


def test_unqualified_table_resolves_when_unambiguous_else_fails(dash, spec, plan):
    _dataset(dash, "total_sales", "SELECT SUM(amount) AS total_sales FROM sales")
    assert check_lineage(dash, spec, plan).passed
    _dataset(dash, "total_sales", "SELECT 1 AS total_sales FROM mystery")
    assert not check_lineage(dash, spec, plan).passed


@pytest.mark.parametrize("check", CHECKS)
def test_malformed_files_are_findings_not_exceptions(check, dash, spec, plan):
    _dataset(dash, "total_sales", "SELEC FROM WHERE ((")
    (dash / "views.sql").write_text("CREATE VIEW ((", encoding="utf-8")
    (dash / "dashboard.json").write_text("{not json", encoding="utf-8")
    r = check(dash, spec, plan)
    assert not r.passed and r.findings
    assert all(f.check and f.evidence.get("file") for f in r.findings)


@pytest.mark.parametrize("check", CHECKS)
def test_missing_dashboard_files_are_findings(check, tmp_path, spec, plan):
    r = check(tmp_path, spec, plan)
    assert not r.passed or check is check_lineage


def test_wrong_aggregation_fails_alignment_for_data(dash, spec, plan):
    _dataset(dash, "sales_by_region", plan.datasets[0].sql.replace("SUM(s.amount)", "AVG(s.amount)"))
    r = check_chart_alignment(dash, spec, plan)
    assert not r.passed and _owners(r) == {"data"}
    assert r.findings[0].visual_id == "barSalesByRegion" and r.findings[0].fixable


def test_wrong_component_fails_alignment_for_ui(dash, spec, plan):
    _edit_config(dash, lambda cfg: _chart(cfg, "barSalesByRegion").update(component="line"))
    r = check_chart_alignment(dash, spec, plan)
    assert not r.passed and _owners(r) == {"ui"}
    assert not any(f.fixable for f in r.findings)  # the component is computed in code


def test_approved_deviation_passes(dash, spec, plan):
    _edit_config(dash, lambda cfg: _chart(cfg, "barSalesByRegion").update(component="column"))
    assert not check_chart_alignment(dash, spec, plan).passed
    plan.unsupported.append(Unsupported(item="visual:barSalesByRegion", reason="x", fallback="column chart"))
    assert check_chart_alignment(dash, spec, plan).passed


def test_deviation_fallback_must_name_the_component(dash, spec, plan):
    _edit_config(dash, lambda cfg: _chart(cfg, "barSalesByRegion").update(component="pie"))
    plan.unsupported.append(Unsupported(item="visual:barSalesByRegion", reason="x", fallback="not rebuilt — review manually"))
    r = check_chart_alignment(dash, spec, plan)
    assert not r.passed and _owners(r) == {"ui"}
    plan.unsupported[-1] = Unsupported(item="visual:barSalesByRegion", reason="x", fallback="pie chart")
    assert check_chart_alignment(dash, spec, plan).passed


def test_aggregating_the_wrong_column_fails_alignment_for_data(dash, spec, plan):
    _dataset(dash, "sales_by_region", plan.datasets[0].sql.replace("SUM(s.amount)", "SUM(s.customer_id)"))
    r = check_chart_alignment(dash, spec, plan)
    assert not r.passed and _owners(r) == {"data"} and r.findings[0].visual_id == "barSalesByRegion"


def test_count_does_not_stand_in_for_count_distinct(dash, spec, plan):
    next(m for m in plan.measures if m.name == "Total Sales").sql_expression = "COUNT(DISTINCT analytics.sales.amount)"
    _dataset(dash, "sales_by_region", plan.datasets[0].sql.replace("SUM(s.amount)", "COUNT(s.amount)"))
    bar = lambda: [f for f in check_chart_alignment(dash, spec, plan).findings if f.visual_id == "barSalesByRegion"]
    assert bar()
    _dataset(dash, "sales_by_region", plan.datasets[0].sql.replace("SUM(s.amount)", "COUNT(DISTINCT s.amount)"))
    assert not bar()


@pytest.mark.parametrize("sel", ["SUM(s.amount) AS other", "1 AS other"])
def test_encoding_the_dataset_does_not_return_fails_alignment(dash, spec, plan, sel):
    _dataset(dash, "sales_by_region", plan.datasets[0].sql.replace("SUM(s.amount) AS total_sales", sel))
    r = check_chart_alignment(dash, spec, plan)
    assert not r.passed and _owners(r) == {"data"}
    assert any("total_sales" in f.message for f in r.findings)


def test_unreturned_encoding_allowed_when_listed_unsupported(dash, spec, plan):
    _dataset(dash, "sales_by_region", plan.datasets[0].sql.replace("SUM(s.amount) AS total_sales", "1 AS other"))
    plan.unsupported.append(Unsupported(item="encoding:barSalesByRegion.Total Sales", reason="x", fallback="left out"))
    assert check_chart_alignment(dash, spec, plan).passed


def test_wrong_encoding_and_top_n_fail_alignment(dash, spec, plan):
    _edit_config(dash, lambda cfg: _chart(cfg, "barSalesByRegion")["encodings"].update(x="total_sales"))
    r = check_chart_alignment(dash, spec, plan)
    assert _owners(r) == {"ui"} and not any(f.fixable for f in r.findings)
    spec.pages[0].visuals[0].top_n = 3
    r = check_chart_alignment(dash, spec, plan)
    assert any("top_n" in f.message and not f.fixable for f in r.findings)


def test_missing_chart_fails_alignment(dash, spec, plan):
    _edit_config(dash, lambda cfg: cfg["pages"][0]["charts"].pop(0))
    r = check_chart_alignment(dash, spec, plan)
    assert not r.passed and _owners(r) == {"ui"}


def test_string_concat_filter_fails_security(dash, spec, plan):
    sql = plan.datasets[0].sql.replace("c.region = $region", "c.region = '' || $region || ''")
    _dataset(dash, "sales_by_region", sql)
    r = check_code_security(dash, spec, plan)
    assert not r.passed and _owners(r) == {"data"}


@pytest.mark.parametrize(
    "bad", ["c.region = '{region}'", "c.region = '$region'", "c.region = %s", "c.region = 'a' + $region", "c.region = 'West'",
        "c.region = {region}", "c.region = {{ region }}"]
)
def test_other_string_building_fails_security(dash, spec, plan, bad):
    field = {"table": "Customer", "name": "region", "kind": "column"}
    spec.filters.append(Filter(level="report", field=field, operator="eq", values=["West"]))
    sql = f"SELECT SUM(s.amount) AS total_sales FROM analytics.sales s JOIN analytics.customers c ON 1=1 WHERE ($region IS NULL OR {bad})"
    _dataset(dash, "total_sales", sql)
    assert not check_code_security(dash, spec, plan).passed


@pytest.mark.parametrize(
    "edit",
    [
        lambda q: q.replace("SELECT c.region,", "SELECT c.region || ' (' || c.region || ')' AS region,"),
        lambda q: q.replace("GROUP BY", "AND c.region LIKE '%south%' GROUP BY"),
        lambda q: "-- total {sales}" + chr(10) + q,
        lambda q: q.replace("SELECT c.region,", "SELECT 'Region ' || c.region AS region,"),
    ],
)
def test_legitimate_sql_passes_security(dash, spec, plan, edit):
    _dataset(dash, "sales_by_region", edit(plan.datasets[0].sql))
    r = check_code_security(dash, spec, plan)
    assert r.passed, r.findings


def test_multiple_statements_fail_security(dash, spec, plan):
    _dataset(dash, "total_sales", "SELECT 1 AS total_sales; DROP TABLE analytics.sales")
    assert not check_code_security(dash, spec, plan).passed


def test_undeclared_param_fails(dash, spec, plan):
    _dataset(dash, "sales_by_region", plan.datasets[0].sql.replace("$region", "$country"))
    r = check_code_security(dash, spec, plan)
    assert not r.passed and _owners(r) == {"data"}
    assert any("country" in f.message for f in r.findings)


def test_unused_declared_param_fails(dash, spec, plan):
    _dataset(dash, "total_sales", "SELECT SUM(amount) AS total_sales FROM analytics.sales")
    assert not check_code_security(dash, spec, plan).passed


def test_config_filter_on_undeclared_param_fails_for_ui(dash, spec, plan):
    _edit_config(dash, lambda cfg: cfg["filters"][0].update(param="nope"))
    r = check_code_security(dash, spec, plan)
    assert not r.passed and _owners(r) == {"ui"}


def test_non_view_statement_in_views_sql_fails_for_data(dash, spec, plan):
    (dash / "views.sql").write_text("DROP TABLE analytics.sales;", encoding="utf-8")
    r = check_code_security(dash, spec, plan)
    assert not r.passed and _owners(r) == {"data"}


_REGION_SUBQUERY = ("SELECT c.region, {agg}(s.amount) AS total FROM analytics.sales s JOIN analytics.customers c "
                    "ON s.customer_id = c.customer_id WHERE ($region IS NULL OR c.region = $region) GROUP BY 1")  # fmt: skip


@pytest.mark.parametrize(
    "outer, inner, passed",
    [
        ("SUM", "SUM", True),  # a sum of sums is still the sum
        ("", "SUM", True),  # the subquery's column taken as it is
        ("MAX", "SUM", False),  # re-aggregating the sums another way is not Total Sales
        ("AVG", "SUM", False),
        ("COUNT", "SUM", False),
        ("SUM", "AVG", False),
    ],
)  # fmt: skip
def test_aggregation_is_traced_through_a_joined_subquery(dash, spec, plan, outer, inner, passed):
    """A hard measure is often computed in a subquery or CTE and joined back (Tableau FIXED):
    the measure's aggregation counts where the column is computed, and any outer aggregation on
    top of it must be one the measure (or the shelf) asks for."""
    column = f"{outer}(t.total)" if outer else "t.total"
    group = " GROUP BY 1" if outer else ""
    _dataset(dash, "sales_by_region",
             f"SELECT t.region, {column} AS total_sales FROM ({_REGION_SUBQUERY.format(agg=inner)}) t{group}")  # fmt: skip
    r = check_chart_alignment(dash, spec, plan)
    assert r.passed is passed, r.findings
    if not passed:
        assert "does not aggregate" in r.findings[0].message


def test_shelf_aggregation_may_reaggregate_a_traced_measure(dash, spec, plan):
    """Tableau `[avg:Calc]`: the shelf averages the calc, so AVG over the inner SUM is right."""
    (enc,) = [e for v in spec.pages[0].visuals if v.id == "barSalesByRegion" for e in v.encodings if e.role == "y"]
    enc.field.aggregation = "avg"
    _dataset(dash, "sales_by_region",
             f"SELECT t.region, AVG(t.total) AS total_sales FROM ({_REGION_SUBQUERY.format(agg='SUM')}) t GROUP BY 1")  # fmt: skip
    assert check_chart_alignment(dash, spec, plan).passed


def test_sort_and_filter_alignment_findings_are_not_fixable(dash, spec, plan):
    """Sort and filter controls are computed in code: the UI builder cannot act on them."""
    _edit_config(dash, lambda cfg: _chart(cfg, "barSalesByRegion").update(sort="region asc"))
    (f,) = check_chart_alignment(dash, spec, plan).findings
    assert "sort" in f.message and not f.fixable
    _edit_config(dash, lambda cfg: (_chart(cfg, "barSalesByRegion").update(sort=None), cfg.update(filters=[])))
    field = {"table": "Customer", "name": "region", "kind": "column"}
    spec.pages[0].visuals[0].filters.append(Filter(level="visual", field=field, operator="in", values=["West"]))
    _dataset(dash, "sales_by_region", plan.datasets[0].sql.replace("OR c.region = $region", ""))
    findings = [f for f in check_chart_alignment(dash, spec, plan).findings if "filter" in f.message]
    assert findings and not any(f.fixable for f in findings)
