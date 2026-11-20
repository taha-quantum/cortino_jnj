import zipfile

import pytest

from cortino.cli import main
from cortino.config import load
from cortino.orchestrator.run_store import RunStore
from cortino.orchestrator.states import RunState


def test_cli_help(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for cmd in ["run", "status", "approve", "request-changes", "reject", "resume", "serve"]:
        assert cmd in out


def test_config_load():
    assert load("thresholds")["max_verify_loops"] == 3


def _zip_pbip(sales_fixture, dest):
    root = sales_fixture / "pbip"
    with zipfile.ZipFile(dest, "w") as z:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(root).as_posix())
    return dest


def test_cli_run_on_sales_pbip_parses_then_planner_needs_warehouse(tmp_path, sales_fixture, capsys, monkeypatch):
    # Pin "no warehouse configured", whatever this machine's sources.yaml or environment says.
    from cortino.orchestrator import stages

    real_load = stages.config.load
    monkeypatch.setattr(
        stages.config, "load",
        lambda name: {**real_load(name), "warehouse": {"kind": "duckdb", "path": ""}} if name == "sources" else real_load(name),
    )
    monkeypatch.delenv("CORTINO_WAREHOUSE_PATH", raising=False)
    upload = _zip_pbip(sales_fixture, tmp_path / "sales.zip")
    runs = tmp_path / "runs"
    base = ["--runs-dir", str(runs)]
    code = main([*base, "run", str(upload), "--name", "Sales", "--target-repo", "repo"], trace=False)
    assert code == 1
    assert "FAILED" in capsys.readouterr().out
    store = RunStore(runs)
    (run,) = store.list()
    assert run.state == RunState.FAILED
    assert (store.path(run.id) / "spec.json").is_file()
    events = (store.path(run.id) / "events.jsonl").read_text(encoding="utf-8")
    assert "warehouse.path is not set" in events
    store.close()

    assert main([*base, "status", run.id], trace=False) == 0
    out = capsys.readouterr().out
    assert run.id in out and "FAILED" in out
    assert main([*base, "status"], trace=False) == 0
    assert run.id in capsys.readouterr().out
    assert main([*base, "approve", run.id], trace=False) == 1
    assert "cannot approve" in capsys.readouterr().err


def test_cli_resume_end_state_errors_cleanly(tmp_path, sales_fixture, capsys):
    upload = _zip_pbip(sales_fixture, tmp_path / "sales.zip")
    base = ["--runs-dir", str(tmp_path / "runs")]
    main([*base, "run", str(upload), "--name", "S", "--target-repo", "r"], trace=False)
    capsys.readouterr()
    store = RunStore(tmp_path / "runs")
    rid = store.list()[0].id
    store.close()
    assert main([*base, "resume", rid], trace=False) == 1
    assert "end state" in capsys.readouterr().err


PBI_URL = "https://app.powerbi.com/groups/ws/reports/rep/ReportSection"
TABLEAU_URL = "https://tab.example.com/#/views/Sales/Overview"


@pytest.mark.parametrize(("url", "fetcher"), [(PBI_URL, "fetch_powerbi"), (TABLEAU_URL, "fetch_tableau")])
def test_cli_run_passes_links_to_the_fetchers(tmp_path, monkeypatch, capsys, url, fetcher):
    from cortino.intake import intake

    seen = []

    def fetch(link, *args, **kwargs):
        seen.append(link)
        raise intake.FetchError("offline")

    monkeypatch.setattr(intake, fetcher, fetch)
    monkeypatch.chdir(tmp_path)
    base = ["--runs-dir", str(tmp_path / "runs")]
    assert main([*base, "run", url, "--name", "Sales", "--target-repo", "r"], trace=False) == 0
    assert "NEEDS_INPUT" in capsys.readouterr().out
    assert seen == [url]
    store = RunStore(tmp_path / "runs")
    assert store.list()[0].meta["upload"] == url  # kept as the link, not made a path
    store.close()


def test_cli_run_stores_a_file_as_an_absolute_path(tmp_path, sales_fixture, monkeypatch):
    _zip_pbip(sales_fixture, tmp_path / "sales.zip")
    monkeypatch.chdir(tmp_path)
    main(["--runs-dir", "runs", "run", "sales.zip", "--name", "S", "--target-repo", "r"], trace=False)
    store = RunStore(tmp_path / "runs")
    (run,) = store.list()
    assert run.meta["upload"] == str((tmp_path / "sales.zip").resolve())
    assert (store.path(run.id) / "spec.json").is_file()
    store.close()


def _fake_uvicorn(monkeypatch):
    import uvicorn

    calls = []
    monkeypatch.setattr(uvicorn, "run", lambda app, host, port, **kw: calls.append((host, port)))
    return calls


def test_serve_binds_loopback_by_default(tmp_path, monkeypatch, capsys):
    _shared_deps(monkeypatch)
    calls = _fake_uvicorn(monkeypatch)
    assert main(["serve", "--runs-dir", str(tmp_path / "runs")], trace=False) == 0
    assert calls == [("127.0.0.1", 8000)]
    assert (tmp_path / "runs" / "cortino.db").exists()
    assert "warning" not in capsys.readouterr().err.lower()


def test_serve_warns_on_non_loopback_host(tmp_path, monkeypatch, capsys):
    _shared_deps(monkeypatch)
    calls = _fake_uvicorn(monkeypatch)
    argv = ["serve", "--host", "0.0.0.0", "--port", "9001", "--runs-dir", str(tmp_path / "runs")]
    assert main(argv, trace=False) == 0
    assert calls == [("0.0.0.0", 9001)]
    assert "warning" in capsys.readouterr().err.lower()


class _Warehouse:
    def __init__(self):
        self.closed = 0

    def close(self):
        self.closed += 1


def _shared_deps(monkeypatch):
    from types import SimpleNamespace

    from cortino import cli
    from cortino.llm import FakeLLM
    from cortino.orchestrator.stages import StageDeps

    deps = StageDeps(llm=FakeLLM({}), catalog=SimpleNamespace(warehouse=_Warehouse()), chart_map={},
                     thresholds=load("thresholds"))  # fmt: skip
    monkeypatch.setattr(cli, "real_deps", lambda: deps)
    return deps


def test_serve_builds_deps_once_and_closes_them_at_shutdown(tmp_path, monkeypatch):
    import uvicorn
    from fastapi.testclient import TestClient

    from cortino import cli
    from cortino.orchestrator.runner import StageResult

    S = RunState
    deps = _shared_deps(monkeypatch)
    given = []

    def stages(d):
        given.append(d)
        nxt = {S.INTAKE: S.PARSED, S.PARSED: S.PLANNED, S.PLANNED: S.AWAITING_PLAN_APPROVAL,
               S.BUILDING: S.INTEGRATING, S.INTEGRATING: S.VERIFYING, S.VERIFYING: S.AWAITING_PREVIEW_APPROVAL}  # fmt: skip
        return {s: lambda ctx, n=n: StageResult(n) for s, n in nxt.items()}

    monkeypatch.setattr(cli, "default_stages", stages)
    from fastapi import FastAPI

    from cortino.api import server

    previewed = []
    monkeypatch.setattr(server, "build_preview",
                        lambda run, run_dir, wh, note: (previewed.append(wh), (FastAPI(), lambda: None))[1])  # fmt: skip
    states = []

    def run(app, host, port, **kw):
        with TestClient(app, base_url="http://127.0.0.1:8000", headers={"X-Cortino": "1"}) as c:
            data = {"name": "S", "target_repo": str(tmp_path)}
            rid = c.post("/runs", data=data, files=[("file", ("s.zip", b"PK"))]).json()["id"]
            app.state.workers.join(rid, timeout=10)
            assert c.post(f"/runs/{rid}/approve").status_code == 202
            app.state.workers.join(rid, timeout=10)
            states.append(c.get(f"/runs/{rid}").json()["state"])
            assert c.get(f"/runs/{rid}/preview", follow_redirects=False).status_code == 307
            assert previewed == [deps.catalog.warehouse]
            assert deps.catalog.warehouse.closed == 0
        states.append(deps.catalog.warehouse.closed)

    monkeypatch.setattr(uvicorn, "run", run)
    assert main(["serve", "--runs-dir", str(tmp_path / "runs")], trace=False) == 0
    assert states == ["AWAITING_PREVIEW_APPROVAL", 1]  # the lifespan shutdown closed the warehouse
    assert given == [deps, deps]  # both actions shared one StageDeps


def test_cli_run_copies_expected_csvs_into_the_run(tmp_path, sales_fixture):
    upload = _zip_pbip(sales_fixture, tmp_path / "sales.zip")
    (tmp_path / "truth").mkdir()
    (tmp_path / "truth" / "cardTotalSales.csv").write_text("total_sales\n1\n", encoding="utf-8")
    (tmp_path / "truth" / "readme.txt").write_text("not ground truth", encoding="utf-8")
    base = ["--runs-dir", str(tmp_path / "runs")]
    main([*base, "run", str(upload), "--name", "S", "--target-repo", "r", "--expected", str(tmp_path / "truth")],
         trace=False)  # fmt: skip
    store = RunStore(tmp_path / "runs")
    (run,) = store.list()
    assert sorted(p.name for p in (store.path(run.id) / "expected").iterdir()) == ["cardTotalSales.csv"]
    store.close()


def test_cli_run_expected_must_be_a_directory(tmp_path, capsys):
    argv = ["--runs-dir", str(tmp_path / "runs"), "run", "x.zip", "--name", "S", "--target-repo", "r",
            "--expected", str(tmp_path / "missing")]  # fmt: skip
    assert main(argv, trace=False) == 1
    assert "--expected" in capsys.readouterr().err


def test_cli_reject_removes_worktree_and_branch(tmp_path):
    import subprocess

    from cortino.building.worktree import create_worktree

    repo = tmp_path / "target"
    repo.mkdir()
    for args in (["init", "-b", "main"], ["config", "user.name", "T"], ["config", "user.email", "t@example.com"],
                 ["commit", "--allow-empty", "-m", "init"]):  # fmt: skip
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)
    store = RunStore(tmp_path / "runs")
    rid = store.create("x.zip", {"name": "Sales", "target_repo": str(repo)}).id
    for s in (RunState.INTAKE, RunState.PARSED, RunState.PLANNED, RunState.AWAITING_PLAN_APPROVAL):
        store.transition(rid, s)
    wt = create_worktree(repo, store.path(rid), "sales", rid)
    store.close()
    assert main(["--runs-dir", str(tmp_path / "runs"), "reject", rid], trace=False) == 0
    assert not wt.path.exists()
    branches = subprocess.run(["git", "-C", str(repo), "branch", "--list", "cortino/*"], check=True,
                              capture_output=True, text=True)
    assert branches.stdout.strip() == ""
