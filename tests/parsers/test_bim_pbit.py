import copy
import importlib.util
import json
import shutil
import zipfile
from pathlib import Path

import pytest

from cortino.intake.intake import run_intake
from cortino.orchestrator.run_store import RunStore
from cortino.parsers import parse

SALES = Path(__file__).parents[1] / "fixtures" / "sales"


def _dump(spec):
    return spec.model_dump(mode="json", by_alias=True)


def _golden(source_format: str) -> dict:
    golden = json.loads((SALES / "golden_spec.json").read_text(encoding="utf-8"))
    golden["source_format"] = source_format
    return golden


def _extract_pbit(tmp_path: Path, layout: bytes | None = None, schema=None) -> Path:
    dest = tmp_path / "extracted"
    with zipfile.ZipFile(SALES / "pbit" / "Sales.pbit") as z:
        z.extractall(dest)
    if layout is not None:
        (dest / "Report" / "Layout").write_bytes(layout)
    if schema is not None:
        (dest / "DataModelSchema").write_bytes(schema)
    return dest


def _layout() -> dict:
    return json.loads((SALES / "pbit" / "src" / "Layout.json").read_text("utf-8"))


def _bim_project(tmp_path: Path, report: str = "pbir") -> Path:
    dest = tmp_path / "extracted"
    shutil.copytree(SALES / "bim", dest)
    if report == "pbir":
        shutil.copytree(SALES / "pbip" / "Sales.Report", dest / "Sales.Report")
    else:
        (dest / "Sales.Report").mkdir()
        (dest / "Sales.Report" / "report.json").write_bytes(
            b"\xff\xfe" + json.dumps(_layout()).encode("utf-16-le")
        )
    return dest


def test_bim_matches_golden(tmp_path):
    spec = parse(_bim_project(tmp_path), "pbip_zip", "Sales")
    assert _dump(spec) == _golden("pbip_bim")


def test_bim_with_legacy_report_json(tmp_path):
    spec = parse(_bim_project(tmp_path, "legacy"), "pbip_zip", "Sales")
    assert [p.id for p in spec.pages] == ["overview"]
    assert len(spec.pages[0].visuals) == 6 and spec.source_format == "pbip_bim"


def test_pbit_matches_golden(tmp_path):
    spec = parse(_extract_pbit(tmp_path), "pbit", "Sales")
    actual = _dump(spec)
    # The custom visual's raw text is the legacy container config, not PBIR visual.json.
    [unknown] = actual["unknowns"]
    assert (unknown["where"], unknown["what"]) == (
        "visual:customXyz",
        "custom visual customVisualXYZ",
    )
    assert json.loads(unknown["raw"])["singleVisual"]["visualType"] == "customVisualXYZ"
    golden = _golden("pbit")
    golden["unknowns"][0]["raw"] = unknown["raw"]
    assert actual == golden


def test_pbit_one_folder_down(tmp_path):
    root = tmp_path / "x"
    shutil.copytree(_extract_pbit(tmp_path), root / "inner")
    assert parse(root, "pbit", "Sales").source_format == "pbit"


def _configs(layout: dict) -> list[dict]:
    return [json.loads(c["config"]) for c in layout["sections"][0]["visualContainers"]]


def test_layout_config_double_parsed(tmp_path):
    layout = _layout()
    assert isinstance(layout["sections"][0]["visualContainers"][0]["config"], str)
    spec = parse(_extract_pbit(tmp_path), "pbit", "Sales")
    visuals = {v.id: v for v in spec.pages[0].visuals}
    # Names, types and fields all come from inside the stringified config.
    assert set(visuals) == {c["name"] for c in _configs(layout)}
    card = visuals["cardTotalSales"]
    assert card.type == "card"
    assert [(e.field.name, e.field.kind) for e in card.encodings] == [
        ("Total Sales", "measure")
    ]
    bar = visuals["barSalesByRegion"]
    assert [(e.role, e.field.table, e.field.name) for e in bar.encodings] == [
        ("x", "Customer", "region"),
        ("y", "Sales", "Total Sales"),
    ]


def _with_layout(tmp_path: Path, layout: dict) -> Path:
    return _extract_pbit(tmp_path, layout=json.dumps(layout).encode("utf-8"))


def test_unresolved_alias_becomes_unknown(tmp_path):
    layout = _layout()
    container = layout["sections"][0]["visualContainers"][0]
    config = json.loads(container["config"])
    config["singleVisual"]["prototypeQuery"]["From"] = []
    container["config"] = json.dumps(config)
    spec = parse(_with_layout(tmp_path, layout), "pbit", "Sales")
    card = next(v for v in spec.pages[0].visuals if v.id == "cardTotalSales")
    assert card.encodings == []
    assert ("visual:cardTotalSales", "unrecognized role or field Values") in {
        (u.where, u.what) for u in spec.unknowns
    }


def test_odd_visual_shapes_do_not_crash(tmp_path):
    layout = _layout()
    containers = layout["sections"][0]["visualContainers"]
    bad = json.loads(containers[0]["config"])
    bad["singleVisual"] = "oops"
    containers[0]["config"] = json.dumps(bad)
    odd = json.loads(containers[1]["config"])
    odd["singleVisual"]["prototypeQuery"]["From"] = [{"Entity": "Customer"}, 5]
    odd["singleVisual"]["projections"] = {"Values": "x"}
    containers[1]["config"] = json.dumps(odd)
    spec = parse(_with_layout(tmp_path, layout), "pbit", "Sales")
    assert len(spec.pages[0].visuals) == 6
    assert any(u.what == "unreadable visual config" for u in spec.unknowns)


def test_malformed_inputs_raise_clear_errors(tmp_path):
    path = _extract_pbit(tmp_path, layout=b'{"sections": [5]}')
    with pytest.raises(ValueError, match="report layout"):
        parse(path, "pbit", "Sales")
    path = _extract_pbit(tmp_path / "b", schema=b'{"model": {"tables": [{}]}}')
    with pytest.raises(ValueError, match="DataModelSchema"):
        parse(path, "pbit", "Sales")


def test_pbit_without_layout_records_unknown(tmp_path):
    extracted = _extract_pbit(tmp_path)
    (extracted / "Report" / "Layout").unlink()
    spec = parse(extracted, "pbit", "Sales")
    assert spec.pages == []
    assert [(u.where, u.what) for u in spec.unknowns] == [
        ("report", "no readable report layout")
    ]


def test_report_folder_without_layout_records_unknown(tmp_path):
    project = _bim_project(tmp_path, "legacy")
    (project / "Sales.Report" / "report.json").unlink()
    spec = parse(project, "pbip_zip", "Sales")
    assert spec.pages == []
    assert ("report", "no readable report layout") in {
        (u.where, u.what) for u in spec.unknowns
    }


@pytest.mark.parametrize("bom", [True, False])
@pytest.mark.parametrize("codec", ["utf-16-le", "utf-8"])
def test_utf16_bom_handled(tmp_path, bom, codec):
    bom_bytes = {"utf-16-le": b"\xff\xfe", "utf-8": b"\xef\xbb\xbf"}[codec]
    layout = json.dumps(_layout()).encode(codec)
    schema = (SALES / "pbit" / "src" / "DataModelSchema.json").read_text("utf-8")
    schema_bytes = schema.encode(codec)
    prefix = bom_bytes if bom else b""
    extracted = _extract_pbit(
        tmp_path, layout=prefix + layout, schema=prefix + schema_bytes
    )
    spec = parse(extracted, "pbit", "Sales")
    assert [t.name for t in spec.tables] == ["Sales", "Customer", "Product", "Date"]
    assert len(spec.pages[0].visuals) == 6


def test_unmapped_layout_things_become_unknowns(tmp_path):
    layout = copy.deepcopy(_layout())
    section = layout["sections"][0]
    section["filters"] = json.dumps([{"name": "f1", "type": "Categorical"}])
    section["visualContainers"][0]["config"] = "{not json"
    layout["bookmarks"] = [{"name": "b1"}]
    path = _extract_pbit(tmp_path, layout=json.dumps(layout).encode("utf-8"))
    spec = parse(path, "pbit", "Sales")
    whats = {(u.where, u.what) for u in spec.unknowns}
    assert ("page:overview", "unparsed filters") in whats
    assert ("report", "unparsed bookmarks") in whats
    assert any(w.startswith("visual:") and "config" in what for w, what in whats)
    assert len(spec.pages[0].visuals) == 6  # the broken visual is still present


def test_unmapped_bim_things_become_unknowns(tmp_path):
    project = _bim_project(tmp_path)
    path = project / "Sales.SemanticModel" / "model.bim"
    bim = json.loads(path.read_text("utf-8"))
    model = bim["model"]
    model["tables"][1]["columns"].append(
        {"name": "calc", "type": "calculated", "dataType": "string", "expression": "1"}
    )
    model["tables"][1]["partitions"][0]["source"] = {"type": "calculated"}
    model["perspectives"] = [{"name": "p"}]
    model["relationships"][0]["isActive"] = False
    model["relationships"][1]["fromCardinality"] = "one"
    model["relationships"][1]["toCardinality"] = "many"
    path.write_text(json.dumps(bim), encoding="utf-8")
    spec = parse(project, "pbip_zip", "Sales")
    whats = {u.what for u in spec.unknowns if u.where.startswith("bim:")}
    assert {
        "calculated column",
        "non-M partition",
        "unrecognized perspectives",
        "inactive relationship",
    } <= whats
    swapped = spec.relationships[1]
    assert (swapped.from_table, swapped.to_table) == ("Product", "Sales")


def test_pbit_zip_is_deterministic(tmp_path):
    spec = importlib.util.spec_from_file_location("make_pbit", SALES / "make_pbit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    built = module.build(tmp_path / "again.pbit")

    def members(path: Path) -> dict[str, bytes]:
        with zipfile.ZipFile(path) as z:
            return {n: z.read(n) for n in z.namelist()}

    assert members(built) == members(SALES / "pbit" / "Sales.pbit")


def test_pbix_report_only_needs_input(tmp_path):
    upload = tmp_path / "r.pbix"
    with zipfile.ZipFile(upload, "w") as z:
        z.writestr("DataModel", "x")
        z.writestr("Report/Layout", "{}")
    store = RunStore(tmp_path / "runs")
    try:
        run = store.create(upload.name, {})
        res = run_intake(store, run.id, upload, "", "org/repo", [])
    finally:
        store.close()
    assert not res.ok
    assert ".pbit or PBIP" in res.needs_input
    with pytest.raises(ValueError, match="report-only"):
        parse(tmp_path, "pbix", "r")


@pytest.mark.parametrize("mode", ["Between", "Before", "Relative"])
def test_non_basic_legacy_slicer_mode_is_reported(tmp_path, mode):
    """The legacy layout parser records every slicer as an `in` filter: other modes are reported."""
    layout = _layout()
    container = next(c for c in layout["sections"][0]["visualContainers"] if "slicerRegion" in c["config"])
    config = json.loads(container["config"])
    config["singleVisual"]["objects"] = {
        "data": [{"properties": {"mode": {"expr": {"Literal": {"Value": f"'{mode}'"}}}}}]}
    container["config"] = json.dumps(config)
    spec = parse(_with_layout(tmp_path, layout), "pbit", "Sales")
    assert ("visual:slicerRegion", f"slicer mode {mode} read as an in filter") in {
        (u.where, u.what) for u in spec.unknowns
    }
