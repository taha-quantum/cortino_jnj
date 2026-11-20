"""Realistic Tableau shapes the Sales fixture does not exercise."""

import zipfile

import pytest

from cortino.parsers import parse

NS = "federated.x"
REL = "<relation connection='sql.1' name='Sales' table='[dbo].[Sales]' type='table'/>"
REGIONS = (
    "<relation connection='sql.1' name='Regions' table='[dbo].[Regions]' type='table'/>"
)
META = "".join(
    f"<metadata-record class='column'><remote-name>{n}</remote-name>"
    f"<local-name>[{n}]</local-name><parent-name>[Sales]</parent-name>"
    f"<local-type>{t}</local-type></metadata-record>"
    for n, t in [
        ("order_id", "integer"),
        ("order_date", "date"),
        ("amount", "real"),
        ("region", "string"),
    ]
)
COLS = (
    "<column datatype='integer' name='[order_id]' role='dimension' type='ordinal'/>"
    "<column datatype='real' name='[amount]' role='measure' type='quantitative'/>"
    "<column caption='Total Sales' datatype='real' name='[Calculation_1]' "
    "role='measure' type='quantitative'>"
    "<calculation class='tableau' formula='SUM([amount])'/></column>"
)
REGION = f"[{NS}].[none:region:nk]"
TOTAL = f"[{NS}].[usr:Calculation_1:qk]"


def sheet(name="S", mark="Bar", rows=REGION, cols=TOTAL, enc="", flt="", panes=None):
    mark_xml = f"<mark class='{mark}'/>" if mark else ""
    pane = f"<pane>{mark_xml}{enc}</pane>"
    return (
        f"<worksheet name='{name}'><table><view>{flt}</view>"
        f"<panes>{panes or pane}</panes><rows>{rows}</rows><cols>{cols}</cols>"
        "</table></worksheet>"
    )


def dash(names=("S",), size="<size maxheight='500' maxwidth='1000'/>"):
    zones = "".join(
        f"<zone h='50000' id='{i}' name='{n}' w='50000' x='0' y='0'/>"
        for i, n in enumerate(names)
    )
    return f"<dashboard name='D'>{size}<zones>{zones}</zones></dashboard>"


def workbook(
    relations=REL, meta=META, cols=COLS, ds_extra="", sheets=None, dashboards=None
):
    sheets = sheet() if sheets is None else sheets
    dashboards = dash() if dashboards is None else dashboards
    ds = (
        f"<datasource name='{NS}' caption='X'><connection class='federated'>"
        "<named-connections><named-connection caption='s' name='sql.1'>"
        "<connection class='sqlserver' dbname='db' server='srv'/></named-connection>"
        f"</named-connections>{relations}<metadata-records>{meta}</metadata-records>"
        f"</connection>{cols}{ds_extra}</datasource>"
    )
    return (
        f"<?xml version='1.0' encoding='utf-8' ?><workbook><datasources>{ds}</datasources>"
        f"<worksheets>{sheets}</worksheets><dashboards>{dashboards}</dashboards></workbook>"
    )


def run(tmp_path, xml):
    (tmp_path / "x.twb").write_text(xml, encoding="utf-8")
    return parse(tmp_path, "twb", "x")


def unknowns(spec, where_prefix="twb:"):
    return [
        (u.where, u.what) for u in spec.unknowns if u.where.startswith(where_prefix)
    ]


def visual(spec):
    return spec.pages[0].visuals[0]


# A. filters
def filt(inner):
    return f"<filter class='categorical' column='{REGION}'>{inner}</filter>"


def test_all_members_filter_is_no_filter(tmp_path):
    inner = "<groupfilter function='level-members' level='[none:region:nk]'/>"
    spec = run(tmp_path, workbook(sheets=sheet(flt=filt(inner))))
    assert visual(spec).filters == [] and unknowns(spec) == []


def test_union_of_members_is_in_filter(tmp_path):
    inner = (
        "<groupfilter function='union'>"
        "<groupfilter function='member' level='[r]' member='&quot;North&quot;'/>"
        "<groupfilter function='member' level='[r]' member='&quot;South&quot;'/>"
        "</groupfilter>"
    )
    spec = run(tmp_path, workbook(sheets=sheet(flt=filt(inner))))
    assert [(f.operator, f.values) for f in visual(spec).filters] == [
        ("in", ["North", "South"])
    ]


@pytest.mark.parametrize(
    "inner",
    [
        (
            "<groupfilter function='except'><groupfilter function='level-members' level='[r]'/>"
            "<groupfilter function='member' level='[r]' member='&quot;West&quot;'/></groupfilter>"
        ),
        (
            "<groupfilter count='3' function='end' end='top' units='records'>"
            "<groupfilter function='level-members' level='[r]'/></groupfilter>"
        ),
        "<groupfilter function='range' level='[r]'/>",
    ],
)
def test_other_filters_become_unknown(tmp_path, inner):
    spec = run(tmp_path, workbook(sheets=sheet(flt=filt(inner))))
    assert visual(spec).filters == []
    assert [w for w, _ in unknowns(spec)] == ["twb:worksheet:S"]


# B. XML guard
@pytest.mark.parametrize("encoding", ["utf-16", "utf-8"])
def test_doctype_rejected_in_any_encoding(tmp_path, encoding):
    xml = (
        f"<?xml version='1.0' encoding='{encoding}'?>"
        "<!DOCTYPE w [<!ENTITY a 'b'>]><workbook>&a;</workbook>"
    )
    (tmp_path / "x.twb").write_bytes(xml.encode(encoding))
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse(tmp_path, "twb", "x")


def test_doctype_rejected_in_twbx_member(tmp_path):
    hostile = (
        "<?xml version='1.0' encoding='utf-16'?>"
        "<!DOCTYPE w [<!ENTITY a 'b'>]><workbook/>"
    ).encode("utf-16")
    with zipfile.ZipFile(tmp_path / "x.twbx", "w") as z:
        z.writestr("a.twb", hostile)
        z.writestr("b.twb", workbook())
    with zipfile.ZipFile(tmp_path / "x.twbx") as z:
        z.extractall(tmp_path / "out")
    with pytest.raises(ValueError, match="DOCTYPE"):
        parse(tmp_path / "out", "twbx", "x")


# C. derivations
def test_ctd_is_distinctcount(tmp_path):
    spec = run(tmp_path, workbook(sheets=sheet(cols=f"[{NS}].[ctd:order_id:qk]")))
    got = [(e.role, e.field.name, e.field.aggregation) for e in visual(spec).encodings]
    assert got == [("x", "region", None), ("y", "order_id", "distinctcount")]


@pytest.mark.parametrize(
    "ref", ["[federated.x].[med:amount:qk]", "[federated.x].[mn:order_date:ok]"]
)
def test_unmapped_derivation_becomes_unknown(tmp_path, ref):
    spec = run(tmp_path, workbook(sheets=sheet(cols=ref)))
    assert [e.field.name for e in visual(spec).encodings] == ["region"]
    assert [w for w, _ in unknowns(spec)] == ["twb:worksheet:S"]


# D. never vanish
def test_datasource_filter_is_unknown(tmp_path):
    inner = "<groupfilter function='member' member='&quot;West&quot;'/>"
    spec = run(tmp_path, workbook(ds_extra=filt(inner)))
    assert ("twb:datasource", "datasource filter") in unknowns(spec)


def test_user_function_calc_flagged(tmp_path):
    calc = (
        "<column caption='Mine' datatype='boolean' name='[Calculation_2]' "
        "role='dimension' type='nominal'>"
        "<calculation class='tableau' formula='[region] = USERNAME()'/></column>"
    )
    spec = run(tmp_path, workbook(cols=COLS + calc))
    assert any(
        w == "twb:datasource" and "row-level security" in what
        for w, what in unknowns(spec)
    )


def test_automatic_mark_is_flagged_with_inference(tmp_path):
    spec = run(tmp_path, workbook(sheets=sheet(mark="Automatic")))
    assert visual(spec).type == "bar"
    assert any("Automatic" in what for _, what in unknowns(spec))


def test_automatic_continuous_date_is_line(tmp_path):
    shelf = f"[{NS}].[none:order_date:qk]"
    spec = run(tmp_path, workbook(sheets=sheet(mark=None, rows=TOTAL, cols=shelf)))
    assert visual(spec).type == "line"


def test_extra_pane_is_unknown(tmp_path):
    panes = "<pane><mark class='Bar'/></pane><pane><mark class='Circle'/></pane>"
    spec = run(tmp_path, workbook(sheets=sheet(panes=panes)))
    assert any("pane" in what for _, what in unknowns(spec))


OBJECT_GRAPH = (
    "<object-graph><objects>"
    "<object caption='Sales' id='Sales_1'><properties context=''>"
    "<relation name='Sales' table='[dbo].[Sales]' type='table'/></properties></object>"
    "<object caption='Regions' id='Regions_2'><properties context=''>"
    "<relation name='Regions' table='[dbo].[Regions]' type='table'/></properties></object>"
    "</objects><relationships><relationship><expression op='='>"
    "<expression op='[region]'/><expression op='[region (Regions)]'/></expression>"
    "<first-end-point object-id='Sales_1' unique-key='false'/>"
    "<second-end-point object-id='Regions_2' unique-key='true'/>"
    "</relationship></relationships></object-graph>"
)


def test_object_model_relationships_without_duplicate_tables(tmp_path):
    false_tag = "_.fcp.ObjectModelEncapsulateLegacy.false...relation"
    true_tag = "_.fcp.ObjectModelEncapsulateLegacy.true...relation"
    rels = (
        f"<{false_tag} type='join' join='inner'><clause type='join'>"
        "<expression op='='><expression op='[Sales].[region]'/>"
        "<expression op='[Regions].[region]'/></expression></clause>"
        f"{REL}{REGIONS}</{false_tag}>"
        f"<{true_tag} type='collection'>{REL}{REGIONS}</{true_tag}>"
    )
    meta = META + (
        "<metadata-record class='column'><remote-name>region</remote-name>"
        "<local-name>[region (Regions)]</local-name><parent-name>[Regions]</parent-name>"
        "<local-type>string</local-type></metadata-record>"
    )
    spec = run(tmp_path, workbook(relations=rels, meta=meta, ds_extra=OBJECT_GRAPH))
    assert [t.name for t in spec.tables] == ["Sales", "Regions"]
    assert len(spec.sources) == 2
    assert [
        (r.from_table, r.from_column, r.to_table, r.to_column)
        for r in spec.relationships
    ] == [("Sales", "region", "Regions", "region")]


def test_compound_join_is_unknown(tmp_path):
    rels = (
        "<relation type='join' join='inner'><clause type='join'><expression op='AND'>"
        "<expression op='='><expression op='[Sales].[a]'/><expression op='[Regions].[a]'/></expression>"
        "<expression op='='><expression op='[Sales].[b]'/><expression op='[Regions].[b]'/></expression>"
        f"</expression></clause>{REL}{REGIONS}</relation>"
    )
    spec = run(tmp_path, workbook(relations=rels))
    assert spec.relationships == []
    assert any(w == "twb:datasource" and "join" in what for w, what in unknowns(spec))


# E. calculated field role
def test_dimension_calc_is_not_a_measure(tmp_path):
    calc = (
        "<column caption='Bucket' datatype='string' name='[Calculation_3]' "
        "role='dimension' type='nominal'>"
        "<calculation class='tableau' formula='LEFT([region],1)'/></column>"
    )
    sheets = sheet(rows=f"[{NS}].[usr:Calculation_3:nk]")
    spec = run(tmp_path, workbook(cols=COLS + calc, sheets=sheets))
    assert [m.name for m in spec.measures] == ["Total Sales"]
    v = visual(spec)
    assert v.type == "bar"
    assert [(e.role, e.field.name, e.field.kind) for e in v.encodings] == [
        ("x", "Bucket", "column"),
        ("y", "Total Sales", "measure"),
    ]
    assert any(
        "calculated dimension" in what for _, what in unknowns(spec, "twb:datasource")
    )


# G. robustness
def test_custom_sql_relation(tmp_path):
    rel = (
        "<relation connection='sql.1' name='Custom SQL Query' type='text'>"
        "SELECT * FROM t</relation>"
    )
    meta = META.replace("[Sales]", "[Custom SQL Query]")
    spec = run(tmp_path, workbook(relations=rel, meta=meta))
    assert spec.sources[0].query == "SELECT * FROM t"
    assert [t.name for t in spec.tables] == ["Custom SQL Query"]


def test_union_relation_is_unknown_not_crash(tmp_path):
    spec = run(tmp_path, workbook(relations=REL + "<relation name='U' type='union'/>"))
    assert any("union" in what for _, what in unknowns(spec, "twb:datasource"))


def test_automatic_size_and_missing_coordinates(tmp_path):
    spec = run(
        tmp_path, workbook(dashboards=dash(size="<size sizing-mode='automatic'/>"))
    )
    assert visual(spec).position is None
    assert any("size" in what for _, what in unknowns(spec, "twb:dashboard"))
    no_xy = (
        "<dashboard name='D'><size maxheight='5' maxwidth='5'/>"
        "<zones><zone id='1' name='S'/></zones></dashboard>"
    )
    assert visual(run(tmp_path, workbook(dashboards=no_xy))).position is None


@pytest.mark.parametrize(
    "xml",
    [
        "<workbook><worksheets/></workbook>",
        "<workbook><datasources><datasource name='Parameters'/></datasources></workbook>",
        (
            "<workbook><datasources><datasource name='a'><connection class='hyper'/>"
            "</datasource></datasources></workbook>"
        ),
        "<workbook><datasources><datasource name='a'/></datasources></workbook>",
        "<workbook><oops",
    ],
)
def test_unusable_workbook_is_clean_value_error(tmp_path, xml):
    (tmp_path / "x.twb").write_text(xml, encoding="utf-8")
    with pytest.raises(ValueError, match=r"x\.twb"):
        parse(tmp_path, "twb", "x")


def test_join_operand_without_table_and_sheet_without_table(tmp_path):
    rels = (
        "<relation type='join' join='inner'><clause type='join'><expression op='='>"
        "<expression op='[a]'/><expression op='[Regions].[a]'/></expression></clause>"
        f"{REL}{REGIONS}</relation>"
    )
    spec = run(tmp_path, workbook(relations=rels, sheets="<worksheet name='S'/>"))
    assert spec.relationships == []
    assert any("join" in what for _, what in unknowns(spec, "twb:datasource"))
    assert any(w == "twb:worksheet:S" for w, _ in unknowns(spec))


# Table-calc addressing lives on the worksheet's column-instance; the spec keeps only the formula,
# read as Table (across). Any other addressing is reported, never silently read as the default.
def deps(table_calc: str) -> str:
    return (
        f"<datasource-dependencies datasource='{NS}'>"
        "<column-instance column='[Calculation_1]' derivation='User' name='[usr:Calculation_1:qk]' "
        f"pivot='key' type='quantitative'>{table_calc}</column-instance></datasource-dependencies>"
    )


@pytest.mark.parametrize(
    "table_calc",
    [
        "<table-calc ordering-type='Rows'/>",
        "<table-calc ordering-type='CellInPane'/>",
        "<table-calc ordering-type='Field'><order field='[none:region:nk]'/></table-calc>",
        "<table-calc ordering-type='Columns'><restart-every field='[none:region:nk]'/></table-calc>",
        "<table-calc ordering-field='[none:region:nk]' ordering-type='Columns'/>",
    ],
)
def test_non_default_table_calc_addressing_is_unknown(tmp_path, table_calc):
    spec = run(tmp_path, workbook(sheets=sheet(flt=deps(table_calc))))
    (found,) = [u for u in spec.unknowns if "table-calc addressing" in u.what]
    assert found.where == "twb:worksheet:S" and "usr:Calculation_1:qk" in found.what
    assert "table-calc" in found.raw


@pytest.mark.parametrize("table_calc", ["", "<table-calc ordering-type='Columns'/>"])
def test_default_table_calc_addressing_is_not_flagged(tmp_path, table_calc):
    spec = run(tmp_path, workbook(sheets=sheet(flt=deps(table_calc))))
    assert unknowns(spec) == []
