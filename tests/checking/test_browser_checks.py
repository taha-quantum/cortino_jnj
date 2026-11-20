"""The visual and functional checks against the real preview app in Chromium."""

import json
import socket
import threading
import time
from contextlib import contextmanager

import pytest
import uvicorn

from cortino.checking.functional import check_functional
from cortino.checking.visual import check_visual
from cortino.llm import FakeLLM
from cortino.plan.models import Plan
from cortino.platform.config_schema import DashboardConfig
from cortino.platform.preview_app import create_preview_app
from cortino.tools.browser import PlaywrightBrowser
from cortino.tools.warehouse import DuckDBWarehouse

pytestmark = pytest.mark.browser

# What a checker writes from the spec alone, with the filter bound.
INDEPENDENT = {
    "barSalesByRegion": ("SELECT c.region, SUM(s.amount) AS total_sales FROM analytics.sales s "
                         "JOIN analytics.customers c USING (customer_id) WHERE c.region = $region GROUP BY 1"),
    "cardTotalSales": ("SELECT SUM(s.amount) AS total_sales FROM analytics.sales s "
                       "JOIN analytics.customers c USING (customer_id) WHERE c.region = $region"),
    "lineSalesByMonth": ("SELECT d.month, SUM(s.amount) AS total_sales FROM analytics.sales s "
                         "JOIN analytics.customers c USING (customer_id) "
                         "JOIN analytics.dates d ON s.order_date = d.date WHERE c.region = $region GROUP BY 1"),
    "tableByCategory": ("SELECT p.category, SUM(s.amount) AS total_sales, COUNT(DISTINCT s.order_id) AS order_count "
                        "FROM analytics.sales s JOIN analytics.customers c USING (customer_id) "
                        "JOIN analytics.products p USING (product_id) WHERE c.region = $region GROUP BY 1"),
}  # fmt: skip


@pytest.fixture(scope="module")
def browser():
    try:
        b = PlaywrightBrowser()
    except Exception as e:  # noqa: BLE001 - any launch failure means no browser here
        pytest.skip(f"Chromium unavailable: {str(e).splitlines()[0]}")
    yield b
    b.close()


@contextmanager
def _serve(app):
    """Serve `app` on a free local port; yields the origin with no trailing slash."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started and time.monotonic() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(10)


@pytest.fixture
def preview(sales_dashboard, plan):
    dashboard, db = sales_dashboard
    config = DashboardConfig.model_validate_json((dashboard / "dashboard.json").read_text(encoding="utf-8"))
    with _serve(create_preview_app(dashboard, db, plan=plan)) as origin:
        yield origin + "/", config, db


def test_boxes_match_spec_layout(browser, preview, spec, tmp_path):
    url, config, _ = preview
    boxes = browser.boxes(url)
    page = config.pages[0]
    for c in page.charts:
        want = (c.x / page.width, c.y / page.height, c.w / page.width, c.h / page.height)
        assert boxes[c.id] == pytest.approx(want, abs=0.01)
    result = check_visual(url, config, spec, browser, tmp_path / "shot.png", None, FakeLLM({}))
    assert result.passed, result.findings
    assert result.evidence["mode"] == "layout only"
    assert (tmp_path / "shot.png").read_bytes()[:4] == b"\x89PNG"


def test_layout_mismatch_fails(browser, preview, spec, tmp_path):
    url, config, _ = preview
    visuals = {v.id: v for v in spec.pages[0].visuals}
    a, b = visuals["barSalesByRegion"], visuals["tableByCategory"]
    a.position, b.position = b.position, a.position
    result = check_visual(url, config, spec, browser, tmp_path / "shot.png", None, FakeLLM({}))
    assert not result.passed
    assert result.findings[0].owner == "ui"


def test_filter_changes_numbers(browser, preview, plan):
    url, config, db = preview

    def expected(chart_id, param, value):
        return db.query(INDEPENDENT[chart_id], {param: value})

    result = check_functional(url, config, plan, db, browser, expected)
    assert result.passed, [f.model_dump() for f in result.findings]
    total = browser.read_chart_data(url, "cardTotalSales")[0]["total_sales"]
    assert total > 28217.85  # the filter is reset to all regions afterwards


def test_filter_wrong_numbers_routed_to_data(browser, preview, plan):
    url, config, db = preview

    def expected(chart_id, param, value):
        return db.query(INDEPENDENT[chart_id], {param: "West" if value == "East" else value})

    result = check_functional(url, config, plan, db, browser, expected)
    assert not result.passed
    assert {f.owner for f in result.findings} == {"data"}
    assert {f.visual_id for f in result.findings} == set(INDEPENDENT)


def test_filter_state_kept_for_url_without_trailing_slash(browser, preview):
    url, config, _ = preview
    url = url.rstrip("/")
    (f,) = config.filters
    browser.set_filter(url, f.id, "East")
    assert browser.read_chart_data(url, "cardTotalSales")[0]["total_sales"] == pytest.approx(28217.85)
    browser.set_filter(url, f.id, "")


TYPED_SQL = ("SELECT SUM(amount) AS total FROM facts WHERE ($region IS NULL OR region = $region) "
             "AND ($price IS NULL OR price = $price) AND ($flag IS NULL OR flag = $flag) "
             "AND ($ts IS NULL OR ts = $ts)")  # fmt: skip


def test_functional_typed_filters_on_card(browser, tmp_path):
    """Decimal, boolean and timestamp filters; a region filter on a card that does not project
    region, whose first dimension values have no facts; and a default on the last filter."""
    db = DuckDBWarehouse(tmp_path / "typed.duckdb", read_only=False)
    db.query("CREATE TABLE regions (region VARCHAR); INSERT INTO regions VALUES ('Aaa'), ('Abb'), ('East'), ('West');"
             "CREATE TABLE facts (region VARCHAR, price DECIMAL(10, 2), flag BOOLEAN, ts TIMESTAMP, amount INTEGER);"
             "INSERT INTO facts VALUES ('East', 10.50, true, '2024-01-01', 1), ('West', 20.00, false, '2024-02-01', 2),"
             "('West', 20.00, false, '2024-02-01', 4)")  # fmt: skip
    names = ["region", "price", "flag", "ts"]
    dashboard = tmp_path / "dash"
    (dashboard / "datasets").mkdir(parents=True)
    (dashboard / "datasets" / "total.sql").write_text(TYPED_SQL, encoding="utf-8")
    config = DashboardConfig.model_validate({
        "title": "Typed",
        "pages": [{"name": "P", "width": 600, "height": 300, "charts": [
            {"id": "card", "component": "card", "dataset": "total", "x": 0, "y": 0, "w": 300, "h": 200,
             "title": "Total", "encodings": {"value": "total"}}]}],
        "filters": [{"id": n, "label": n, "field": n, "control": "dropdown", "param": n, "applies_to": "all"}
                    for n in names],
        "datasets": [{"id": "total", "file": "datasets/total.sql",
                      "params": [{"name": n, "type": "string"} for n in names]}],
    })  # fmt: skip
    config.filters[-1].default = ["2024-02-01 00:00:00"]
    (dashboard / "dashboard.json").write_text(config.model_dump_json(), encoding="utf-8")
    plan = Plan.model_validate({"plan_version": "0.1", "column_map": [
        {"spec_table": "T", "spec_column": n, "target_table": "regions" if n == "region" else "facts",
         "target_column": n, "types_compatible": True} for n in names]})  # fmt: skip

    def expected(chart_id, param, value):
        return db.query(TYPED_SQL, {n: value if n == param else None for n in names})

    try:
        with _serve(create_preview_app(dashboard, db, plan=plan)) as url:
            result = check_functional(url, config, plan, db, browser, expected)
    finally:
        db.close()
    assert result.passed, json.dumps([f.model_dump() for f in result.findings], default=str)
