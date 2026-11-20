import json
import shutil
from pathlib import Path

import pytest

from cortino.parsers import parse

PBIP = "pbip"


def _dump(spec):
    return spec.model_dump(mode="json", by_alias=True)


def _copy(sales_fixture, tmp_path) -> Path:
    dest = tmp_path / "extracted"
    shutil.copytree(sales_fixture / PBIP, dest)
    return dest


def test_pbip_matches_golden(sales_fixture):
    spec = parse(sales_fixture / PBIP, "pbip_zip", "Sales")
    golden = json.loads(
        (sales_fixture / "golden_spec.json").read_text(encoding="utf-8")
    )
    assert _dump(spec) == golden


def test_project_one_folder_down(sales_fixture, tmp_path):
    root = tmp_path / "extracted"
    shutil.copytree(sales_fixture / PBIP, root / "Sales")
    assert parse(root, "pbip_zip", "Sales").name == "Sales"


def test_custom_visual_goes_to_unknowns(sales_fixture):
    spec = parse(sales_fixture / PBIP, "pbip_zip", "Sales")
    visual = next(v for v in spec.pages[0].visuals if v.id == "customXyz")
    assert visual.type == "unknown" and visual.encodings == []
    assert [(u.where, u.what) for u in spec.unknowns] == [
        ("visual:customXyz", "custom visual customVisualXYZ")
    ]


def test_rls_role_parsed(sales_fixture):
    spec = parse(sales_fixture / PBIP, "pbip_zip", "Sales")
    role = spec.rls_roles[0]
    assert (role.name, role.table) == ("RegionManager", "Customer")
    assert role.filter_expression == 'Customer[region] = "West"'


def test_multiline_measure(sales_fixture):
    spec = parse(sales_fixture / PBIP, "pbip_zip", "Sales")
    avg = next(m for m in spec.measures if m.name == "Avg Order")
    assert avg.expression == "DIVIDE(\n    [Total Sales],\n    [Order Count]\n)"
    assert avg.depends_on == ["Total Sales", "Order Count"]


def test_unknown_tmdl_keyword_does_not_crash(sales_fixture, tmp_path):
    root = _copy(sales_fixture, tmp_path)
    table = root / "Sales.SemanticModel/definition/tables/Customer.tmdl"
    table.write_text(
        table.read_text(encoding="utf-8") + "\n\tfrobnicate Thing\n\t\tfoo: bar\n",
        encoding="utf-8",
    )
    spec = parse(root, "pbip_zip", "Sales")
    unknown = next(u for u in spec.unknowns if u.where == "tmdl:Customer.tmdl")
    assert unknown.what == "unrecognized frobnicate"
    assert "foo: bar" in unknown.raw
    assert len(spec.tables) == 4


def test_non_sql_source_keeps_raw_m(sales_fixture, tmp_path):
    root = _copy(sales_fixture, tmp_path)
    table = root / "Sales.SemanticModel/definition/tables/Date.tmdl"
    text = table.read_text(encoding="utf-8")
    start = text.index("\t\t\t\tlet")
    end = text.index("\tannotation PBI_ResultType")
    table.write_text(
        text[:start]
        + '\t\t\t\tExcel.Workbook(File.Contents("d.xlsx"))\n\n'
        + text[end:],
        encoding="utf-8",
    )
    source = next(s for s in parse(root, "pbip_zip", "Sales").sources if s.id == "Date")
    assert source.kind == "other"
    assert source.query == 'Excel.Workbook(File.Contents("d.xlsx"))'


def test_hard_pbip_matches_golden(sales_fixture):
    """Time-intelligence and % of total measures and a Between (date-range) slicer."""
    hard = sales_fixture.parent / "sales_hard"
    spec = parse(hard / PBIP, "pbip_zip", "Sales Hard")
    assert _dump(spec) == json.loads((hard / "golden_spec.json").read_text(encoding="utf-8"))
    (slicer,) = spec.pages[0].filters
    assert (slicer.field.name, slicer.operator) == ("date", "between")


def test_basic_slicer_stays_an_in_filter(sales_fixture):
    spec = parse(sales_fixture / PBIP, "pbip_zip", "Sales")
    assert [f.operator for f in spec.pages[0].filters] == ["in"]


def _slicer_with_mode(sales_fixture, tmp_path, mode: str):
    root = _copy(sales_fixture, tmp_path)
    path = root / "Sales.Report/definition/pages/overview/visuals/slicerRegion/visual.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["visual"]["objects"]["data"][0]["properties"]["mode"]["expr"]["Literal"]["Value"] = f"'{mode}'"
    path.write_text(json.dumps(data), encoding="utf-8")
    return parse(root, "pbip_zip", "Sales")


@pytest.mark.parametrize(
    "mode, operator",
    [("Basic", "in"), ("Dropdown", "in"), ("Between", "between"), ("Before", "lte"), ("After", "gte"),
     ("Relative", "relative_date")],
)  # fmt: skip
def test_slicer_mode_sets_the_filter_operator(sales_fixture, tmp_path, mode, operator):
    spec = _slicer_with_mode(sales_fixture, tmp_path, mode)
    assert [f.operator for f in spec.pages[0].filters] == [operator]
    assert not [u for u in spec.unknowns if "slicer mode" in u.what]


def test_unknown_slicer_mode_is_reported(sales_fixture, tmp_path):
    spec = _slicer_with_mode(sales_fixture, tmp_path, "Hierarchy")
    assert ("visual:slicerRegion", "slicer mode Hierarchy read as an in filter") in {
        (u.where, u.what) for u in spec.unknowns
    }
