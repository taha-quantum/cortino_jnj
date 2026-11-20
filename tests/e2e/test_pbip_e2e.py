"""The whole pipeline on the sales PBIP fixture: upload -> plan gate -> build -> verify -> preview
gate -> PR, offline (scripted LLM, DuckDB warehouse, temp git repo, LocalGitHost)."""

import json
import os
from pathlib import Path

import pytest
from pipeline import assert_verified, create_run, zip_dir
from pipeline import events as _events
from pipeline import git as _git
from pipeline import report as _report
from pipeline import runner as _runner
from scripted_llm import VISUALS, sales_llm

from cortino import config
from cortino.checking import verify
from cortino.llm import StrandsLLM
from cortino.orchestrator import stages
from cortino.orchestrator.states import RunState as S

pytestmark = pytest.mark.browser


@pytest.fixture
def upload(tmp_path, sales_fixture) -> Path:
    """The PBIP project zipped as a user would upload it."""
    return zip_dir(sales_fixture / "pbip", tmp_path / "Sales.zip")


def _create(store, upload, repo, sales_fixture) -> str:
    return create_run(store, upload, repo, sales_fixture / "expected", "Sales")


def _assert_verified(report: dict, chromium: bool) -> dict:
    return assert_verified(report, chromium, VISUALS)


def _assert_checker_independent(llm) -> None:
    """The checker saw the original dashboard only: no dataset ids and none of the plan's SQL."""
    prompts = [c["prompt"] for c in llm.calls if c["role"] == "checker"]
    assert prompts
    for p in prompts:
        assert "sales_by_region" not in p and "IS NULL OR" not in p and "AVG(" not in p


def test_pbip_to_pr_open(store, catalog, upload, repo, sales_fixture, chromium):
    llm = sales_llm()
    runner = _runner(store, catalog, llm)
    rid = _create(store, upload, repo, sales_fixture)
    run_dir = store.path(rid)

    assert runner.advance(rid) == S.AWAITING_PLAN_APPROVAL
    assert store.get(rid).meta["source_type"] == "pbip_zip"
    assert json.loads((run_dir / "spec.json").read_text(encoding="utf-8"))["name"] == "Sales"
    assert "RegionManager" in (run_dir / "plan.md").read_text(encoding="utf-8")  # RLS flagged, not dropped

    assert runner.approve(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert store.get(rid).verify_loops == 0
    checks = _assert_verified(_report(store, rid), chromium)
    if chromium:
        assert (run_dir / "verify" / "screenshots" / "dashboard.png").read_bytes()[:4] == b"\x89PNG"
        assert checks["visual"]["evidence"]["mode"] == "layout only"  # no reference images, no judge
    _assert_checker_independent(llm)

    assert runner.approve(rid) == S.PR_OPEN
    assert llm.unused() == {}
    assert not [c for c in llm.calls if c["role"] == "cheap"]
    pr_md = (run_dir / "pr.md").read_text(encoding="utf-8")
    assert pr_md.splitlines()[0] == "# Cortino: rebuild Sales from Power BI"
    branch = f"cortino/sales-{rid[:8]}"
    pr = json.loads((run_dir / "pr.json").read_text(encoding="utf-8"))
    assert pr["title"] == "Cortino: rebuild Sales from Power BI" and pr["branch"] == branch
    tracked = _git(repo, "ls-tree", "-r", "--name-only", branch).splitlines()
    for f in ("spec.json", "plan.md", "verify_report.json"):
        assert f"docs/cortino/sales/{f}" in tracked
    assert "dashboards/sales/dashboard.json" in tracked
    assert {f"dashboards/sales/datasets/{d}.sql" for d in ("sales_by_region", "total_sales")} <= set(tracked)
    assert _git(repo, "log", "--format=%s", branch).splitlines()[:2] == [
        "cortino: evidence for Sales", "cortino: build v1"]  # fmt: skip
    assert _git(repo, "log", "--format=%s", "main").splitlines() == ["init"]  # never merged
    assert _git(repo, "remote") == "" and _git(repo, "for-each-ref", "refs/remotes") == ""  # nothing pushed


STATIC = ("data_values", "lineage", "chart_alignment", "code_security")


def test_wrong_measure_triggers_fix_loop(monkeypatch, store, catalog, upload, repo, sales_fixture, chromium):
    llm = sales_llm(wrong_measure=True)
    runner = _runner(store, catalog, llm)
    rid = _create(store, upload, repo, sales_fixture)
    card_sql = store.path(rid) / "worktree" / "dashboards" / "sales" / "datasets" / "total_sales.sql"

    ran: list[tuple[str, str, bool]] = []  # (check, the card's aggregate on disk when it ran, passed)

    def recorder(name, check):
        def record(*args, **kwargs):
            result = check(*args, **kwargs)
            agg = "AVG" if "AVG(" in card_sql.read_text(encoding="utf-8") else "SUM"
            ran.append((name, agg, result.passed))
            return result

        return record

    for name in STATIC:
        monkeypatch.setattr(verify, f"check_{name}", recorder(name, getattr(verify, f"check_{name}")))
    reports: list[dict] = []  # verify/report.json after each loop, before the next overwrites it

    def keep_report(ctx, *args, _run=stages.run_verify, **kwargs):
        result = _run(ctx, *args, **kwargs)
        reports.append(json.loads((ctx.path / "verify" / "report.json").read_text(encoding="utf-8")))
        return result

    monkeypatch.setattr(stages, "run_verify", keep_report)

    assert runner.advance(rid) == S.AWAITING_PLAN_APPROVAL
    assert runner.approve(rid) == S.AWAITING_PREVIEW_APPROVAL
    assert store.get(rid).verify_loops == 1
    assert llm.unused() == {}

    first, second = reports
    assert first["loop"] == 1 and first["passed"] is False
    failing = {c["name"]: c for c in first["checks"] if not c["passed"] and not c.get("skipped")}
    # the numbers differ, the SQL aggregates with AVG where the spec sums, and with Chromium the
    # filtered card differs too; every finding is about the card and goes to the data builder
    assert set(failing) == {"data_values", "chart_alignment"} | ({"functional"} if chromium else set())
    assert {f["visual_id"] for c in failing.values() for f in c["findings"]} == {"cardTotalSales"}
    assert set(first["by_owner"]) == {"data"}
    (failed,) = [e for e in _events(store, rid)
                 if e["kind"] == "transition" and e["from"] == "VERIFYING" and e["to"] == "BUILDING"]  # fmt: skip
    assert "total_sales" in failed["note"]
    builder = [c for c in llm.calls if c["role"] == "builder"]
    assert len(builder) == 2  # first build: titles only; the fix: the data builder only
    assert '"owner": "data"' in builder[1]["prompt"] and "AVG(s.amount)" in builder[1]["prompt"]

    branch = f"cortino/sales-{rid[:8]}"
    assert _git(repo, "log", "--format=%s", branch).splitlines()[:2] == ["cortino: build v2", "cortino: build v1"]
    changed = _git(repo, "diff", "--name-only", f"{branch}~1", branch).splitlines()
    assert changed == ["dashboards/sales/datasets/total_sales.sql"]  # only the data files changed

    # every data and code check ran once on build v1 (AVG) and again on build v2 (SUM)
    assert sorted(ran) == sorted([(n, "AVG", n not in failing) for n in STATIC]
                                 + [(n, "SUM", True) for n in STATIC])  # fmt: skip
    assert second == _report(store, rid) and second["loop"] == 2
    checks = _assert_verified(second, chromium)
    if chromium:
        assert checks["visual"]["evidence"]["carried_from_loop"] == 1  # passed on loop 1, not re-run
    _assert_checker_independent(llm)


@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("ANTHROPIC_API_KEY"), reason="needs ANTHROPIC_API_KEY")
def test_pbip_live(store, catalog, upload, repo, sales_fixture):
    """Real models: the output is not deterministic, so only a gate or a clear FAILED is asserted."""
    llm = StrandsLLM(config.load("models"), config.load("thresholds"))
    runner = _runner(store, catalog, llm)
    rid = _create(store, upload, repo, sales_fixture)
    state = runner.advance(rid)
    assert state in (S.AWAITING_PLAN_APPROVAL, S.FAILED)
    if state == S.AWAITING_PLAN_APPROVAL:
        state = runner.approve(rid)
        assert state in (S.AWAITING_PREVIEW_APPROVAL, S.NEEDS_INPUT, S.FAILED)
    if state == S.FAILED:
        assert [e for e in _events(store, rid) if e["kind"] == "stage_failed"][-1]["error"]
