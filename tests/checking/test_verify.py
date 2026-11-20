"""The verify stage: re-run policy, owner routing, report, teardown and the build/verify loop."""

import json
import re
import socket
import subprocess
from typing import ClassVar
from urllib.parse import urlparse

import pytest

from cortino import config
from cortino.building.data_builder import build_data
from cortino.building.ui_builder import build_ui
from cortino.building.worktree import create_worktree
from cortino.checking import CheckResult, Finding, functional, verify
from cortino.checking.chart_alignment import check_chart_alignment
from cortino.checking.code_security import check_code_security
from cortino.checking.data_values import NO_GROUND_TRUTH, check_data_values
from cortino.checking.functional import check_functional
from cortino.checking.lineage import check_lineage
from cortino.checking.verify import CHECKS, run_verify
from cortino.llm import FakeLLM, FakeReply
from cortino.orchestrator.run_store import RunStore
from cortino.orchestrator.runner import RunContext, Runner, StageResult
from cortino.orchestrator.stages import StageDeps, default_stages
from cortino.orchestrator.states import RunState as S
from cortino.plan.models import ColumnMapping
from cortino.platform import serving
from cortino.spec.models import save_spec
from cortino.tools.repo_tools import RepoTools
from cortino.tools.source_catalog import SourceCatalog
from cortino.tools.warehouse import DuckDBWarehouse, WarehouseError

BUILT = ["barSalesByRegion", "cardTotalSales", "lineSalesByMonth", "tableByCategory"]
TITLES = {"charts": [{"id": i, "title": i, "number_format": None} for i in BUILT]}
# What a checker writes from the spec alone: unfiltered, and with the filter bound as $region.
_FROM = "FROM analytics.sales s JOIN analytics.customers c ON s.customer_id = c.customer_id"
_QUERIES = {
    "barSalesByRegion": ("SELECT c.region, SUM(s.amount) AS total_sales " + _FROM, " GROUP BY 1"),
    "cardTotalSales": ("SELECT SUM(s.amount) AS total_sales " + _FROM, ""),
    "lineSalesByMonth": ("SELECT d.month, SUM(s.amount) AS total_sales " + _FROM
                         + " JOIN analytics.dates d ON s.order_date = d.date", " GROUP BY 1"),
    "tableByCategory": ("SELECT p.category, SUM(s.amount) AS total_sales, COUNT(DISTINCT s.order_id) AS order_count "
                        + _FROM + " JOIN analytics.products p ON s.product_id = p.product_id", " GROUP BY 1"),
}  # fmt: skip


class CheckerLLM(FakeLLM):
    """FakeLLM that answers the `checker` role per visual: the bound query when asked for a filter."""

    def _complete(self, role, system, prompt, tools, images):
        if role != "checker":
            return super()._complete(role, system, prompt, tools, images)
        self.calls.append({"role": role, "prompt": prompt})
        vid = re.search(r'"id":\s*"(\w+)"', prompt).group(1)
        head, group = _QUERIES[vid]
        where = " WHERE c.region = $region" if "$region" in prompt else ""
        return FakeReply(head + where + group)


class FakeBrowser:
    instances: ClassVar[list["FakeBrowser"]] = []

    def __init__(self):
        self.closed = False
        FakeBrowser.instances.append(self)

    def close(self):
        self.closed = True

    def set_filter(self, url, filter_id, value):
        pass

    def read_chart_data(self, url, chart_id):
        return []


def _fake_checks(monkeypatch, failing: dict[str, str] | None = None) -> list[str]:
    """Replace every checker with one that records its name and fails with the given owner."""
    calls: list[str] = []

    def fake(name):
        def fn(*args, **kwargs):
            calls.append(name)
            owner = (failing or {}).get(name)
            findings = [Finding(check=name, owner=owner, visual_id=None, message=f"{name} broke")] if owner else []
            return CheckResult(name=name, passed=not findings, findings=findings)

        return fn

    for name in CHECKS:
        monkeypatch.setattr(verify, f"check_{name}", fake(name))
    FakeBrowser.instances = []
    monkeypatch.setattr(verify, "PlaywrightBrowser", FakeBrowser)
    return calls


@pytest.fixture
def source(warehouse):
    wh = DuckDBWarehouse(warehouse, read_only=True)
    yield wh
    wh.close()


@pytest.fixture
def catalog(source):
    return SourceCatalog(source, config.load("sources")["mappings"])


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runs")
    yield s
    s.close()


@pytest.fixture
def ctx(store, spec, plan, tmp_path):
    """A run whose worktree holds the built sales dashboard."""
    rid = store.create("sales.zip", {"name": "Sales", "target_repo": str(tmp_path / "unused")}).id
    path = store.path(rid)
    save_spec(spec, path / "spec.json")
    (path / "plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    tools = RepoTools(path / "worktree")
    build_data(plan, spec, tools, FakeLLM({}), None)
    build_ui(plan, spec, tools, FakeLLM({"builder": [TITLES]}), None)
    return RunContext(store, store.get(rid), path, config.load("thresholds"))


def _deps(llm, catalog):
    return StageDeps(llm=llm, catalog=catalog, chart_map=config.load("chart_map")["chart_map"],
                     thresholds=config.load("thresholds"))  # fmt: skip


def _report(ctx) -> dict:
    return json.loads((ctx.path / "verify" / "report.json").read_text(encoding="utf-8"))


STATIC = ["data_values", "lineage", "chart_alignment", "code_security"]


def test_only_failed_browser_checks_rerun_but_code_and_data_always(monkeypatch, ctx, catalog):
    calls = _fake_checks(monkeypatch)
    assert run_verify(ctx, None, catalog, FakeLLM({})).passed
    assert sorted(calls) == sorted(CHECKS)  # the first loop runs everything

    calls.clear()
    failed = {"lineage", "visual"}
    previous = {"loop": 1, "passed": False, "by_owner": {},
                "checks": [{"name": n, "passed": n not in failed, "findings": [], "evidence": {}} for n in CHECKS]}  # fmt: skip
    report = run_verify(ctx, previous, catalog, FakeLLM({}))
    assert calls == [*STATIC, "visual"]
    assert [c.name for c in report.checks] == list(CHECKS)  # functional passed, so it is carried forward
    assert report.passed
    checks = {c["name"]: c for c in _report(ctx)["checks"]}
    assert checks["functional"]["evidence"] == {"carried_from_loop": 1}
    assert "carried_from_loop" not in checks["visual"]["evidence"]

    calls.clear()
    browsers = len(FakeBrowser.instances)
    previous = _report(ctx) | {"loop": 2}
    run_verify(ctx, previous, catalog, FakeLLM({}))
    assert calls == STATIC
    assert len(FakeBrowser.instances) == browsers  # no browser when no browser check runs
    assert all(b.closed for b in FakeBrowser.instances)
    checks = {c["name"]: c for c in _report(ctx)["checks"]}
    assert checks["functional"]["evidence"] == {"carried_from_loop": 1}  # still the loop that ran it
    assert checks["visual"]["evidence"] == {"carried_from_loop": 2}


def test_failures_routed_by_owner(monkeypatch, ctx, catalog):
    _fake_checks(monkeypatch, {"data_values": "data", "visual": "ui", "lineage": "data"})
    stages = default_stages(_deps(FakeLLM({}), catalog))
    result = stages[S.VERIFYING](ctx)
    assert result.next == S.BUILDING
    assert {o: [f.check for f in fs] for o, fs in result.owner_failures.items()} == {
        "data": ["data_values", "lineage"], "ui": ["visual"]}  # fmt: skip
    fix = json.loads((ctx.path / "fix.json").read_text(encoding="utf-8"))
    assert {o: [f["message"] for f in fs] for o, fs in fix.items()} == {
        "data": ["data_values broke", "lineage broke"], "ui": ["visual broke"]}  # fmt: skip
    assert _report(ctx)["by_owner"]["ui"][0]["message"] == "visual broke"

    _fake_checks(monkeypatch)
    assert stages[S.VERIFYING](ctx).next == S.AWAITING_PREVIEW_APPROVAL
    assert not (ctx.path / "fix.json").exists()


@pytest.mark.browser
def test_report_written(ctx, catalog):
    """The real checkers, preview app and Chromium; the LLM stands in only for the checker."""
    llm = CheckerLLM({})
    report = run_verify(ctx, None, catalog, llm)
    data = _report(ctx)
    assert report.passed, data
    assert data["loop"] == 1 and data["passed"] is True and data["by_owner"] == {}
    assert [c["name"] for c in data["checks"]] == list(CHECKS)
    for c in data["checks"]:
        assert set(c) == {"name", "passed", "findings", "evidence"}, c
        assert c["passed"], c
    checks = {c["name"]: c for c in data["checks"]}
    assert {v["ground_truth"] for v in checks["data_values"]["evidence"]["visuals"].values()} == {NO_GROUND_TRUTH}
    assert (ctx.path / "verify" / "screenshots" / "dashboard.png").read_bytes()[:4] == b"\x89PNG"
    bound = [c for c in llm.calls if "$region" in c["prompt"]]
    assert len(bound) == len(BUILT)  # one filtered independent query per chart, reused across values
    assert not (ctx.path / "verify_db").exists()


def test_environment_torn_down_on_exception(monkeypatch, ctx, catalog):
    _fake_checks(monkeypatch)
    seen = []

    def boom(url, *args, **kwargs):
        seen.append(url)
        raise RuntimeError("visual check crashed")

    monkeypatch.setattr(verify, "check_visual", boom)
    with pytest.raises(RuntimeError, match="crashed"):
        run_verify(ctx, None, catalog, FakeLLM({}))
    port = urlparse(seen[0]).port
    with socket.socket() as s:
        assert s.connect_ex(("127.0.0.1", port)) != 0  # the preview app is gone
    assert not (ctx.path / "verify_db").exists()
    assert FakeBrowser.instances[0].closed


def _events(path) -> list[dict]:
    return [json.loads(e) for e in (path / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def test_cleanup_failure_does_not_hide_the_original_error(monkeypatch, ctx, catalog):
    _fake_checks(monkeypatch)

    def boom(*args, **kwargs):
        raise RuntimeError("visual check crashed")

    def locked(path):
        raise PermissionError(f"in use: {path}")

    monkeypatch.setattr(verify, "check_visual", boom)
    monkeypatch.setattr(serving.shutil, "rmtree", locked)
    with pytest.raises(RuntimeError, match="crashed"):
        run_verify(ctx, None, catalog, FakeLLM({}))
    (problem,) = [e for e in _events(ctx.path) if e["kind"] == "verify_teardown"]
    assert "in use" in problem["problem"]


def test_browser_unavailable_is_skipped_not_passed(monkeypatch, ctx, catalog):
    calls = _fake_checks(monkeypatch)

    def no_browser():
        raise RuntimeError("Executable doesn't exist")

    monkeypatch.setattr(verify, "PlaywrightBrowser", no_browser)
    report = run_verify(ctx, None, catalog, FakeLLM({}))
    assert "visual" not in calls and "functional" not in calls
    checks = {c["name"]: c for c in _report(ctx)["checks"]}
    for name in ("visual", "functional"):
        assert checks[name] == {"name": name, "passed": False, "skipped": True, "findings": [],
                                "evidence": {"reason": "browser unavailable",
                                             "error": "RuntimeError: Executable doesn't exist"}}  # fmt: skip
    assert [e["error"] for e in _events(ctx.path) if e["kind"] == "browser_unavailable"] == [
        "RuntimeError: Executable doesn't exist"]  # fmt: skip
    assert report.passed and _report(ctx)["passed"] is True  # skipped is neither passed nor failed

    calls.clear()
    monkeypatch.setattr(verify, "PlaywrightBrowser", FakeBrowser)
    run_verify(ctx, _report(ctx), catalog, FakeLLM({}))
    assert calls == [*STATIC, "visual", "functional"]  # skipped checks run again


@pytest.mark.parametrize("error", [None, KeyError("chart"), ValueError("bad value")])
def test_functional_independent_query_failure_is_a_finding(monkeypatch, ctx, catalog, error):
    _fake_checks(monkeypatch)
    monkeypatch.setattr(verify, "check_functional", check_functional)
    if error is not None:

        def broken(*args, **kwargs):
            raise error

        monkeypatch.setattr(verify, "write_independent_query", broken)
    report = run_verify(ctx, None, catalog, FakeLLM({}))  # None: no scripted checker reply, so LLMError
    (functional,) = [c for c in report.checks if c.name == "functional"]
    assert not functional.passed
    (f,) = functional.findings
    assert f.owner == "data" and "independent query" in f.message and not f.fixable
    assert report.by_owner["data"] == [f]


def test_functional_own_warehouse_error_is_not_an_independent_query_failure(monkeypatch, ctx, catalog):
    _fake_checks(monkeypatch)
    monkeypatch.setattr(verify, "check_functional", check_functional)

    def no_values(*args, **kwargs):
        raise WarehouseError("pick_values broke")

    monkeypatch.setattr(functional, "pick_values", no_values)
    with pytest.raises(WarehouseError, match="pick_values"):
        run_verify(ctx, None, catalog, FakeLLM({}))


def test_functional_filter_column_resolves_to_first_mapping(monkeypatch, ctx, catalog, plan):
    plan.column_map.append(ColumnMapping(spec_table="Other", spec_column="region", target_table="analytics.other",
                                         target_column="region", types_compatible=True))  # fmt: skip
    (ctx.path / "plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    _fake_checks(monkeypatch)
    monkeypatch.setattr(verify, "check_functional", check_functional)
    llm = FakeLLM({})
    run_verify(ctx, None, catalog, llm)
    assert "analytics.customers.region equals the parameter $region" in llm.calls[0]["prompt"]


# --- the build/verify loop through the runner ---


def _git(path, *args):
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path):
    repo = tmp_path / "target"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# target\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-m", "init")
    return repo


def test_verify_failure_fixes_only_the_failing_owner(monkeypatch, store, spec, plan, catalog, repo):
    calls = _fake_checks(monkeypatch)
    data_runs = []

    def data_values(*args, **kwargs):
        data_runs.append(1)
        if len(data_runs) > 1:
            return check_data_values(*args, **kwargs)
        f = Finding(check="data_values", owner="data", visual_id="barSalesByRegion", message="total_sales is off by 3")
        return CheckResult(name="data_values", passed=False, findings=[f])

    monkeypatch.setattr(verify, "check_data_values", data_values)

    rid = store.create("sales.zip", {"name": "Sales", "target_repo": str(repo)}).id
    path = store.path(rid)
    save_spec(spec, path / "spec.json")
    (path / "plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    for s in (S.INTAKE, S.PARSED, S.PLANNED, S.BUILDING):
        store.transition(rid, s)
    wt = create_worktree(repo, path, "sales", rid)
    RepoTools(wt.path).write_file("dashboards/sales/datasets/stale.sql", "SELECT 1")  # left by an older plan

    fixed = {"views_sql": "", "datasets": {d.id: d.sql for d in plan.datasets}}
    fixed["datasets"]["sales_by_region"] = "-- fixed\n" + fixed["datasets"]["sales_by_region"]
    llm = CheckerLLM({"builder": [TITLES, fixed]})
    assert Runner(store, default_stages(_deps(llm, catalog))).advance(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert store.get(rid).verify_loops == 1
    builder = [c for c in llm.calls if c["role"] == "builder"]
    assert len(builder) == 2 and "total_sales is off by 3" in builder[1]["prompt"]  # only the data builder
    branch = f"cortino/sales-{rid[:8]}"
    assert _git(repo, "log", "--format=%s", branch).splitlines()[:2] == ["cortino: build v2", "cortino: build v1"]
    changed = _git(repo, "diff", "--name-only", f"{branch}~1", branch).splitlines()
    assert changed == ["dashboards/sales/datasets/sales_by_region.sql"]  # UI files untouched
    assert "stale.sql" not in _git(repo, "ls-tree", "-r", "--name-only", branch)
    assert len(data_runs) == 2 and calls.count("lineage") == 2  # code checks run every loop
    assert calls.count("visual") == 1  # a passed browser check is carried forward
    states = [e["to"] for e in map(json.loads, (path / "events.jsonl").read_text(encoding="utf-8").splitlines())
              if e["kind"] == "transition"]  # fmt: skip
    assert states[-5:] == ["VERIFYING", "BUILDING", "INTEGRATING", "VERIFYING", "AWAITING_PREVIEW_APPROVAL"]
    for leftover in ("fix.json", "build_db", "verify_db"):
        assert not (path / leftover).exists(), leftover


def test_data_fix_with_string_built_filter_fails_code_security_next_loop(monkeypatch, store, spec, plan, catalog,
                                                                         repo):  # fmt: skip
    _fake_checks(monkeypatch)
    for name, check in (("lineage", check_lineage), ("chart_alignment", check_chart_alignment),
                        ("code_security", check_code_security)):  # fmt: skip
        monkeypatch.setattr(verify, f"check_{name}", check)
    data_runs = []

    def data_values(*args, **kwargs):
        data_runs.append(1)
        if len(data_runs) > 1:
            return check_data_values(*args, **kwargs)
        f = Finding(check="data_values", owner="data", visual_id="barSalesByRegion", message="total_sales is off by 3")
        return CheckResult(name="data_values", passed=False, findings=[f])

    monkeypatch.setattr(verify, "check_data_values", data_values)
    rid = store.create("sales.zip", {"name": "Sales", "target_repo": str(repo)}).id
    path = store.path(rid)
    save_spec(spec, path / "spec.json")
    (path / "plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
    for s in (S.INTAKE, S.PARSED, S.PLANNED, S.BUILDING):
        store.transition(rid, s)
    good = {"views_sql": "", "datasets": {d.id: d.sql for d in plan.datasets}}
    concat = {"views_sql": "", "datasets": dict(good["datasets"])}
    concat["datasets"]["sales_by_region"] = good["datasets"]["sales_by_region"].replace(
        "c.region = $region", "c.region = '' || $region"
    )
    llm = CheckerLLM({"builder": [TITLES, concat, good]})
    assert Runner(store, default_stages(_deps(llm, catalog))).advance(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert store.get(rid).verify_loops == 2
    notes = [e["note"] for e in _events(path)
             if e["kind"] == "transition" and e["from"] == "VERIFYING" and e["to"] == "BUILDING"]  # fmt: skip
    assert len(notes) == 2 and "builds SQL by string handling" in notes[1]


# --- routing findings a builder cannot act on ---


def _verify_with(monkeypatch, findings: list[Finding]):
    """run_verify replaced by one that fails with `findings` under data_values."""

    def fake(ctx, previous, catalog, llm):
        by_owner: dict[str, list[Finding]] = {}
        for f in findings:
            by_owner.setdefault(f.owner, []).append(f)
        check = CheckResult(name="data_values", passed=False, findings=findings)
        return verify.VerifyReport(False, [check], by_owner)

    monkeypatch.setattr("cortino.orchestrator.stages.run_verify", fake)


LAYOUT = Finding(check="visual", owner="ui", visual_id=None, message="layout differs", fixable=False)
OFF = Finding(check="data_values", owner="data", visual_id="cardTotalSales", message="total is off by 3")


def test_only_unfixable_findings_need_input_without_a_loop(monkeypatch, ctx, catalog):
    _verify_with(monkeypatch, [LAYOUT])
    result = default_stages(_deps(FakeLLM({}), catalog))[S.VERIFYING](ctx)
    assert result.next == S.NEEDS_INPUT and "layout differs" in result.note
    assert not (ctx.path / "fix.json").exists()
    (event,) = [e for e in _events(ctx.path) if e["kind"] == "unfixable_findings"]
    assert [f["message"] for f in event["findings"]] == ["layout differs"]


def test_mixed_findings_rebuild_only_the_fixable(monkeypatch, ctx, catalog):
    _verify_with(monkeypatch, [LAYOUT, OFF])
    result = default_stages(_deps(FakeLLM({}), catalog))[S.VERIFYING](ctx)
    assert result.next == S.BUILDING
    assert result.owner_failures == {"data": [OFF]}
    fix = json.loads((ctx.path / "fix.json").read_text(encoding="utf-8"))
    assert {o: [f["message"] for f in fs] for o, fs in fix.items()} == {"data": ["total is off by 3"]}


def test_unfixable_recorded_in_report_by_run_verify(monkeypatch, ctx, catalog):
    _fake_checks(monkeypatch)
    monkeypatch.setattr(verify, "check_visual", lambda *a, **k: CheckResult(name="visual", passed=False,
                                                                            findings=[LAYOUT]))  # fmt: skip
    run_verify(ctx, None, catalog, FakeLLM({}))
    assert [f["message"] for f in _report(ctx)["unfixable"]] == ["layout differs"]


def test_resume_from_verify_cap_rebuilds_with_the_last_findings(monkeypatch, store, ctx, catalog):
    _verify_with(monkeypatch, [OFF])
    seen = []

    def building(c):
        fix = c.path / "fix.json"
        seen.append(json.loads(fix.read_text(encoding="utf-8")) if fix.exists() else None)
        return StageResult(S.INTEGRATING)

    stages = {**default_stages(_deps(FakeLLM({}), catalog)), S.BUILDING: building,
              S.INTEGRATING: lambda c: StageResult(S.VERIFYING)}  # fmt: skip
    rid = ctx.run.id
    for s in (S.INTAKE, S.PARSED, S.PLANNED, S.BUILDING, S.INTEGRATING, S.VERIFYING):
        store.transition(rid, s)
    runner = Runner(store, stages)
    assert runner.advance(rid) == S.NEEDS_INPUT
    assert store.get(rid).verify_loops == 3
    seen.clear()
    monkeypatch.setattr("cortino.orchestrator.stages.run_verify",
                        lambda *a: verify.VerifyReport(True, [], {}))  # fmt: skip
    assert runner.resume(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert seen == [{"data": [OFF.model_dump()]}]
    assert store.get(rid).verify_loops == 0


def test_expected_csvs_in_the_run_are_ground_truth(monkeypatch, ctx, catalog, sales_fixture):
    import shutil

    _fake_checks(monkeypatch)
    monkeypatch.setattr(verify, "check_data_values", check_data_values)
    shutil.copytree(sales_fixture / "expected", ctx.path / "expected")  # as `run --expected` copies them
    run_verify(ctx, None, catalog, CheckerLLM({}))
    visuals = next(c for c in _report(ctx)["checks"] if c["name"] == "data_values")["evidence"]["visuals"]
    assert {v: i["ground_truth"] for v, i in visuals.items()} == {v: f"{v}.csv" for v in visuals}
