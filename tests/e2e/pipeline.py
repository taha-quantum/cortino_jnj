"""Helpers for driving the real stages end to end (scripted LLM, DuckDB warehouse, temp git repo)."""

import json
import shutil
import subprocess
import zipfile
from pathlib import Path

from cortino import config
from cortino.checking.verify import BROWSER_CHECKS, BROWSER_UNAVAILABLE, CHECKS
from cortino.orchestrator.runner import Runner
from cortino.orchestrator.stages import StageDeps, default_stages
from cortino.pr.github import LocalGitHost


def git(path, *args) -> str:
    return subprocess.run(["git", "-C", str(path), *args], check=True, capture_output=True, text=True).stdout.strip()


def zip_dir(root: Path, path: Path) -> Path:
    """`root`'s files zipped as a user would upload them."""
    with zipfile.ZipFile(path, "w") as z:
        for f in sorted(root.rglob("*")):
            if f.is_file():
                z.write(f, f.relative_to(root).as_posix())
    return path


def runner(store, catalog, llm) -> Runner:
    deps = StageDeps(llm=llm, catalog=catalog, chart_map=config.load("chart_map")["chart_map"],
                     thresholds=config.load("thresholds"), git_host=LocalGitHost())  # fmt: skip
    return Runner(store, default_stages(deps))


def create_run(store, upload: Path, repo: Path, expected: Path, name: str) -> str:
    """A run with the meta `cortino run` writes, and the fixture's ground truth in expected/."""
    meta = {"upload": str(upload), "name": name, "target_repo": str(repo), "notes": "", "references": []}
    rid = store.create(upload.name, meta).id
    shutil.copytree(expected, store.path(rid) / "expected")
    return rid


def report(store, rid) -> dict:
    return json.loads((store.path(rid) / "verify" / "report.json").read_text(encoding="utf-8"))


def events(store, rid) -> list[dict]:
    return [json.loads(e) for e in (store.path(rid) / "events.jsonl").read_text(encoding="utf-8").splitlines()]


def assert_verified(report: dict, chromium: bool, visuals: list[str]) -> dict:
    """Every check passed; the browser checks passed, or were skipped as such when Chromium is
    missing; every visual in `visuals` was compared with its ground-truth CSV."""
    assert report["passed"] is True and report["by_owner"] == {}, report
    checks = {c["name"]: c for c in report["checks"]}
    assert list(checks) == list(CHECKS)
    for name in CHECKS:
        if name in BROWSER_CHECKS and not chromium:
            assert checks[name]["skipped"] is True and checks[name]["passed"] is False
            assert checks[name]["evidence"]["reason"] == BROWSER_UNAVAILABLE
        else:
            assert checks[name]["passed"] is True and not checks[name].get("skipped"), checks[name]
    truth = {v: i["ground_truth"] for v, i in checks["data_values"]["evidence"]["visuals"].items()}
    assert truth == {v: f"{v}.csv" for v in visuals}  # compared with the ground truth, not just the checker
    return checks
