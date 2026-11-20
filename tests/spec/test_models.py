import pytest
from pydantic import ValidationError

from cortino.spec.models import (
    Column,
    DashboardSpec,
    Encoding,
    FieldRef,
    Filter,
    Measure,
    Page,
    Position,
    Source,
    Table,
    Unknown,
    Visual,
    load_spec,
    save_spec,
)


def make_spec(**overrides) -> DashboardSpec:
    field = FieldRef(table="Sales", name="Amount", kind="measure", aggregation="sum")
    visual = Visual(
        id="v1",
        page="p1",
        type="bar",
        original_type="clusteredBarChart",
        position=Position(x=0, y=0, w=400, h=300),
        encodings=[Encoding(role="y", field=field)],
        sort=[{"field": field, "direction": "desc"}],
        top_n=10,
        filters=[Filter(level="visual", field=field, operator="gte", values=[1])],
        title="Sales",
    )
    data: dict = dict(  # noqa: C408
        spec_version="0.1",
        name="Demo",
        source_format="pbip_tmdl",
        sources=[Source(id="s1", kind="postgres", schema_="public", table="sales")],
        tables=[
            Table(
                name="Sales",
                source_id="s1",
                columns=[Column(name="Amount", data_type="decimal", source_column="amount")],
            )
        ],
        measures=[Measure(name="Total", table="Sales", expression="SUM(x)", language="dax")],
        pages=[Page(id="p1", name="Overview", visuals=[visual])],
        unknowns=[Unknown(where="p1/v1", what="custom visual", raw="x" * 5000)],
    )
    data.update(overrides)
    return DashboardSpec(**data)


def test_roundtrip(tmp_path):
    spec = make_spec()
    path = tmp_path / "spec.json"
    save_spec(spec, path)
    assert '"schema"' in path.read_text(encoding="utf-8")
    assert load_spec(path) == spec


def test_rejects_wrong_version():
    with pytest.raises(ValidationError):
        make_spec(spec_version="9")


def test_extra_field_forbidden():
    with pytest.raises(ValidationError):
        make_spec(bogus=1)


def test_unknown_raw_truncated():
    assert len(make_spec().unknowns[0].raw) == 2000


def test_json_schema_exports():
    assert DashboardSpec.model_json_schema()["title"] == "DashboardSpec"
