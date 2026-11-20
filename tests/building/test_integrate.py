import json
import subprocess

import pytest

from cortino import config
from cortino.building.data_builder import build_data
from cortino.building.integrate import integrate
from cortino.building.ui_builder import build_ui
from cortino.llm import FakeLLM, FakeReply
from cortino.orchestrator.run_store import RunStore
from cortino.orchestrator.runner import Runner, StageResult
from cortino.orchestrator.stages import StageDeps, default_stages
from cortino.orchestrator.states import RunState as S
from cortino.spec.models import save_spec
from cortino.tools.repo_tools import RepoTools
from cortino.tools.source_catalog import SourceCatalog
from cortino.tools.warehouse import DuckDBWarehouse, throwaway_copy

BUILT = ["barSalesByRegion", "cardTotalSales", "lineSalesByMonth", "tableByCategory"]
TITLES = {"charts": [{"id": i, "title": i, "number_format": None} for i in BUILT]}
DIR = "dashboards/sales"


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def source(warehouse):
    wh = DuckDBWarehouse(warehouse, read_only=True)
    yield wh
    wh.close()


@pytest.fixture
def built(tmp_path, spec, plan, source):
    """A worktree-like folder holding a first build, and a throwaway copy of the warehouse."""
    tools = RepoTools(tmp_path / "wt")
    build_data(plan, spec, tools, FakeLLM({}), None)
    build_ui(plan, spec, tools, FakeLLM({"builder": [TITLES]}), None)
    db = throwaway_copy(source, tmp_path / "db")
    yield tools, db
    db.close()


def test_integrate_passes_a_clean_build(built, plan):
    tools, db = built
    result = integrate(tools.root, plan, db, DIR)
    assert result.ok and result.failures == {}


def test_integrate_routes_sql_error_to_data_owner(built, plan):
    tools, db = built
    tools.write_file(f"{DIR}/datasets/sales_by_region.sql", "SELECT nope FROM analytics.sales WHERE $region IS NULL")
    result = integrate(tools.root, plan, db, DIR)
    assert not result.ok and list(result.failures) == ["data"]
    (f,) = result.failures["data"]
    assert (f.check, f.owner, f.visual_id) == ("dataset_run", "data", "barSalesByRegion")
    assert "nope" in f.message and f.evidence["dataset"] == "sales_by_region"


def test_integrate_flags_chart_encoding_its_dataset_does_not_return(built, plan):
    tools, db = built
    sql = plan.datasets[0].sql.replace("AS total_sales", "AS sales")
    tools.write_file(f"{DIR}/datasets/sales_by_region.sql", sql)
    result = integrate(tools.root, plan, db, DIR)
    assert list(result.failures) == ["data"]
    (f,) = result.failures["data"]
    assert (f.check, f.visual_id, f.evidence["dataset"]) == ("encoding_column", "barSalesByRegion", "sales_by_region")
    assert "total_sales" in f.message


@pytest.mark.parametrize(
    "rel, text, check",
    [
        ("views.sql", "SELECT 1;", "sql_parse"),
        ("views.sql", "CREATE OR REPLACE VIEW v AS SELECT nope FROM analytics.sales;", "views_apply"),
        ("datasets/total_sales.sql", "SELECT 'abc FROM x", "sql_parse"),
        ("datasets/total_sales.sql", "SELECT 1; SELECT 2", "sql_parse"),
    ],
)  # fmt: skip
def test_integrate_sql_parse_and_view_errors_go_to_data(built, plan, rel, text, check):
    tools, db = built
    tools.write_file(f"{DIR}/{rel}", text)
    result = integrate(tools.root, plan, db, DIR)
    assert list(result.failures) == ["data"]
    assert result.failures["data"][0].check == check


def test_integrate_routes_config_error_to_ui_owner(built, plan):
    tools, db = built
    cfg = json.loads(tools.read_file(f"{DIR}/dashboard.json"))
    cfg["datasets"] = cfg["datasets"][1:]
    cfg["pages"][0]["charts"] = [c for c in cfg["pages"][0]["charts"] if c["dataset"] != "sales_by_region"]
    tools.write_file(f"{DIR}/dashboard.json", json.dumps(cfg))
    result = integrate(tools.root, plan, db, DIR)
    assert list(result.failures) == ["ui"]
    assert "sales_by_region" in result.failures["ui"][0].message
    tools.write_file(f"{DIR}/dashboard.json", "{not json")
    assert integrate(tools.root, plan, db, DIR).failures["ui"][0].check == "config"


# --- the BUILDING and INTEGRATING stages ---


def _run_at_building(store, spec, plan, repo):
    rid = store.create("sales.zip", {"name": "Sales", "target_repo": str(repo)}).id
    save_spec(spec, store.path(rid) / "spec.json")
    (store.path(rid) / "plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    for s in (S.INTAKE, S.PARSED, S.PLANNED, S.BUILDING):
        store.transition(rid, s)
    return rid


def _runner(store, llm, source):
    catalog = SourceCatalog(source, config.load("sources")["mappings"])
    deps = StageDeps(llm=llm, catalog=catalog, chart_map=config.load("chart_map")["chart_map"],
                     thresholds=config.load("thresholds"))  # fmt: skip
    stages = default_stages(deps)
    stages[S.VERIFYING] = lambda ctx: StageResult(S.AWAITING_PREVIEW_APPROVAL)
    return Runner(store, stages)


def _events(store, rid):
    lines = (store.path(rid) / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines]


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runs")
    yield s
    s.close()


def test_commit_created_on_branch(store, tmp_git_repo, spec, plan, source):
    plan.datasets[3].sql = plan.datasets[3].sql.replace("AS order_count", "AS orders")  # leave a UI gap
    rid = _run_at_building(store, spec, plan, tmp_git_repo)
    llm = FakeLLM({"builder": [FakeReply(json.dumps(TITLES), 50, 7)]})
    assert _runner(store, llm, source).advance(rid) == S.AWAITING_PREVIEW_APPROVAL
    branch = f"cortino/sales-{rid[:8]}"
    assert _git(tmp_git_repo, "log", "-1", "--format=%s", branch) == "cortino: build v1"
    files = _git(tmp_git_repo, "show", "--name-only", "--format=", branch).splitlines()
    assert "dashboards/sales/dashboard.json" in files
    assert all(f.startswith("dashboards/sales/") for f in files)
    assert _git(tmp_git_repo, "branch", "--show-current") == "main"
    report = json.loads((store.path(rid) / "build_report.json").read_text(encoding="utf-8"))
    assert report["files"]["ui"] == ["dashboards/sales/dashboard.json"]
    assert "dashboards/sales/views.sql" in report["files"]["data"]
    assert report["results"] == {"ok": True, "failures": {}}
    assert report["tokens"] == {"input": 50, "output": 7}
    skipped = {s["item"]: s["reason"] for s in report["skipped"]}
    assert skipped["visual:customXyz"] == "custom visual customVisualXYZ"
    assert "rls:RegionManager" in skipped
    assert "encoding:tableByCategory.Order Count" in skipped  # UI gaps are listed too
    assert report["commit"] == _git(tmp_git_repo, "rev-parse", branch)


def test_failed_integrate_goes_back_to_owner_once_then_commits(store, tmp_git_repo, spec, plan, source):
    good = plan.datasets[0].sql
    plan.datasets[0].sql = good.replace("c.region,", "c.regionx AS region,")
    rid = _run_at_building(store, spec, plan, tmp_git_repo)
    fixed = {"views_sql": "", "datasets": {d.id: d.sql for d in plan.datasets} | {"sales_by_region": good}}
    llm = FakeLLM({"builder": [TITLES, fixed]})
    assert _runner(store, llm, source).advance(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert len(llm.calls) == 2 and "regionx" in llm.calls[1]["prompt"]  # the UI is not rebuilt
    states = [e["to"] for e in _events(store, rid) if e["kind"] == "transition"]
    assert states[-5:-1] == ["INTEGRATING", "BUILDING", "INTEGRATING", "VERIFYING"]
    assert _git(tmp_git_repo, "log", "-1", "--format=%s", f"cortino/sales-{rid[:8]}") == "cortino: build v1"


def test_integrate_gives_up_after_2_retries(store, tmp_git_repo, spec, plan, source):
    plan.datasets[0].sql = plan.datasets[0].sql.replace("c.region,", "c.regionx AS region,")
    rid = _run_at_building(store, spec, plan, tmp_git_repo)
    broken = {"views_sql": "", "datasets": {d.id: d.sql for d in plan.datasets}}
    llm = FakeLLM({"builder": [TITLES, broken, broken, broken]})
    assert _runner(store, llm, source).advance(rid) == S.FAILED
    assert len(llm.calls) == 3  # titles, then the data builder twice
    failed = [e for e in _events(store, rid) if e["kind"] == "stage_failed"]
    assert failed[0]["stage"] == "INTEGRATING" and "regionx" in failed[0]["error"]
    assert "2 retries" in failed[0]["error"]
