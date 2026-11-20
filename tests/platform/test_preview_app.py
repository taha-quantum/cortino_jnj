import json

import pytest
from fastapi.testclient import TestClient

from cortino.platform.preview_app import create_preview_app


@pytest.fixture
def client(sales_dashboard):
    dashboard, db = sales_dashboard
    return TestClient(create_preview_app(dashboard, db, None, None), base_url="http://127.0.0.1")


def test_dataset_endpoint_binds_params(client):
    r = client.get("/datasets/sales_by_region", params={"region": "O'Brien"})
    assert r.status_code == 200 and r.json() == []
    r = client.get("/datasets/sales_by_region", params={"region": "1; DROP TABLE x"})
    assert r.status_code == 200 and r.json() == []
    r = client.get("/datasets/sales_by_region", params={"region": "East"})
    assert r.status_code == 200
    (row,) = r.json()
    assert row["region"] == "East" and row["total_sales"] == pytest.approx(28217.85)
    # No param bound: NULL, so every region.
    assert len(client.get("/datasets/sales_by_region").json()) == 4


def test_dataset_endpoint_rejects_unknown_param(client):
    r = client.get("/datasets/sales_by_region", params={"customer": "x"})
    assert r.status_code == 400


def test_unknown_or_path_like_dataset_is_404(client):
    assert client.get("/datasets/nope").status_code == 404
    assert client.get("/datasets/views").status_code == 404
    assert client.get("/datasets/..%2Fviews").status_code == 404
    assert client.get("/datasets/%2E%2E%2Fdashboard").status_code == 404


def test_index_config_and_script(client, sales_dashboard):
    dashboard, _ = sales_dashboard
    assert '<script src="/render.js">' in client.get("/").text
    assert client.get("/config").json() == json.loads((dashboard / "dashboard.json").read_text(encoding="utf-8"))
    r = client.get("/render.js")
    assert r.status_code == 200 and "javascript" in r.headers["content-type"]


def test_compare_shows_references_screenshots_and_summary(sales_dashboard, tmp_path):
    dashboard, db = sales_dashboard
    refs = tmp_path / "refs"
    (refs / "sub").mkdir(parents=True)
    (refs / "overview.png").write_bytes(b"\x89PNG ref")
    (refs / "notes.txt").write_text("not an image")
    (refs / "sub" / "deep.png").write_bytes(b"\x89PNG deep")
    report = tmp_path / "verify" / "report.json"
    (report.parent / "screenshots").mkdir(parents=True)
    (report.parent / "screenshots" / "live.png").write_bytes(b"\x89PNG live")
    report.write_text(json.dumps({"passed": False, "checks": [
        {"name": "visual", "passed": False, "findings": [{"message": "<b>layout</b>"}]},
        {"name": "functional", "passed": True, "findings": []},
    ]}))  # fmt: skip
    client = TestClient(create_preview_app(dashboard, db, refs, report), base_url="http://127.0.0.1")
    page = client.get("/compare").text
    assert "/compare/reference/overview.png" in page and "/compare/screenshot/live.png" in page
    assert "notes.txt" not in page and "deep.png" not in page
    assert "&lt;b&gt;layout&lt;/b&gt;" in page and "<b>layout" not in page
    assert client.get("/compare/reference/overview.png").content == b"\x89PNG ref"
    assert client.get("/compare/screenshot/live.png").content == b"\x89PNG live"
    assert client.get("/compare/reference/notes.txt").status_code == 404
    assert client.get("/compare/reference/sub%2Fdeep.png").status_code == 404
    assert client.get("/compare/reference/..%2F..%2Fverify%2Freport.json").status_code == 404


def test_compare_without_references_or_report(client):
    page = client.get("/compare").text
    assert "No reference images" in page and "No verify report" in page


def test_filter_options_come_from_mapped_column(sales_dashboard, plan):
    dashboard, db = sales_dashboard
    client = TestClient(create_preview_app(dashboard, db, plan=plan), base_url="http://127.0.0.1")
    (f,) = client.get("/config").json()["filters"]
    assert client.get(f"/filters/{f['id']}/options").json() == ["East", "North", "South", "West"]
    assert client.get("/filters/nope/options").status_code == 404


def test_filter_options_without_plan_bind_declared_params(client, sales_dashboard):
    # sales_by_region declares $region; with no plan the options come from its unfiltered rows.
    dashboard, _ = sales_dashboard
    config = client.get("/config").json()
    (f,) = config["filters"]
    r = client.get(f"/filters/{f['id']}/options")
    assert r.status_code == 200 and r.json() == ["East", "North", "South", "West"]
    # A target dataset that fails does not take the other datasets' options with it.
    (total,) = [d for d in config["datasets"] if d["id"] == "total_sales"]
    (dashboard / total["file"]).write_text("SELECT * FROM no_such_table", encoding="utf-8")
    r = client.get(f"/filters/{f['id']}/options")
    assert r.status_code == 200 and r.json() == ["East", "North", "South", "West"]


def test_foreign_host_rejected(client):
    """DNS rebinding: a page on another name must not read warehouse rows through the preview."""
    r = client.get("/datasets/sales_by_region", headers={"Host": "evil.example:8001"})
    assert r.status_code == 400
    for host in ("127.0.0.1:8001", "localhost:8001", "[::1]:8001"):
        assert client.get("/datasets/sales_by_region", headers={"Host": host}).status_code == 200
