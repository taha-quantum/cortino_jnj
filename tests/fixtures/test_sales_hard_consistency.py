"""The sales_hard fixture: golden specs validate and the expected CSVs follow from the sales data,
recomputed here in plain Python (csv + Decimal; no DuckDB, no pandas)."""

import csv
import json
from collections import defaultdict
from decimal import Decimal
from pathlib import Path

from cortino.spec.models import load_spec

HARD = Path(__file__).parent / "sales_hard"
DATA = Path(__file__).parent / "sales" / "data"  # same warehouse as the sales fixture


def _rows(name):
    with open(DATA / name, newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def _money(v: Decimal) -> str:
    return f"{v:.2f}"


def recompute() -> dict[str, list[list[str]]]:
    """Expected rows per visual id (header first), from the raw CSVs."""
    sales = _rows("sales.csv")
    customers = {r["customer_id"]: r for r in _rows("customers.csv")}
    amount = [Decimal(r["amount"]) for r in sales]
    by_month: dict[tuple[int, int], Decimal] = defaultdict(Decimal)  # (year, month) -> sales
    by_region: dict[str, Decimal] = defaultdict(Decimal)
    by_date: dict[str, Decimal] = defaultdict(Decimal)
    for r, a in zip(sales, amount):
        year, month = int(r["order_date"][:4]), int(r["order_date"][5:7])
        by_month[year, month] += a
        by_region[customers[r["customer_id"]]["region"]] += a
        by_date[r["order_date"]] += a
    total = sum(amount)

    # Sales YTD = TOTALYTD([Total Sales], 'Date'[date]) at (month, year): running sum within the year.
    ytd, running = [], defaultdict(Decimal)
    for year, month in sorted(by_month):
        running[year] += by_month[year, month]
        ytd.append([month, year, running[year]])
    # Sales LY = CALCULATE([Total Sales], SAMEPERIODLASTYEAR(...)): the same month a year earlier;
    # BLANK (no row on the chart) where the earlier month has no sales.
    ly = [[m, y, by_month[y - 1, m]] for y, m in sorted(by_month) if (y - 1, m) in by_month]
    # {FIXED [region]: SUM([amount])}, shown as AVG per customer (name, region) with sales.
    shown = {(customers[r["customer_id"]]["name"], customers[r["customer_id"]]["region"]) for r in sales}
    cumulative, run = Decimal(0), []
    for day in sorted(by_date):
        cumulative += by_date[day]
        run.append([day, _money(cumulative)])

    return {
        "barShareByRegion": [["region", "% of Total"]]
        + [[g, f"{by_region[g] / total:.4f}"] for g in sorted(by_region)],
        "lineSalesYtd": [["month", "year", "Sales YTD"]]
        + [[str(m), str(y), _money(v)] for m, y, v in sorted(ytd, key=lambda r: (r[0], r[1]))],
        "lineSalesLy": [["month", "year", "Sales LY"]]
        + [[str(m), str(y), _money(v)] for m, y, v in sorted(ly, key=lambda r: (r[0], r[1]))],
        "Region Sales by Customer": [["name", "Region Sales", "region"]]
        + [[n, _money(by_region[g]), g] for n, g in sorted(shown)],
        "Running Sales": [["order_date", "Running Sales"]] + run,
    }


def _read(path):
    with open(path, newline="", encoding="utf-8") as f:
        return list(csv.reader(f))


def test_expected_csvs_match_raw_data():
    files = {p.stem: _read(p) for p in (HARD / "expected").glob("*.csv")}
    assert files == recompute()


def test_customer_names_are_unique():
    """The LOD chart keys on (name, region); a repeated name would merge two customers."""
    names = [r["name"] for r in _rows("customers.csv")]
    assert len(names) == len(set(names))


def test_golden_specs_validate():
    for name in ("golden_spec.json", "golden_spec_twb.json"):
        path = HARD / name
        spec = load_spec(path)
        assert spec.model_dump(mode="json", by_alias=True) == json.loads(path.read_text(encoding="utf-8"))
    pbip, twb = load_spec(HARD / "golden_spec.json"), load_spec(HARD / "golden_spec_twb.json")
    assert [m.name for m in pbip.measures] == ["Total Sales", "Sales YTD", "Sales LY", "% of Total"]
    assert [m.expression for m in twb.measures] == ["{FIXED [region]: SUM([amount])}", "RUNNING_SUM(SUM([amount]))"]
    for spec in (pbip, twb):
        (slicer,) = [f for p in spec.pages for f in p.filters if f.is_slicer]
        assert slicer.operator == "between" and slicer.values == []
        assert spec.unknowns == [] and spec.rls_roles == []
