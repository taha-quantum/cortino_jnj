import json
import subprocess
from pathlib import Path
from types import SimpleNamespace

import pytest

from cortino.building.worktree import create_worktree
from cortino.checking.data_values import NO_GROUND_TRUTH
from cortino.orchestrator.run_store import RunStore
from cortino.orchestrator.runner import RunContext, StageError
from cortino.orchestrator.states import RunState as S
from cortino.pr.github import GhCliGitHost, LocalGitHost, PrInfo
from cortino.pr.pr_stage import _check_row, open_pr_stage, pr_title

SPEC = {"spec_version": "0.1", "name": "Sales", "source_format": "pbip_tmdl"}
PLAN = {
    "plan_version": "0.1",
    "source_map": [
        {"source_id": "s1", "spec_table": "Sales", "target_table": "analytics.sales",
         "confidence": 0.95, "method": "explicit", "evidence": "x"},
    ],
    "measures": [
        {"name": "Total | Sales", "original": "SUM(Sales[Amt])\nx", "sql_expression": "SUM(amt)",
         "difficulty": "hard", "test_query": "SELECT SUM(amt) FROM analytics.sales", "flagged": True},
    ],
    "unsupported": [{"item": "visual:v9", "reason": "custom visual", "fallback": "table"}],
    "questions": ["Which currency?"],
}  # fmt: skip
REPORT = {
    "loop": 2,
    "passed": True,
    "checks": [
        {"name": "browser", "passed": True, "skipped": True, "findings": [],
         "evidence": {"reason": "browser unavailable"}},
        {"name": "data_values", "passed": True, "findings": [],
         "evidence": {"visuals": {"v1": {"dataset": "d", "ground_truth": NO_GROUND_TRUTH}}}},
        {"name": "code", "passed": False,
         "findings": [{"message": "bad | sql"}], "evidence": {}},
    ],
    "by_owner": {},
}  # fmt: skip
BUILD = {"skipped": [{"item": "visual:v8", "reason": "no component", "fallback": "table"}]}


def _git(path, *args):
    return subprocess.run(
        ["git", "-C", str(path), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def ctx(tmp_git_repo, tmp_path):
    store = RunStore(tmp_path / "runs")
    run = store.create("sales.pbip", {"name": "Sales", "target_repo": str(tmp_git_repo)})
    d = store.path(run.id)
    (d / "spec.json").write_text(json.dumps(SPEC))
    (d / "plan.json").write_text(json.dumps(PLAN))
    (d / "plan.md").write_text("# Plan\n")
    (d / "build_report.json").write_text(json.dumps(BUILD))
    (d / "verify" / "screenshots").mkdir(parents=True)
    (d / "verify" / "report.json").write_text(json.dumps(REPORT))
    (d / "verify" / "screenshots" / "page1.png").write_bytes(b"png")
    (d / "input" / "references").mkdir()
    (d / "input" / "references" / "ref.png").write_bytes(b"ref")
    return RunContext(store, run, d, {})


def test_title_format():
    assert pr_title("Sales", "pbip_tmdl") == "Cortino: rebuild Sales from Power BI"
    for fmt in ("pbip_bim", "pbit", "pbix"):
        assert pr_title("X", fmt).endswith("from Power BI")
    for fmt in ("twb", "twbx"):
        assert pr_title("X", fmt) == "Cortino: rebuild X from Tableau"


def test_body_has_all_sections(ctx):
    open_pr_stage(ctx, LocalGitHost())
    body = (ctx.path / "pr.md").read_text(encoding="utf-8")
    order = ["## Summary", "## Source mapping", "## Measure translations",
             "## Verification results", "## Screenshots", "## Deviations and unsupported",
             "## Questions"]  # fmt: skip
    pos = [body.index(h) for h in order]
    assert pos == sorted(pos)
    assert body.startswith("# Cortino: rebuild Sales from Power BI")
    assert ctx.run.id in body
    assert "skipped: browser unavailable" in body
    assert f"ground_truth: {NO_GROUND_TRUTH}" in body
    assert "analytics.sales" in body and "SUM(amt)" in body
    assert r"Total \| Sales" in body and "SUM(Sales[Amt])<br>x" in body
    assert "SELECT SUM(amt) FROM analytics.sales" in body
    assert "bad \\| sql" in body
    assert "docs/cortino/sales/screenshots/page1.png" in body
    assert "visual:v9" in body and "visual:v8" in body
    assert "Which currency?" in body


def test_check_row_ground_truth_per_visual():
    def row(visuals):
        return _check_row({"name": "data_values", "passed": True, "findings": [],
                           "evidence": {"visuals": visuals}})[2]  # fmt: skip

    assert row({"v1": {"ground_truth": NO_GROUND_TRUTH}}) == f"ground_truth: {NO_GROUND_TRUTH}"
    mixed = row({"v1": {"ground_truth": "v1.csv"}, "v2": {"ground_truth": NO_GROUND_TRUTH}})
    assert mixed == f"ground_truth: v1=v1.csv; v2={NO_GROUND_TRUTH}"
    assert row({"v1": {"dataset": "d"}}) == ""


def test_evidence_committed_under_docs_cortino(ctx):
    result = open_pr_stage(ctx, LocalGitHost())
    assert result.next == S.PR_OPEN
    repo = Path(ctx.store.get(ctx.run.id).meta["target_repo"])
    wt = create_worktree(repo, ctx.path, "sales", ctx.run.id)
    assert _git(wt.path, "status", "--porcelain") == ""
    tracked = _git(wt.path, "ls-files", "docs/cortino/sales").splitlines()
    assert sorted(tracked) == [
        "docs/cortino/sales/plan.md",
        "docs/cortino/sales/screenshots/page1.png",
        "docs/cortino/sales/screenshots/ref.png",
        "docs/cortino/sales/spec.json",
        "docs/cortino/sales/verify_report.json",
    ]
    assert _git(wt.path, "log", "-1", "--format=%s") == "cortino: evidence for Sales"
    pr = json.loads((ctx.path / "pr.json").read_text())
    assert pr["branch"] == f"cortino/sales-{ctx.run.id[:8]}"
    assert pr["title"] == "Cortino: rebuild Sales from Power BI"
    assert pr["number"] == 0 and pr["url"].startswith("file://") and pr["url"].endswith("pr.md")


def test_local_host_never_pushes(ctx, monkeypatch):
    calls = []
    real = subprocess.run

    def spy(cmd, *a, **k):
        calls.append(cmd)
        return real(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", spy)
    open_pr_stage(ctx, LocalGitHost())
    assert calls and not any("push" in c for c in calls)


def test_missing_verify_report_fails(ctx):
    (ctx.path / "verify" / "report.json").unlink()
    with pytest.raises(StageError, match="verification"):
        open_pr_stage(ctx, LocalGitHost())
    assert not (ctx.path / "pr.json").exists()


def test_githost_has_no_merge():
    assert not hasattr(GhCliGitHost, "merge")
    assert not hasattr(LocalGitHost, "merge")


def _fake(responses):
    calls = []

    def run(cmd, **kw):
        calls.append((cmd, kw))
        return responses.pop(0) if responses else SimpleNamespace(returncode=0, stdout="", stderr="")

    return run, calls


def _wt(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "CODEOWNERS").write_text(
        "# c\n* @alice\n/dashboards/ @org/bi-team\n/docs/ @bob\n"
    )
    return SimpleNamespace(path=tmp_path, branch="cortino/sales-abcdef12")


def test_gh_cli_argv_and_parse(tmp_path):
    ok = SimpleNamespace(returncode=0, stdout="https://github.com/o/r/pull/42\n", stderr="")
    run, calls = _fake([SimpleNamespace(returncode=0, stdout="", stderr=""), ok])
    host = GhCliGitHost(run=run)
    wt = _wt(tmp_path)
    host.push(wt)
    info = host.open_pr(wt, "T", "BODY", "main", ["cortino", "needs-review"], host.reviewers(wt))
    assert info == PrInfo("https://github.com/o/r/pull/42", 42)
    assert calls[0][0] == ["git", "push", "-u", "origin", "cortino/sales-abcdef12"]
    assert calls[0][1]["cwd"] == tmp_path
    argv = calls[1][0]
    assert argv[:3] == ["gh", "pr", "create"]
    assert argv[argv.index("--title") + 1] == "T"
    assert argv[argv.index("--base") + 1] == "main"
    assert argv.count("--label") == 2 and "cortino" in argv and "needs-review" in argv
    assert [argv[i + 1] for i, a in enumerate(argv) if a == "--reviewer"] == ["alice", "org/bi-team"]
    assert "--body-file" in argv and "merge" not in argv


def test_gh_cli_failure_raises(tmp_path):
    bad = SimpleNamespace(returncode=1, stdout="", stderr="auth required")
    host = GhCliGitHost(run=_fake([bad])[0])
    with pytest.raises(RuntimeError, match="auth required"):
        host.push(_wt(tmp_path))
