"""Datasets get a deterministic ORDER BY when the planner's SQL has none."""

import pytest

from cortino.building.data_builder import build_data, with_default_order
from cortino.llm import FakeLLM
from cortino.spec.models import Encoding, FieldRef, Sort, Visual
from cortino.tools.repo_tools import RepoTools
from cortino.tools.warehouse import DuckDBWarehouse

BY_REGION = (
    "SELECT c.region, SUM(s.amount) AS total_sales FROM analytics.sales s "
    "JOIN analytics.customers c ON s.customer_id = c.customer_id GROUP BY 1"
)


def visual(type_, x=None, y=None, values=(), sort=None):
    enc = []
    if x:
        enc.append(Encoding(role="x", field=FieldRef(table="T", name=x, kind="column")))
    if y:
        enc.append(Encoding(role="y", field=FieldRef(table="T", name=y, kind="measure")))
    for v in values:
        enc.append(Encoding(role="values", field=FieldRef(table="T", name=v, kind="column")))
    srt = [Sort(field=FieldRef(table="T", name=sort[0], kind="column"), direction=sort[1])] if sort else []
    return Visual(id="v", page="p", type=type_, encodings=enc, sort=srt)


def order_clause(sql: str) -> str:
    return sql.rstrip().rsplit("\n", 1)[-1]


def test_power_bi_bar_orders_by_measure_largest_first():
    out = with_default_order(BY_REGION, visual("bar", x="region", y="Total Sales"), "pbip_tmdl")
    assert out.startswith(BY_REGION)
    assert order_clause(out) == "ORDER BY 2 DESC, 1"


def test_tableau_bar_orders_by_dimension():
    out = with_default_order(BY_REGION, visual("bar", x="region", y="Total Sales"), "twbx")
    assert order_clause(out) == "ORDER BY 1"


def test_line_orders_by_x_axis():
    sql = "SELECT d.year, d.month, SUM(s.amount) AS total_sales FROM t GROUP BY 1, 2"
    out = with_default_order(sql, visual("line", x="month", y="Total Sales"), "pbip_tmdl")
    assert order_clause(out) == "ORDER BY 2, 1"


def test_table_orders_by_its_dimensions():
    sql = "SELECT p.category, SUM(s.amount) AS total_sales, COUNT(DISTINCT s.order_id) AS order_count FROM t GROUP BY 1"
    out = with_default_order(sql, visual("table", values=["category", "Total Sales", "Order Count"]), "pbit")
    assert order_clause(out) == "ORDER BY 1"


def test_spec_sort_wins():
    v = visual("bar", x="region", y="Total Sales", sort=("region", "desc"))
    assert order_clause(with_default_order(BY_REGION, v, "pbip_tmdl")) == "ORDER BY 1 DESC"


def test_window_function_counts_as_a_measure():
    sql = "SELECT d.month, SUM(SUM(s.amount)) OVER (ORDER BY d.month) AS running FROM t GROUP BY 1"
    assert order_clause(with_default_order(sql, None, "pbip_tmdl")) == "ORDER BY 1"


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT SUM(s.amount) AS total_sales FROM t",  # a card: one row, nothing to order by
        BY_REGION + " ORDER BY 1",  # already ordered
        BY_REGION + " LIMIT 5",  # ORDER BY cannot follow LIMIT
        "SELECT * FROM t",  # columns unknown
        "not sql at all (",
        "SELECT 1; SELECT 2",
    ],
)
def test_left_unchanged(sql):
    assert with_default_order(sql, visual("bar", x="region", y="Total Sales"), "pbip_tmdl") == sql


def test_trailing_semicolon_and_line_comment():
    sql = BY_REGION + " -- by region\n;\n"
    out = with_default_order(sql, None, "pbip_tmdl")
    assert out.endswith("-- by region\nORDER BY 1\n")


def test_built_datasets_return_rows_in_a_stable_order(tmp_path, spec, plan, warehouse):
    """End to end: the written dataset files run in the warehouse, through the same LIMIT wrapper the
    preview and the checks use, and come back in the default order every time."""
    tools = RepoTools(tmp_path)
    build_data(plan, spec, tools, FakeLLM({}), None)
    db = DuckDBWarehouse(warehouse, read_only=True)
    try:
        def rows(ds):
            sql = tools.read_file(f"dashboards/sales/datasets/{ds}.sql")
            return [tuple(r.values()) for r in db.query(sql, {"region": None}, limit=1000)]

        region = rows("sales_by_region")
        assert [r[1] for r in region] == sorted((r[1] for r in region), reverse=True)  # largest first
        assert [r[0] for r in rows("sales_by_month")] == list(range(1, 13))
        categories = [r[0] for r in rows("by_category")]
        assert categories == sorted(categories)
        assert all(rows(ds) == rows(ds) for ds in ("sales_by_region", "sales_by_month", "by_category"))
    finally:
        db.close()
