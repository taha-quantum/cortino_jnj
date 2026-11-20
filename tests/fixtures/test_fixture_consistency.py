import csv
import json
from collections import defaultdict
from decimal import Decimal

import duckdb

from cortino.spec.models import load_spec

TABLES = {
    "sales": "sales.csv",
    "customers": "customers.csv",
    "products": "products.csv",
    "dates": "dates.csv",
}


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def _rows(sales_fixture, name):
    header, *rows = _read(sales_fixture / "data" / name)
    return [dict(zip(header, r)) for r in rows]


def _recompute(sales_fixture):
    """Expected visual results, computed in plain Python from the raw CSVs."""
    sales = _rows(sales_fixture, "sales.csv")
    region = {
        r["customer_id"]: r["region"] for r in _rows(sales_fixture, "customers.csv")
    }
    category = {
        r["product_id"]: r["category"] for r in _rows(sales_fixture, "products.csv")
    }
    month = {r["date"]: int(r["month"]) for r in _rows(sales_fixture, "dates.csv")}

    def total(rows):
        return f"{sum(Decimal(r['amount']) for r in rows):.2f}"

    def grouped(key):
        groups = defaultdict(list)
        for r in sales:
            groups[key(r)].append(r)
        return sorted(groups.items())

    return {
        "cardTotalSales": [["Total Sales"], [total(sales)]],
        "barSalesByRegion": [["region", "Total Sales"]]
        + [[g, total(rs)] for g, rs in grouped(lambda r: region[r["customer_id"]])],
        "lineSalesByMonth": [["month", "Total Sales"]]
        + [[str(g), total(rs)] for g, rs in grouped(lambda r: month[r["order_date"]])],
        "tableByCategory": [["category", "Total Sales", "Order Count"]]
        + [
            [g, total(rs), f"{len({r['order_id'] for r in rs}):.2f}"]
            for g, rs in grouped(lambda r: category[r["product_id"]])
        ],
    }


def test_golden_spec_validates(sales_fixture):
    path = sales_fixture / "golden_spec.json"
    spec = load_spec(path)
    # Every field is written out explicitly, so the file equals the model's own dump.
    assert spec.model_dump(mode="json", by_alias=True) == json.loads(
        path.read_text(encoding="utf-8")
    )
    assert spec.name == "Sales"
    assert [t.name for t in spec.tables] == ["Sales", "Customer", "Product", "Date"]
    assert [m.name for m in spec.measures] == [
        "Total Sales",
        "Order Count",
        "Avg Order",
    ]
    assert [r.name for r in spec.rls_roles] == ["RegionManager"]
    assert [u.where for u in spec.unknowns] == ["visual:customXyz"]


def test_golden_unknown_raw_is_visual_json(sales_fixture):
    spec = load_spec(sales_fixture / "golden_spec.json")
    visual = (
        sales_fixture
        / "pbip/Sales.Report/definition/pages/overview/visuals/customXyz/visual.json"
    )
    assert spec.unknowns[0].raw == visual.read_text(encoding="utf-8")


def test_golden_spec_ids_match_pbip_folders(sales_fixture):
    spec = load_spec(sales_fixture / "golden_spec.json")
    pages_dir = sales_fixture / "pbip" / "Sales.Report" / "definition" / "pages"
    page_order = json.loads((pages_dir / "pages.json").read_text(encoding="utf-8"))[
        "pageOrder"
    ]
    assert [p.id for p in spec.pages] == page_order
    for page in spec.pages:
        folders = {d.name for d in (pages_dir / page.id / "visuals").iterdir()}
        assert {v.id for v in page.visuals} == folders
        assert all(v.page == page.id for v in page.visuals)


def test_expected_csvs_match_raw_data(sales_fixture):
    expected = _recompute(sales_fixture)
    files = {p.stem: _read(p) for p in (sales_fixture / "expected").glob("*.csv")}
    assert files == expected
    amounts = sum(Decimal(r["amount"]) for r in _rows(sales_fixture, "sales.csv"))
    assert Decimal(files["cardTotalSales"][1][0]) == amounts


def test_warehouse_matches_csvs(warehouse, sales_fixture):
    with duckdb.connect(str(warehouse), read_only=True) as con:
        for table, name in TABLES.items():
            header, *rows = _read(sales_fixture / "data" / name)
            cols = [
                c[0]
                for c in con.execute(
                    f"SELECT * FROM analytics.{table} LIMIT 0"
                ).description
            ]
            assert cols == header
            assert con.execute(f"SELECT count(*) FROM analytics.{table}").fetchone()[
                0
            ] == len(rows)
        total = con.execute("SELECT sum(amount) FROM analytics.sales").fetchone()[0]
    assert total == sum(Decimal(r["amount"]) for r in _rows(sales_fixture, "sales.csv"))
