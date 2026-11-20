from types import SimpleNamespace

import pytest

from cortino import config
from cortino.plan.models import Plan
from cortino.planning.source_mapping import map_columns, map_sources
from cortino.spec.models import Column, DashboardSpec, Source, Table, load_spec
from cortino.tools.source_catalog import SourceCatalog
from cortino.tools.warehouse import DuckDBWarehouse


@pytest.fixture
def spec(sales_fixture):
    return load_spec(sales_fixture / "golden_spec.json")


@pytest.fixture
def wh(warehouse):
    w = DuckDBWarehouse(warehouse, read_only=True)
    yield w
    w.close()


def _catalog(wh, explicit):
    mappings = config.load("sources")["mappings"] if explicit else {}
    return SourceCatalog(wh, mappings)


def test_explicit_mapping_wins(spec, wh):
    mappings, questions = map_sources(spec, _catalog(wh, True))
    by = {m.spec_table: m for m in mappings}
    assert by["Sales"].target_table == "analytics.sales"
    assert all(m.confidence == 1.0 and m.method == "explicit" for m in mappings)
    assert len(mappings) == 4 and questions == []


def test_overlap_mapping_scored(spec, wh):
    mappings, _ = map_sources(spec, _catalog(wh, False))
    by = {m.spec_table: m for m in mappings}
    assert by["Sales"].target_table == "analytics.sales"
    assert by["Sales"].method == "overlap"
    assert 0 < by["Sales"].confidence <= 1.0


def test_low_confidence_becomes_question(wh):
    spec = DashboardSpec(
        spec_version="0.1", name="x", source_format="twb",
        sources=[Source(id="s1", kind="other"), Source(id="s2", kind="other")],
        tables=[
            Table(name="Orders", source_id="s1", columns=[
                Column(name="order_id", data_type="int64"),
                Column(name="bogus_a", data_type="string"),
                Column(name="bogus_b", data_type="string"),
            ]),
            Table(name="Nothing", source_id="s2",
                  columns=[Column(name="zzz_qqq", data_type="string")]),
        ],
    )  # fmt: skip
    mappings, questions = map_sources(spec, _catalog(wh, False))
    assert mappings[0].confidence < 0.85 and mappings[0].target_table is not None
    assert mappings[1].target_table is None and mappings[1].confidence == 0.0
    assert len(questions) == 2
    Plan(plan_version="0.1", source_map=mappings, questions=questions)


def test_map_columns(spec, wh):
    cat = _catalog(wh, True)
    mappings, _ = map_sources(spec, cat)
    cols = map_columns(spec, mappings, cat)
    amount = next(c for c in cols if c.spec_column == "amount")
    assert amount.target_table == "analytics.sales"
    assert amount.target_column == "amount"
    assert amount.types_compatible
    assert len(cols) == 15


def _stub_catalog(tables):
    return SimpleNamespace(
        list_sources=lambda: list(tables),
        describe_table=lambda t: SimpleNamespace(columns=[(c, "int") for c in tables[t]]),
        explicit_mapping=lambda source: None,
    )


def test_overlap_picks_best_candidate_then_name_tiebreak():
    spec = DashboardSpec(
        spec_version="0.1", name="x", source_format="twb",
        sources=[Source(id="s", kind="other")],
        tables=[Table(name="Sales", source_id="s", columns=[
            Column(name="a", data_type="int"), Column(name="b", data_type="int")])],
    )  # fmt: skip
    cat = _stub_catalog({
        "m.weak": ["a", "x", "y"], "m.foo": ["a", "b"], "m.sales": ["A", "B"],
    })
    (m,), questions = map_sources(spec, cat)
    assert m.target_table == "m.sales" and m.confidence == 1.0 and questions == []


def test_default_threshold_from_config():
    spec = DashboardSpec(
        spec_version="0.1", name="x", source_format="twb",
        sources=[Source(id="s", kind="other")],
        tables=[Table(name="T", source_id="s", columns=[
            Column(name="a", data_type="int"), Column(name="b", data_type="int")])],
    )  # fmt: skip
    # Jaccard 2/2.2.. choose 0.9 overlap: 9 shared of 10 -> below nothing; use 0.8
    cat = _stub_catalog({"m.t": ["a", "b", "c", "d", "e"]})
    _, questions = map_sources(spec, cat)
    assert len(questions) == 1
    _, questions = map_sources(spec, cat, high_confidence=0.1)
    assert questions == []


def test_type_family_exact_base_type():
    from cortino.planning.source_mapping import _family

    assert _family("DECIMAL(18,2)") == "numeric"
    assert _family("VARCHAR(20)") == "text"
    assert _family("interval") is None
    assert _family("timestamp") == "temporal"
    assert _family("pointer") is None
