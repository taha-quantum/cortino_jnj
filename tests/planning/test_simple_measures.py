import pytest

from cortino import config
from cortino.planning.simple_measures import translate_simple
from cortino.planning.source_mapping import map_columns, map_sources
from cortino.spec.models import Measure, load_spec
from cortino.tools.source_catalog import SourceCatalog
from cortino.tools.warehouse import DuckDBWarehouse


@pytest.fixture
def setup(warehouse, sales_fixture):
    spec = load_spec(sales_fixture / "golden_spec.json")
    wh = DuckDBWarehouse(warehouse, read_only=True)
    cat = SourceCatalog(wh, config.load("sources")["mappings"])
    cmap = map_columns(spec, map_sources(spec, cat)[0], cat)
    yield spec, cmap
    wh.close()


def _m(name, expr, language="dax"):
    return Measure(name=name, table="Sales", expression=expr, language=language)


def test_sum_translated(setup):
    spec, cmap = setup
    t = translate_simple(spec.measures[0], cmap, spec.measures)
    assert t.sql_expression == "SUM(analytics.sales.amount)"
    assert t.difficulty == "simple" and not t.flagged and t.test_query is None
    assert translate_simple(spec.measures[1], cmap, spec.measures).sql_expression == (
        "COUNT(DISTINCT analytics.sales.order_id) + "
        "COALESCE(MAX(CASE WHEN analytics.sales.order_id IS NULL THEN 1 ELSE 0 END), 0)"
    )


def test_quoted_table_and_other_aggregates(setup):
    _, cmap = setup
    for expr, want in [
        ("AVERAGE('Sales'[amount])", "AVG(analytics.sales.amount)"),
        ("COUNTROWS(Sales)", "COUNT(*)"),
        ("MAX(Sales[quantity])", "MAX(analytics.sales.quantity)"),
    ]:
        assert translate_simple(_m("x", expr), cmap, []).sql_expression == want


def test_divide_uses_nullif(setup):
    spec, cmap = setup
    t = translate_simple(spec.measures[2], cmap, spec.measures)
    assert t.sql_expression == (
        "(SUM(analytics.sales.amount)) / NULLIF(COUNT(DISTINCT analytics.sales.order_id) + "
        "COALESCE(MAX(CASE WHEN analytics.sales.order_id IS NULL THEN 1 ELSE 0 END), 0), 0)"
    )


def test_divide_of_hard_measure_is_none(setup):
    spec, cmap = setup
    hard = _m("Hard", "CALCULATE(SUM(Sales[amount]), Sales[quantity] > 1)")
    ms = [*spec.measures, hard, _m("D", "DIVIDE([Total Sales], [Hard])")]
    assert translate_simple(ms[-1], cmap, ms) is None


@pytest.mark.parametrize("expr", [
    "CALCULATE(SUM(Sales[amount]))",
    "SUMX(Sales, Sales[amount])",
    "TOTALYTD(SUM(Sales[amount]), 'Date'[date])",
    "SUM(Sales[nope])",
])  # fmt: skip
def test_calculate_returns_none(setup, expr):
    _, cmap = setup
    assert translate_simple(_m("x", expr), cmap, []) is None


def test_countrows_unmapped_table_is_none(setup):
    _, cmap = setup
    assert translate_simple(_m("x", "COUNTROWS(Bogus)"), cmap, []) is None


def test_countrows_home_table_case_insensitive(setup):
    _, cmap = setup
    assert translate_simple(_m("x", "COUNTROWS('sales')"), cmap, []).sql_expression == "COUNT(*)"


def test_countrows_other_table_is_none(setup):
    _, cmap = setup
    assert translate_simple(_m("x", "COUNTROWS(Customer)"), cmap, []) is None


def test_divide_across_tables_is_none(setup):
    spec, cmap = setup
    cust = Measure(name="CustomerRows", table="Customer", expression="COUNTROWS(Customer)", language="dax")
    ms = [*spec.measures, cust, _m("D", "DIVIDE([Total Sales], [CustomerRows])")]
    assert translate_simple(ms[-1], cmap, ms) is None
    cross = _m("E", "DIVIDE([Total Sales], [CustCount])")
    cc = Measure(name="CustCount", table="Customer", expression="DISTINCTCOUNT(Customer[customer_id])", language="dax")
    assert translate_simple(cross, cmap, [*spec.measures, cc, cross]) is None


def test_distinctcount_counts_blank_like_dax(setup, warehouse):
    import duckdb

    _, cmap = setup
    sql = translate_simple(_m("x", "DISTINCTCOUNT(Sales[order_id])"), cmap, []).sql_expression
    con = duckdb.connect()
    con.execute("create schema analytics; create table analytics.sales(order_id int)")
    con.execute("insert into analytics.sales values (NULL), (NULL), (2)")
    assert con.execute(f"select {sql} from analytics.sales").fetchone()[0] == 2
    con.execute("delete from analytics.sales")
    assert con.execute(f"select {sql} from analytics.sales").fetchone()[0] == 0


def test_distinctcountnoblank(setup):
    _, cmap = setup
    t = translate_simple(_m("x", "DISTINCTCOUNTNOBLANK(Sales[order_id])"), cmap, [])
    assert t.sql_expression == "COUNT(DISTINCT analytics.sales.order_id)"
