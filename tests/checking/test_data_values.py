import datetime
import shutil

import pytest

from cortino.building.data_builder import build_data
from cortino.building.ui_builder import build_ui
from cortino.checking import data_values
from cortino.checking.data_values import NO_GROUND_TRUTH, check_data_values, compare
from cortino.llm import FakeLLM
from cortino.plan.models import Plan, ViewDef
from cortino.spec.models import load_spec
from cortino.tools.repo_tools import RepoTools
from cortino.tools.source_catalog import SourceCatalog
from cortino.tools.warehouse import DuckDBWarehouse, throwaway_copy

BUILT = ["barSalesByRegion", "cardTotalSales", "lineSalesByMonth", "tableByCategory"]
TITLES = {"charts": [{"id": i, "title": i, "number_format": None} for i in BUILT]}
# What a checker writes from the spec alone, in plan.visual_map order.
INDEPENDENT = [
    ("SELECT c.region, SUM(s.amount) AS total_sales FROM analytics.sales s "
     "JOIN analytics.customers c ON s.customer_id = c.customer_id GROUP BY 1"),
    "SELECT SUM(amount) AS total_sales FROM analytics.sales",
    ("```sql\nSELECT d.month, SUM(s.amount) AS total_sales FROM analytics.sales s "
     "JOIN analytics.dates d ON s.order_date = d.date GROUP BY 1\n```"),
    ("SELECT p.category, SUM(s.amount) AS total_sales, COUNT(DISTINCT s.order_id) AS order_count "
     "FROM analytics.sales s JOIN analytics.products p ON s.product_id = p.product_id GROUP BY 1"),
]  # fmt: skip
TOL = {"tol_abs": 0.01, "tol_rel": 0.001}


# --- compare -----------------------------------------------------------------------------------


def test_within_tolerance_passes():
    actual = [{"Region": " East ", "total_sales": 1.004}, {"Region": "West", "total_sales": 5}]
    expected = [{"region": "West", "Total Sales": "5.00"}, {"region": "East", "Total Sales": "1.00"}]
    assert compare(actual, expected, ["region"], **TOL).passed
    out = compare(actual, [{"region": "East", "Total Sales": "1.02"}, expected[0]], ["region"], **TOL)
    assert not out.passed
    (d,) = out.value_diffs
    assert d["column"] == "total_sales" and d["key"] == {"region": "East"}


def test_relative_tolerance():
    assert compare([{"v": 1_000_000.9}], [{"v": "1000000"}], [], **TOL).passed  # 0.9 <= 0.1% of 1e6
    assert not compare([{"v": 1_001_001}], [{"v": "1000000"}], [], **TOL).passed


def test_missing_key_fails():
    out = compare([{"month": 1, "v": 1}], [{"month": "1", "v": "1"}, {"month": "2", "v": "2"}], ["month"], **TOL)
    assert not out.passed and len(out.missing_keys) == 1 and out.extra_keys == []
    out = compare([{"month": 1, "v": 1}, {"month": 3, "v": 3}], [{"month": "1", "v": "1"}], ["month"], **TOL)
    assert not out.passed and len(out.extra_keys) == 1


def test_duplicate_key_fails():
    rows = [{"k": "a", "v": 1}, {"k": "a", "v": 1}]
    assert not compare(rows, [{"k": "a", "v": 1}], ["k"], **TOL).passed
    assert not compare([{"k": "a", "v": 1}], rows, ["k"], **TOL).passed
    assert not compare([{"v": 1}, {"v": 1}], [{"v": 1}], [], **TOL).passed  # a card has one row


def test_none_vs_zero_fails():
    assert not compare([{"v": None}], [{"v": 0}], [], **TOL).passed
    assert not compare([{"v": 0}], [{"v": ""}], [], **TOL).passed  # an empty CSV cell is BLANK
    assert compare([{"v": None}], [{"v": ""}], [], **TOL).passed


def test_null_key_matches_empty_csv_cell():
    assert compare([{"r": None, "v": 1}], [{"r": "", "v": "1"}], ["r"], **TOL).passed
    assert compare([{"r": None, "v": 1}], [{"r": "  ", "v": "1"}], ["r"], **TOL).passed


def test_temporal_keys_normalise_to_one_form():
    jan = datetime.datetime(2024, 1, 1)
    assert compare([{"m": jan, "v": 1}], [{"m": "2024-01-01", "v": "1"}], ["m"], **TOL).passed
    assert compare([{"m": jan, "v": 1}], [{"m": datetime.date(2024, 1, 1), "v": 1}], ["m"], **TOL).passed
    five = datetime.datetime(2024, 1, 1, 5)
    assert compare([{"m": five, "v": 1}], [{"m": "2024-01-01 05:00:00", "v": "1"}], ["m"], **TOL).passed
    assert compare([{"m": five, "v": 1}], [{"m": "2024-01-01T05:00:00", "v": "1"}], ["m"], **TOL).passed
    assert not compare([{"m": five, "v": 1}], [{"m": "2024-01-01", "v": "1"}], ["m"], **TOL).passed


def test_differing_rows_capped_at_20():
    actual = [{"k": i, "v": 0} for i in range(30)]
    expected = [{"k": i, "v": 1} for i in range(30)]
    out = compare(actual, expected, ["k"], **TOL)
    assert not out.passed and len(out.value_diffs) == 20


# --- check_data_values -------------------------------------------------------------------------


@pytest.fixture
def spec(sales_fixture):
    return load_spec(sales_fixture / "golden_spec.json")


@pytest.fixture
def plan(sales_fixture):
    return Plan.model_validate_json((sales_fixture / "plan.json").read_text(encoding="utf-8"))


@pytest.fixture
def env(tmp_path, spec, plan, warehouse):
    """The Task 13 build of the sales fixture in a worktree, a throwaway copy and a catalog."""
    source = DuckDBWarehouse(warehouse, read_only=True)
    tools = RepoTools(tmp_path / "wt")
    build_data(plan, spec, tools, FakeLLM({}), None)
    build_ui(plan, spec, tools, FakeLLM({"builder": [TITLES]}), None)
    db = throwaway_copy(source, tmp_path / "db")
    yield tools.root, db, SourceCatalog(source, {})
    db.close()
    source.close()


def _only(plan, visual_id):
    plan.visual_map = [m for m in plan.visual_map if m.visual_id == visual_id]


def test_real_sales_build_passes_against_expected(env, spec, plan, sales_fixture):
    wt, db, catalog = env
    result = check_data_values(wt, spec, plan, db, sales_fixture / "expected", catalog,
                               FakeLLM({"checker": list(INDEPENDENT)}))  # fmt: skip
    assert result.name == "data_values"
    assert result.passed, result.findings
    table = result.evidence["visuals"]["tableByCategory"]
    assert table["ground_truth"] == "tableByCategory.csv"
    assert "not_compared" not in table
    assert result.evidence["visuals"]["cardTotalSales"]["independent_query"] == INDEPENDENT[1]


def test_ground_truth_mismatch_fails_with_rows(env, spec, plan, sales_fixture, tmp_path):
    wt, db, catalog = env
    gt = tmp_path / "gt"
    shutil.copytree(sales_fixture / "expected", gt)
    path = gt / "barSalesByRegion.csv"
    path.write_text(path.read_text(encoding="utf-8").replace("East,28217.85", "East,99.00"), encoding="utf-8")
    _only(plan, "barSalesByRegion")
    result = check_data_values(wt, spec, plan, db, gt, catalog, FakeLLM({"checker": INDEPENDENT[:1]}))
    assert not result.passed
    (f,) = result.findings
    assert (f.check, f.owner, f.visual_id) == ("data_values", "data", "barSalesByRegion")
    assert f.evidence["against"] == "ground_truth"
    (d,) = f.evidence["value_diffs"]
    assert d["key"] == {"region": "East"} and d["column"] == "total_sales"


def test_independent_mismatch_fails(env, spec, plan):
    wt, db, catalog = env
    _only(plan, "cardTotalSales")
    wrong = "SELECT SUM(amount) * 2 AS total_sales FROM analytics.sales"
    result = check_data_values(wt, spec, plan, db, None, catalog, FakeLLM({"checker": [wrong]}))
    (f,) = result.findings
    assert not result.passed and f.evidence["against"] == "independent_query" and f.owner == "data"


def test_no_ground_truth_evidence_string_exact(env, spec, plan):
    wt, db, catalog = env
    result = check_data_values(wt, spec, plan, db, None, catalog, FakeLLM({"checker": list(INDEPENDENT)}))
    assert result.passed, result.findings
    assert NO_GROUND_TRUTH == "none — independent query only"
    assert all(v["ground_truth"] == "none — independent query only" for v in result.evidence["visuals"].values())


def test_independent_prompt_excludes_builder_sql(env, spec, plan):
    wt, db, catalog = env
    plan.views = [ViewDef(name="sales_v", sql="SELECT * FROM analytics.sales WHERE amount > 0", grain="line")]
    llm = FakeLLM({"checker": list(INDEPENDENT)})
    check_data_values(wt, spec, plan, db, None, catalog, llm)
    assert [c["role"] for c in llm.calls] == ["checker"] * 4
    secret = [d.sql for d in plan.datasets] + [m.sql_expression for m in plan.measures] + [plan.views[0].sql]
    secret += [p.read_text(encoding="utf-8") for p in wt.rglob("*") if p.is_file()]
    for call in llm.calls:
        text = call["system"] + call["prompt"]
        assert not [s for s in secret if s.strip() and s in text]
    assert "DISTINCTCOUNT(Sales[order_id])" in llm.calls[3]["prompt"]  # original expressions are given
    assert {t.__name__ for t in llm.calls[0]["tools"]} == {"list_sources", "describe_table", "run_readonly_sql"}


def test_malformed_independent_sql_retried_once_then_fails(env, spec, plan):
    wt, db, catalog = env
    _only(plan, "cardTotalSales")
    llm = FakeLLM({"checker": ["DELETE FROM analytics.sales", "SELECT FROM WHERE ((("]})
    result = check_data_values(wt, spec, plan, db, None, catalog, llm)
    assert len(llm.calls) == 2 and "single SELECT" in llm.calls[1]["prompt"]
    assert not result.passed
    (f,) = result.findings
    assert (f.check, f.owner, f.visual_id) == ("data_values", "data", "cardTotalSales")
    assert f.evidence["note"] == "independent query unavailable"
    assert not f.fixable  # a checker-side failure: no builder can fix it


def test_ground_truth_still_passes_when_independent_unavailable(env, spec, plan, sales_fixture):
    wt, db, catalog = env
    _only(plan, "cardTotalSales")
    llm = FakeLLM({"checker": ["nope", "nope"]})
    result = check_data_values(wt, spec, plan, db, sales_fixture / "expected", catalog, llm)
    assert result.passed
    assert result.evidence["visuals"]["cardTotalSales"]["independent_query"].startswith("unavailable")


def test_large_dataset_uses_bounded_comparison(env, spec, plan, sales_fixture, monkeypatch):
    wt, db, catalog = env
    monkeypatch.setattr(data_values, "MAX_ROWS", 2)
    _only(plan, "barSalesByRegion")
    result = check_data_values(wt, spec, plan, db, sales_fixture / "expected", catalog,
                               FakeLLM({"checker": INDEPENDENT[:1]}))  # fmt: skip
    assert result.passed, result.findings
    assert result.evidence["visuals"]["barSalesByRegion"]["comparison"] == "bounded comparison"
    bad = INDEPENDENT[0].replace("SUM(s.amount)", "SUM(s.amount) * 1.01")
    result = check_data_values(wt, spec, plan, db, None, catalog, FakeLLM({"checker": [bad]}))
    assert not result.passed


def test_dataset_that_fails_to_run_is_a_data_finding(env, spec, plan):
    wt, db, catalog = env
    _only(plan, "cardTotalSales")
    (wt / "dashboards" / "sales" / "datasets" / "total_sales.sql").write_text("SELECT nope FROM analytics.sales")
    result = check_data_values(wt, spec, plan, db, None, catalog, FakeLLM({"checker": INDEPENDENT[1:2]}))
    (f,) = result.findings
    assert not result.passed and (f.owner, f.visual_id) == ("data", "cardTotalSales")


@pytest.mark.parametrize(("visual_id", "dataset", "sql", "missing"), [
    ("cardTotalSales", "total_sales", "SELECT 999 AS revenue WHERE $region IS NULL", ["total_sales"]),
    ("barSalesByRegion", "sales_by_region",
     "SELECT c.region, 1 AS amt FROM analytics.customers c WHERE $region IS NULL GROUP BY 1", ["total_sales"]),
    ("tableByCategory", "by_category",
     ("SELECT p.category, SUM(s.amount) AS total_sales FROM analytics.sales s "
      "JOIN analytics.products p ON s.product_id = p.product_id WHERE $region IS NULL GROUP BY 1"), ["order_count"]),
])  # fmt: skip
def test_value_column_missing_from_dataset_fails(env, spec, plan, sales_fixture, visual_id, dataset, sql, missing):
    wt, db, catalog = env
    _only(plan, visual_id)
    (wt / "dashboards" / "sales" / "datasets" / f"{dataset}.sql").write_text(sql)
    llm = FakeLLM({"checker": [INDEPENDENT[BUILT.index(visual_id)]]})
    result = check_data_values(wt, spec, plan, db, sales_fixture / "expected", catalog, llm)
    assert not result.passed
    lacks = [f for f in result.findings if "missing_columns" in f.evidence]
    assert [(f.check, f.owner, f.visual_id) for f in lacks] == [("data_values", "data", visual_id)]
    assert lacks[0].evidence["missing_columns"] == missing
