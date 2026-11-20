"""Visual and functional checks without a browser: layout math, rubric parsing, value picking,
and the check logic against a fake Browser."""

import pytest

from cortino.checking.functional import check_functional, pick_values
from cortino.checking.visual import check_visual, iou, layout_ious, parse_judgement
from cortino.llm import FakeLLM
from cortino.platform.config_schema import DashboardConfig
from cortino.spec.models import Position
from cortino.tools.browser import BrowserError
from cortino.tools.warehouse import DuckDBWarehouse


class FakeBrowser:
    """Boxes as given; chart data from `data[(chart_id, filter value)]`."""

    def __init__(self, boxes=None, data=None, broken_filters=()):
        self._boxes, self.data, self.broken = boxes or {}, data or {}, set(broken_filters)
        self.value = None
        self.calls = []

    def screenshot(self, url, path):
        path.write_bytes(b"\x89PNG shot")

    def boxes(self, url):
        return self._boxes

    def set_filter(self, url, filter_id, value):
        self.calls.append((filter_id, value))
        if filter_id in self.broken:
            raise BrowserError(f"no control {filter_id}")
        self.value = value

    def read_chart_data(self, url, chart_id):
        return self.data[(chart_id, self.value)]


def _config(charts, filters=()):
    return DashboardConfig.model_validate({
        "title": "T",
        "pages": [{"name": "P", "width": 1000, "height": 500, "charts": [
            {"id": cid, "component": "bar", "dataset": "d", "x": x, "y": y, "w": w, "h": h, "title": cid,
             "encodings": {"x": "region", "y": ["total"]}}
            for cid, (x, y, w, h) in charts.items()]}],
        "filters": list(filters),
        "datasets": [{"id": "d", "file": "datasets/d.sql", "params": [{"name": "region", "type": "string"}]}],
    })  # fmt: skip


def _spec(spec, positions):
    for v in spec.pages[0].visuals:
        v.position = Position(**positions[v.id]) if v.id in positions else None
    return spec


# --- layout ------------------------------------------------------------------------------------


def test_iou_math():
    assert iou((0, 0, 1, 1), (0, 0, 1, 1)) == 1
    assert iou((0, 0, 1, 1), (2, 2, 1, 1)) == 0
    assert iou((0, 0, 2, 1), (1, 0, 2, 1)) == pytest.approx(1 / 3)
    assert iou((0, 0, 0, 0), (0, 0, 0, 0)) == 0


def test_layout_ious_missing_chart_scores_zero():
    out = layout_ious({"a": (0, 0, 0.5, 0.5)}, {"a": (0, 0, 0.5, 0.5), "b": (0.5, 0.5, 0.5, 0.5)})
    assert out == {"a": 1, "b": 0}


def test_visual_layout_only_passes_when_boxes_match(spec, tmp_path):
    config = _config({"barSalesByRegion": (20, 160, 610, 260)})
    spec = _spec(spec, {"barSalesByRegion": {"x": 20, "y": 160, "w": 610, "h": 260}})
    browser = FakeBrowser(boxes={"barSalesByRegion": (0.02, 0.32, 0.61, 0.52)})
    shot = tmp_path / "shot.png"
    result = check_visual("u", config, spec, browser, shot, None, FakeLLM({}))
    assert result.name == "visual" and result.passed, result.findings
    assert result.evidence["mode"] == "layout only"
    assert result.evidence["layout_iou"]["barSalesByRegion"] == pytest.approx(1)
    assert shot.read_bytes() == b"\x89PNG shot"


def test_visual_layout_mismatch_fails(spec, tmp_path):
    config = _config({"barSalesByRegion": (0, 0, 500, 250)})
    spec = _spec(spec, {"barSalesByRegion": {"x": 0, "y": 0, "w": 500, "h": 250}})
    browser = FakeBrowser(boxes={"barSalesByRegion": (0.5, 0.5, 0.5, 0.5)})
    result = check_visual("u", config, spec, browser, tmp_path / "s.png", None, FakeLLM({}))
    assert not result.passed
    (f,) = result.findings
    assert f.owner == "ui" and f.check == "visual" and "layout" in f.message
    assert not f.fixable  # layout is computed in code: no builder can act on it


# --- rubric ------------------------------------------------------------------------------------


def test_parse_judgement_fenced_and_prose():
    assert parse_judgement('{"score": 0.9, "notes": "ok"}') == (0.9, "ok")
    assert parse_judgement('```json\n{"score": 1, "notes": "same"}\n```') == (1.0, "same")
    assert parse_judgement('Here you go: {"score": 0.4, "notes": "titles differ"} thanks') == (0.4, "titles differ")


@pytest.mark.parametrize("raw", ["no json", '{"score": "high"}', '{"score": 1.5}', '{"notes": "x"}', "[0.5]",
                                 '{"score": true}'])  # fmt: skip
def test_parse_judgement_invalid_raises_value_error(raw):
    with pytest.raises(ValueError):
        parse_judgement(raw)


@pytest.fixture
def refs(tmp_path):
    d = tmp_path / "refs"
    d.mkdir()
    (d / "overview.png").write_bytes(b"\x89PNG ref")
    (d / "readme.txt").write_text("skip me")
    return d


@pytest.mark.parametrize(("reply", "passed"), [
    ('{"score": 0.9, "notes": "close"}', True),
    ('{"score": 0.5, "notes": "legend missing"}', False),
    ("I cannot score this", False),
])  # fmt: skip
def test_visual_reference_scored_by_cheap_role(spec, tmp_path, refs, reply, passed):
    config = _config({"barSalesByRegion": (0, 0, 500, 250)})
    spec = _spec(spec, {"barSalesByRegion": {"x": 0, "y": 0, "w": 500, "h": 250}})
    browser = FakeBrowser(boxes={"barSalesByRegion": (0, 0, 0.5, 0.5)})
    llm = FakeLLM({"cheap": [reply]})
    shot = tmp_path / "shot.png"
    result = check_visual("u", config, spec, browser, shot, refs, llm)
    assert result.passed is passed
    assert result.evidence["mode"] == "layout and reference"
    (call,) = llm.calls
    assert call["images"] == [shot, refs / "overview.png"]
    assert "legends" in call["system"]
    if not passed:
        assert [(f.owner, f.fixable) for f in result.findings] == [("ui", False)]


# --- functional --------------------------------------------------------------------------------


def test_pick_values_queries_mapped_column(warehouse, plan):
    db = DuckDBWarehouse(warehouse, read_only=True)
    try:
        assert pick_values(db, plan, "region") == ["East", "North"]
        assert pick_values(db, plan, "no_such_column") == []
    finally:
        db.close()


REGION = {"id": "region", "label": "Region", "field": "region", "control": "dropdown", "param": "region",
          "applies_to": "all"}  # fmt: skip
EXPECTED = {"East": [{"region": "East", "total": 5}], "North": [{"region": "North", "total": 7}]}


def _expected(chart_id, param, value):
    assert param == "region"
    return EXPECTED.get(value, [])


def _run(data, broken=(), expected=_expected, warehouse_path=None, plan=None):
    config = _config({"a": (0, 0, 10, 10)}, [REGION])
    db = DuckDBWarehouse(warehouse_path, read_only=True)
    try:
        browser = FakeBrowser(data=data, broken_filters=broken)
        return check_functional("u", config, plan, db, browser, expected), browser
    finally:
        db.close()


def test_functional_passes_when_data_changes_and_matches(warehouse, plan):
    data = {("a", "East"): [{"region": "East", "total": 5.0}], ("a", "North"): [{"region": "North", "total": 7}]}
    result, browser = _run(data, warehouse_path=warehouse, plan=plan)
    assert result.name == "functional" and result.passed, result.findings
    assert browser.calls == [("region", ""), ("region", "East"), ("region", "North"), ("region", "")]


def test_functional_unchanged_data_is_ui_wiring(warehouse, plan):
    same = [{"region": "East", "total": 5}]
    result, _ = _run({("a", "East"): same, ("a", "North"): same}, warehouse_path=warehouse, plan=plan)
    (f,) = result.findings
    assert f.owner == "ui" and f.visual_id == "a" and "does not change" in f.message
    assert not f.fixable


def test_functional_wrong_numbers_is_data(warehouse, plan):
    data = {("a", "East"): [{"region": "East", "total": 5}], ("a", "North"): [{"region": "North", "total": 9}]}
    result, _ = _run(data, warehouse_path=warehouse, plan=plan)
    (f,) = result.findings
    assert f.owner == "data" and f.visual_id == "a" and f.evidence["value"] == "North"
    assert f.fixable


def test_functional_missing_control_is_ui(warehouse, plan):
    result, _ = _run({}, broken={"region"}, warehouse_path=warehouse, plan=plan)
    (f,) = result.findings
    assert f.owner == "ui" and "no control region" in f.message and not f.fixable


def test_functional_needs_two_values(warehouse, plan):
    config = _config({"a": (0, 0, 10, 10)}, [{**REGION, "field": "no_such_column"}])
    db = DuckDBWarehouse(warehouse, read_only=True)
    try:
        result = check_functional("u", config, plan, db, FakeBrowser(), _expected)
    finally:
        db.close()
    (f,) = result.findings
    assert f.owner == "ui" and "two values" in f.message and not f.fixable


class StateBrowser:
    """Every filter's current value in `state`; chart rows are `rows_fn(chart_id, state)`.
    set_filter raises for (filter id, value) pairs in `fail_on`."""

    def __init__(self, state, rows_fn, fail_on=()):
        self.state, self.rows_fn, self.fail_on = dict(state), rows_fn, set(fail_on)
        self.calls = []

    def set_filter(self, url, filter_id, value):
        self.calls.append((filter_id, value))
        if (filter_id, value) in self.fail_on:
            raise BrowserError(f"no option {value!r}")
        self.state[filter_id] = value

    def read_chart_data(self, url, chart_id):
        return self.rows_fn(chart_id, self.state)


OTHER = {**REGION, "id": "other", "field": "region", "param": "region"}


def _rows_by_state(chart_id, state):
    """A chart's rows: the region total, or a stale-looking row if the other filter is set."""
    if state.get("other"):
        return [{"region": state["region"] or "all", "total": -1}]
    return EXPECTED.get(state.get("region"), [{"region": "all", "total": 12}])


def _check(config, browser, warehouse, plan, expected=_expected):
    db = DuckDBWarehouse(warehouse, read_only=True)
    try:
        return check_functional("u", config, plan, db, browser, expected)
    finally:
        db.close()


def test_functional_resets_filter_after_browser_error(warehouse, plan):
    config = _config({"a": (0, 0, 10, 10)}, [{**OTHER, "applies_to": []}, REGION])
    browser = StateBrowser({"other": "", "region": ""}, _rows_by_state, fail_on={("other", "North")})
    result = _check(config, browser, warehouse, plan)
    (f,) = result.findings
    assert f.owner == "ui" and "other" in f.message
    assert ("other", "") in browser.calls[browser.calls.index(("other", "North")):]


def test_functional_clears_other_filter_defaults(warehouse, plan):
    config = _config({"a": (0, 0, 10, 10)}, [REGION, {**OTHER, "applies_to": [], "default": ["East"]}])
    browser = StateBrowser({"region": "", "other": "East"}, _rows_by_state)
    result = _check(config, browser, warehouse, plan)
    assert result.passed, result.findings


def test_functional_prefers_values_with_rows(warehouse, plan):
    data = {"South": [{"region": "South", "total": 3}], "West": [{"region": "West", "total": 4}]}

    def expected(chart_id, param, value):
        return data.get(value, [{"region": None, "total": None}])

    browser = StateBrowser({"region": ""}, lambda cid, s: data.get(s["region"], [{"region": "all", "total": 7}]))
    result = _check(_config({"a": (0, 0, 10, 10)}, [REGION]), browser, warehouse, plan, expected)
    assert result.passed, result.findings
    assert ("region", "South") in browser.calls and ("region", "East") not in browser.calls
