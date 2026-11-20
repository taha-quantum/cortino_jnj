"""Anything the parser does not interpret must be recorded, never dropped."""

import json
import shutil

from cortino.parsers import parse

MODEL = "Sales.SemanticModel/definition/"
REPORT = "Sales.Report/definition/"
FILTERS = {"filterConfig": {"filters": [{"name": "f1", "type": "Categorical"}]}}


def _copy(sales_fixture, tmp_path):
    dest = tmp_path / "extracted"
    shutil.copytree(sales_fixture / "pbip", dest)
    return dest


def _edit(root, rel, fn):
    path = root / rel
    path.write_text(fn(path.read_text(encoding="utf-8")), encoding="utf-8")


def _add_json(root, rel, extra):
    def fn(text):
        data = json.loads(text)
        data.update(extra)
        return json.dumps(data, indent=2)

    _edit(root, rel, fn)


def _whats(spec):
    return {(u.where, u.what) for u in spec.unknowns}


def test_filter_config_recorded_at_every_level(sales_fixture, tmp_path):
    root = _copy(sales_fixture, tmp_path)
    _add_json(root, REPORT + "report.json", FILTERS)
    _add_json(root, REPORT + "pages/overview/page.json", FILTERS)
    card = REPORT + "pages/overview/visuals/cardTotalSales/visual.json"
    _add_json(root, card, FILTERS)
    got = _whats(parse(root, "pbip_zip", "Sales"))
    for where in ("report", "page:overview", "visual:cardTotalSales"):
        assert (where, "unparsed filterConfig") in got


def test_report_extras_recorded(sales_fixture, tmp_path):
    root = _copy(sales_fixture, tmp_path)
    (root / REPORT / "reportExtensions.json").write_text("{}", encoding="utf-8")
    (root / REPORT / "bookmarks").mkdir()
    (root / REPORT / "bookmarks/b1.bookmark.json").write_text("{}", encoding="utf-8")
    _add_json(root, REPORT + "pages/overview/page.json", {"type": "Drillthrough"})

    def add_sort(text):
        data = json.loads(text)
        data["visual"]["query"]["sortDefinition"] = {"sort": [{"direction": "Asc"}]}
        return json.dumps(data)

    _edit(
        root, REPORT + "pages/overview/visuals/barSalesByRegion/visual.json", add_sort
    )
    got = _whats(parse(root, "pbip_zip", "Sales"))
    assert ("report", "unparsed reportExtensions") in got
    assert ("report", "unparsed bookmark b1.bookmark") in got
    assert ("page:overview", "unparsed page type") in got
    assert ("visual:barSalesByRegion", "unparsed sortDefinition") in got


def test_pages_without_page_order_or_pages_json(sales_fixture, tmp_path):
    root = _copy(sales_fixture, tmp_path)
    _add_json(root, REPORT + "pages/pages.json", {"pageOrder": ["ghost"]})
    spec = parse(root, "pbip_zip", "Sales")
    assert [p.id for p in spec.pages] == ["overview"]
    assert ("report", "pageOrder entry without folder: ghost") in _whats(spec)
    (root / REPORT / "pages/pages.json").unlink()
    spec = parse(root, "pbip_zip", "Sales")
    assert [p.id for p in spec.pages] == ["overview"]
    assert ("report", "missing pages.json; page order lost") in _whats(spec)


def test_role_without_table_permission_recorded(sales_fixture, tmp_path):
    root = _copy(sales_fixture, tmp_path)
    (root / MODEL / "roles/Empty.tmdl").write_text(
        "role Empty\n\tmodelPermission: read\n", encoding="utf-8"
    )
    _edit(
        root,
        MODEL + "roles/RegionManager.tmdl",
        lambda t: t.replace(
            'Customer[region] = "West"\n',
            'Customer[region] = "West"\n\t\tcolumnPermission name = none\n',
        ),
    )
    spec = parse(root, "pbip_zip", "Sales")
    got = _whats(spec)
    assert ("tmdl:Empty.tmdl", "role Empty has no tablePermission") in got
    assert ("tmdl:RegionManager.tmdl", "column-level security") in got
    assert [r.name for r in spec.rls_roles] == ["RegionManager"]


def test_semantic_tmdl_constructs_recorded(sales_fixture, tmp_path):
    root = _copy(sales_fixture, tmp_path)
    (root / MODEL / "expressions.tmdl").write_text(
        'expression Param = "x" meta [IsParameterQuery=true]\n', encoding="utf-8"
    )
    _edit(
        root,
        MODEL + "tables/Sales.tmdl",
        lambda t: t.replace(
            "\tcolumn order_id\n",
            "\tcolumn Margin = [amount] * 2\n\t\tdataType: double\n\n"
            "\thierarchy H\n\t\tlevel L\n\n\tcolumn order_id\n",
        ),
    )
    _edit(
        root,
        MODEL + "tables/Date.tmdl",
        lambda t: t.replace("partition Date = m", "partition Date = calculated"),
    )
    _edit(
        root,
        MODEL + "relationships.tmdl",
        lambda t: (
            t.replace(
                "\tfromColumn: Sales.product_id",
                "\tisActive: false\n\tfromColumn: Sales.product_id",
            )
            + "\nrelationship broken\n\ttoColumn: Date.date\n"
        ),
    )
    spec = parse(root, "pbip_zip", "Sales")
    got = _whats(spec)
    assert ("tmdl:expressions.tmdl", "unrecognized expression") in got
    assert ("tmdl:Sales.tmdl", "calculated column") in got
    assert ("tmdl:Sales.tmdl", "unrecognized hierarchy") in got
    assert ("tmdl:Date.tmdl", "non-M partition") in got
    assert ("tmdl:relationships.tmdl", "inactive relationship") in got
    assert ("tmdl:relationships.tmdl", "relationship without usable columns") in got
    assert "Margin" in [c.name for c in spec.tables[0].columns]
    assert len(spec.relationships) == 3
    assert next(t for t in spec.tables if t.name == "Date").source_id is None


def _date_source(sales_fixture, tmp_path, m_lines):
    root = _copy(sales_fixture, tmp_path)

    def fn(text):
        start = text.index("\t\t\t\tlet")
        end = text.index("\tannotation PBI_ResultType")
        return text[:start] + m_lines + "\n" + text[end:]

    _edit(root, MODEL + "tables/Date.tmdl", fn)
    sources = parse(root, "pbip_zip", "Sales").sources
    return next(s for s in sources if s.id == "Date")


def test_sql_without_navigation_or_with_extra_steps_keeps_raw_m(
    sales_fixture, tmp_path
):
    no_nav = (
        '\t\t\t\tlet\n\t\t\t\t    Source = Sql.Database("s1", "d1")\n'
        "\t\t\t\tin\n\t\t\t\t    Source\n"
    )
    source = _date_source(sales_fixture, tmp_path / "a", no_nav)
    assert (source.kind, source.server, source.database) == ("other", "s1", "d1")
    assert "Sql.Database" in source.query

    extra = (
        '\t\t\t\tlet\n\t\t\t\t    Source = Sql.Database("s1", "d1"),\n'
        '\t\t\t\t    nav = Source{[Schema="dbo",Item="Dates"]}[Data],\n'
        "\t\t\t\t    filtered = Table.SelectRows(nav, each [year] > 2000)\n"
        "\t\t\t\tin\n\t\t\t\t    filtered\n"
    )
    source = _date_source(sales_fixture, tmp_path / "b", extra)
    assert source.kind == "other" and "Table.SelectRows" in source.query


def test_description_comments_are_not_unknown(sales_fixture, tmp_path):
    root = _copy(sales_fixture, tmp_path)
    _edit(
        root,
        MODEL + "tables/Sales.tmdl",
        lambda t: t.replace(
            "\tmeasure 'Total Sales'", "\t/// Sum of amounts\n\tmeasure 'Total Sales'"
        ),
    )
    spec = parse(root, "pbip_zip", "Sales")
    assert [u.what for u in spec.unknowns] == ["custom visual customVisualXYZ"]
    assert len(spec.measures) == 3


def test_quoted_names_and_equals_in_expression(sales_fixture, tmp_path):
    root = _copy(sales_fixture, tmp_path)
    new = (
        "\tmeasure 'O''Brien Sales' = "
        'CALCULATE([Total Sales], Customer[name] = "O\'Brien")\n\n\tcolumn order_id\n'
    )
    _edit(
        root,
        MODEL + "tables/Sales.tmdl",
        lambda t: t.replace("\tcolumn order_id\n", new),
    )
    measures = parse(root, "pbip_zip", "Sales").measures
    measure = next(m for m in measures if m.name == "O'Brien Sales")
    assert measure.expression == 'CALCULATE([Total Sales], Customer[name] = "O\'Brien")'
    assert measure.depends_on == ["Total Sales"]


def test_odd_projection_does_not_crash(sales_fixture, tmp_path):
    root = _copy(sales_fixture, tmp_path)

    def fn(text):
        data = json.loads(text)
        state = data["visual"]["query"]["queryState"]["Values"]
        state["projections"][0]["field"] = {"HierarchyLevel": {}}
        return json.dumps(data)

    _edit(root, REPORT + "pages/overview/visuals/cardTotalSales/visual.json", fn)
    got = _whats(parse(root, "pbip_zip", "Sales"))
    assert ("visual:cardTotalSales", "unrecognized role or field Values") in got
