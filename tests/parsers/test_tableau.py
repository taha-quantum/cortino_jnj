import csv
import importlib.util
import json
import shutil
import zipfile
from decimal import Decimal

import pytest

from cortino.parsers import parse
from cortino.parsers.tableau_twb import parse_field_ref


def _projection(spec_dict):
    """Names differ between Power BI and Tableau; compare what the dashboard means."""
    visuals = sorted(
        (
            v["type"],
            tuple(
                (e["field"]["name"], e["field"]["aggregation"]) for e in v["encodings"]
            ),
        )
        for page in spec_dict["pages"]
        for v in page["visuals"]
        if v["type"] != "unknown"
    )
    measures = {m["name"]: m["depends_on"] for m in spec_dict["measures"]}
    sources = sorted(s["table"] for s in spec_dict["sources"])
    return {"visuals": visuals, "measures": measures, "sources": sources}


def _twb_dir(sales_fixture, tmp_path):
    dest = tmp_path / "extracted"
    dest.mkdir()
    shutil.copy2(sales_fixture / "twb" / "Sales.twb", dest)
    return dest


def test_twb_matches_golden_semantics(sales_fixture, tmp_path):
    spec = parse(_twb_dir(sales_fixture, tmp_path), "twb", "Sales")
    golden = json.loads(
        (sales_fixture / "golden_spec.json").read_text(encoding="utf-8")
    )
    assert spec.source_format == "twb"
    expected = _projection(golden)
    # The golden DAX `Avg Order` calls the other measures; the Tableau formula (per the brief)
    # repeats the aggregations, so it has no measure dependencies.
    expected["measures"]["Avg Order"] = []
    assert _projection(spec.model_dump(mode="json", by_alias=True)) == expected
    avg = next(m for m in spec.measures if m.name == "Avg Order")
    assert avg.language == "tableau"
    assert avg.expression == "SUM([amount])/COUNTD([order_id])"


def test_model_and_layout(sales_fixture, tmp_path):
    spec = parse(_twb_dir(sales_fixture, tmp_path), "twb", "Sales")
    assert {(s.server, s.database, s.schema_) for s in spec.sources} == {
        ("server01", "salesdb", "dbo")
    }
    assert [t.name for t in spec.tables] == ["Sales", "Customers", "Products", "Dates"]
    customers = spec.tables[1]
    assert customers.source_id == "Customers"
    assert [c.name for c in customers.columns] == ["customer_id", "name", "region"]
    assert {
        (r.from_table, r.from_column, r.to_table, r.to_column)
        for r in spec.relationships
    } == {
        ("Sales", "customer_id", "Customers", "customer_id"),
        ("Sales", "product_id", "Products", "product_id"),
        ("Sales", "order_date", "Dates", "date"),
    }
    page = spec.pages[0]
    assert page.name == "Overview"
    bar = next(v for v in page.visuals if v.type == "bar")
    # zones are 0-100000 fractions of the 1280x720 dashboard
    assert bar.position.model_dump() == pytest.approx(
        {"x": 20.0, "y": 160.0, "w": 610.0, "h": 260.0}, abs=0.1
    )
    slicer = next(v for v in page.visuals if v.type == "slicer")
    assert [(f.field.name, f.is_slicer) for f in page.filters] == [("region", True)]
    assert slicer.encodings[0].field.name == "region"


def test_unmapped_features_go_to_unknowns(sales_fixture, tmp_path):
    spec = parse(_twb_dir(sales_fixture, tmp_path), "twb", "Sales")
    assert [(u.where, "Filter Region" in u.raw) for u in spec.unknowns] == [
        ("twb:dashboard:Overview", True)
    ]


def test_field_ref_split():
    ref = parse_field_ref("[federated.x].[sum:amount:qk]")
    assert (ref.aggregation, ref.field, ref.type) == ("sum", "amount", "qk")
    ref = parse_field_ref("[federated.x].[none:region:nk]")
    assert (ref.aggregation, ref.field, ref.type) == (None, "region", "nk")
    ref = parse_field_ref("[federated.x].[ctd:order_id:qk]")
    assert ref.aggregation == "distinctcount"
    ref = parse_field_ref("[federated.x].[usr:Calculation_1001:qk]")
    assert (ref.aggregation, ref.field, ref.derivation) == (
        None,
        "Calculation_1001",
        "usr",
    )
    assert parse_field_ref("[federated.x].[:Measure Names]") is None


def test_twbx_unpacks_twb(sales_fixture, tmp_path):
    spec = importlib.util.spec_from_file_location(
        "make_twbx", sales_fixture / "make_twbx.py"
    )
    make_twbx = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(make_twbx)
    twbx = make_twbx.build(tmp_path / "Sales.twbx")
    again = make_twbx.build(tmp_path / "again.twbx")
    assert twbx.read_bytes() == again.read_bytes()
    extracted = tmp_path / "extracted"
    with zipfile.ZipFile(twbx) as z:
        z.extractall(extracted)
    parsed = parse(extracted, "twbx", "Sales")
    assert parsed.source_format == "twbx"
    assert len(parsed.pages[0].visuals) == 5


def test_doctype_is_rejected(tmp_path):
    (tmp_path / "x.twb").write_text(
        '<?xml version="1.0"?><!DOCTYPE workbook [<!ENTITY a "b">]><workbook/>',
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse(tmp_path, "twb", "x")


def test_hyper_read_as_ground_truth(sales_fixture, tmp_path):
    hyperapi = pytest.importorskip("tableauhyperapi")
    from cortino.parsers.hyper_extract import read_extract

    rows = list(
        csv.DictReader((sales_fixture / "data" / "sales.csv").open(encoding="utf-8"))
    )
    extract = tmp_path / "Sales.hyper"
    with (
        hyperapi.HyperProcess(
            hyperapi.Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU,
            parameters={"log_config": ""},
        ) as hyper,
        hyperapi.Connection(
            hyper.endpoint, extract, hyperapi.CreateMode.CREATE_AND_REPLACE
        ) as conn,
    ):
        conn.catalog.create_schema("Extract")
        table = hyperapi.TableDefinition(
            hyperapi.TableName("Extract", "Extract"),
            [
                hyperapi.TableDefinition.Column("order_id", hyperapi.SqlType.int()),
                hyperapi.TableDefinition.Column(
                    "amount", hyperapi.SqlType.numeric(12, 2)
                ),
            ],
        )
        conn.catalog.create_table(table)
        with hyperapi.Inserter(conn, table) as inserter:
            inserter.add_rows(
                [(int(r["order_id"]), Decimal(r["amount"])) for r in rows]
            )
            inserter.execute()
    data = read_extract(extract)
    assert data["Extract"].columns == ["order_id", "amount"]
    assert len(data["Extract"].rows) == len(rows)
    assert sum(r[1] for r in data["Extract"].rows) == sum(
        Decimal(r["amount"]) for r in rows
    )


def test_fixture_bar_has_no_filter_and_no_duplicate_tables(sales_fixture, tmp_path):
    spec = parse(_twb_dir(sales_fixture, tmp_path), "twb", "Sales")
    bar = next(v for v in spec.pages[0].visuals if v.type == "bar")
    assert bar.filters == []
    assert len(spec.tables) == len(spec.sources) == 4


def test_hard_twb_matches_golden(sales_fixture, tmp_path):
    """FIXED LOD and RUNNING_SUM calcs survive as measures; a continuous date filter is a range."""
    hard = sales_fixture.parent / "sales_hard"
    dest = tmp_path / "extracted"
    dest.mkdir()
    shutil.copy2(hard / "twb" / "SalesHard.twb", dest)
    spec = parse(dest, "twb", "Sales Hard")
    golden = json.loads((hard / "golden_spec_twb.json").read_text(encoding="utf-8"))
    assert spec.model_dump(mode="json", by_alias=True) == golden
    (slicer,) = spec.pages[0].filters
    assert (slicer.field.name, slicer.operator) == ("order_date", "between")


def test_discrete_filter_zone_stays_an_in_filter(sales_fixture, tmp_path):
    spec = parse(_twb_dir(sales_fixture, tmp_path), "twb", "Sales")
    assert [f.operator for f in spec.pages[0].filters] == ["in"]
