import json
import shutil
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from cortino import config
from cortino.building.data_builder import build_data
from cortino.building.datasets import dashboard_dir
from cortino.building.integrate import integrate
from cortino.building.ui_builder import build_ui, ui_skipped
from cortino.building.worktree import commit_all, create_worktree, slugify
from cortino.checking import Finding
from cortino.checking.verify import run_verify
from cortino.intake.intake import run_intake
from cortino.llm import LLM, StrandsLLM
from cortino.orchestrator.runner import RunContext, StageError, StageFn, StageResult
from cortino.orchestrator.states import RunState as S
from cortino.parsers.dispatch import parse
from cortino.plan.models import Plan
from cortino.planning.plan_md import render_plan_md
from cortino.planning.planner import make_plan
from cortino.pr.github import GhCliGitHost, GitHost, LocalGitHost
from cortino.pr.pr_stage import open_pr_stage
from cortino.spec.models import load_spec, save_spec
from cortino.tools.repo_tools import RepoTools
from cortino.tools.source_catalog import SourceCatalog
from cortino.tools.warehouse import DuckDBWarehouse, throwaway_copy


def intake_stage(ctx: RunContext) -> StageResult:
    meta = ctx.store.get(ctx.run.id).meta
    upload = meta["upload"]  # a link stays a str: run_intake fetches it
    result = run_intake(
        ctx.store,
        ctx.run.id,
        upload if upload.lower().startswith(("http://", "https://")) else Path(upload),
        meta.get("notes", ""),
        meta["target_repo"],
        [Path(p) for p in meta.get("references", [])],
    )
    if not result.ok:
        return StageResult(S.NEEDS_INPUT, note=result.needs_input)
    source_type = ctx.store.get(ctx.run.id).meta["source_type"]
    spec = parse(ctx.path / "extracted", source_type, meta["name"])
    save_spec(spec, ctx.path / "spec.json")
    return StageResult(S.PARSED)


@dataclass
class StageDeps:
    llm: LLM
    catalog: SourceCatalog
    chart_map: dict
    thresholds: dict
    git_host: GitHost = field(default_factory=LocalGitHost)

    def close(self) -> None:
        self.catalog.warehouse.close()


def real_deps() -> StageDeps:
    thresholds = config.load("thresholds")
    sources = config.load("sources")
    path = sources["warehouse"]["path"]
    if not path:
        raise StageError("warehouse.path is not set in config/sources.yaml")
    return StageDeps(
        llm=StrandsLLM(config.load("models"), thresholds),
        catalog=SourceCatalog(DuckDBWarehouse(path, read_only=True), sources["mappings"]),
        chart_map=config.load("chart_map")["chart_map"],
        thresholds=thresholds,
        git_host=GhCliGitHost() if config.load("git")["git_host"] == "github" else LocalGitHost(),
    )


def _load_plan(path: Path) -> Plan:
    return Plan.model_validate_json(path.read_text(encoding="utf-8"))


def _plan(ctx: RunContext, deps: StageDeps, change_request=None, previous=None) -> Plan:
    """Run the planner with token usage logged to the run; write plan.json and plan.md."""
    deps.llm.on_usage = lambda role, i, o: ctx.store.event(
        ctx.run.id, "llm_usage", role=role, input_tokens=i, output_tokens=o
    )
    spec = load_spec(ctx.path / "spec.json")
    plan = make_plan(
        spec, deps.catalog, deps.llm, deps.chart_map, change_request, previous,
        high_confidence=deps.thresholds["high_confidence"],
    )  # fmt: skip
    (ctx.path / "plan.json").write_text(plan.model_dump_json(indent=2), encoding="utf-8")
    (ctx.path / "plan.md").write_text(render_plan_md(plan, spec), encoding="utf-8")
    return plan


def default_stages(deps: StageDeps | None = None) -> dict[S, StageFn]:
    cache = [deps]

    def get() -> StageDeps:
        if cache[0] is None:
            cache[0] = real_deps()
        return cache[0]

    def planner(ctx: RunContext) -> StageResult:
        _plan(ctx, get())
        ctx.store.set_meta(ctx.run.id, plan_origin="initial", source_map_changed=None)
        return StageResult(S.PLANNED)

    def plan_gate(ctx: RunContext) -> StageResult:
        meta = ctx.store.get(ctx.run.id).meta
        if meta.get("plan_origin") == "change_request" and not meta.get("source_map_changed"):
            return StageResult(S.BUILDING, note="change request did not touch the source mapping")
        t = get().thresholds
        plan = _load_plan(ctx.path / "plan.json")
        if (
            t["auto_approve_plan_when_all_high_confidence"]
            and all(m.confidence >= t["high_confidence"] for m in plan.source_map)
            and not plan.questions
        ):
            return StageResult(S.BUILDING, note="auto-approved: every mapping is high confidence")
        return StageResult(S.AWAITING_PLAN_APPROVAL)

    def changes_requested(ctx: RunContext) -> StageResult:
        lines = (ctx.path / "change_requests.jsonl").read_text(encoding="utf-8").splitlines()
        request = json.loads(lines[-1])
        text = request["text"]
        if request.get("visual_id"):
            text += f" (visual: {request['visual_id']})"
        # Saving the previous plan under the request's number keeps a re-run idempotent.
        saved = ctx.path / f"plan.v{len(lines)}.json"
        if not saved.exists():
            shutil.copyfile(ctx.path / "plan.json", saved)
        previous = _load_plan(saved)
        plan = _plan(ctx, get(), text, previous)
        changed = [
            k for k in Plan.model_fields if getattr(plan, k) != getattr(previous, k)
        ]
        ctx.store.event(ctx.run.id, "plan_changed", changed=changed)
        ctx.store.set_meta(
            ctx.run.id, plan_origin="change_request", source_map_changed="source_map" in changed
        )
        return StageResult(S.PLANNED)

    def worktree(ctx: RunContext):
        meta = ctx.store.get(ctx.run.id).meta
        return create_worktree(Path(meta["target_repo"]), ctx.path, slugify(meta["name"]), ctx.run.id)

    def building(ctx: RunContext) -> StageResult:
        """First build: both builders. After a failed integrate: only the owners in fix.json."""
        deps = get()
        spec, plan = load_spec(ctx.path / "spec.json"), _load_plan(ctx.path / "plan.json")
        wt = worktree(ctx)
        tools = RepoTools(wt.path)
        fix_file = ctx.path / "fix.json"
        fix = None
        if fix_file.exists():
            raw = json.loads(fix_file.read_text(encoding="utf-8"))
            fix = {o: [Finding.model_validate(f) for f in fs] for o, fs in raw.items()}
        usage: list[tuple[int, int]] = []

        def on_usage(role, i, o):
            usage.append((i, o))
            ctx.store.event(ctx.run.id, "llm_usage", role=role, input_tokens=i, output_tokens=o)

        deps.llm.on_usage = on_usage
        with ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(fn, plan, spec, tools, deps.llm, fix and fix[owner])
                    for owner, fn in (("data", build_data), ("ui", build_ui))
                    if fix is None or owner in fix]  # fmt: skip
            for job in jobs:
                job.result()
        base = dashboard_dir(spec)
        planned = {d.id for d in plan.datasets}
        for orphan in (wt.path / base / "datasets").glob("*.sql"):  # left by an earlier plan
            if orphan.stem not in planned:
                orphan.unlink()
        files = tools.list_files(base)
        built = {m.visual_id for m in plan.visual_map}
        report = {
            "files": {"data": [f for f in files if not f.endswith("/dashboard.json")],
                      "ui": [f for f in files if f.endswith("/dashboard.json")]},
            "skipped": [u.model_dump() for u in plan.unsupported if u.item.removeprefix("visual:") not in built]
                       + ui_skipped(plan, spec),
            "tokens": {"input": sum(i for i, _ in usage), "output": sum(o for _, o in usage)},
        }  # fmt: skip
        (ctx.path / "build_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
        return StageResult(S.INTEGRATING)

    def integrating(ctx: RunContext) -> StageResult:
        deps = get()
        meta = ctx.store.get(ctx.run.id).meta
        spec, plan = load_spec(ctx.path / "spec.json"), _load_plan(ctx.path / "plan.json")
        wt = worktree(ctx)
        db = throwaway_copy(deps.catalog.warehouse, ctx.path / "build_db")
        try:
            result = integrate(wt.path, plan, db, dashboard_dir(spec))
        finally:
            db.close()
            shutil.rmtree(ctx.path / "build_db")
        failures = {o: [f.model_dump() for f in fs] for o, fs in result.failures.items()}
        report_file, fix_file = ctx.path / "build_report.json", ctx.path / "fix.json"
        report = json.loads(report_file.read_text(encoding="utf-8"))
        report["results"] = {"ok": result.ok, "failures": failures}
        if result.ok:
            n = meta.get("builds", 0) + 1
            report["commit"] = commit_all(wt, f"cortino: build v{n}")
            report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
            ctx.store.set_meta(ctx.run.id, builds=n, integrate_retries=0)
            fix_file.unlink(missing_ok=True)
            return StageResult(S.VERIFYING)
        report_file.write_text(json.dumps(report, indent=2), encoding="utf-8")
        retries = meta.get("integrate_retries", 0)
        messages = "; ".join(f.message for fs in result.failures.values() for f in fs)
        if retries >= deps.thresholds["max_integrate_retries"]:
            raise StageError(f"integrate still failing after {retries} retries: {messages}")
        fix_file.write_text(json.dumps(failures, indent=2), encoding="utf-8")
        ctx.store.set_meta(ctx.run.id, integrate_retries=retries + 1)
        return StageResult(S.BUILDING, note=messages, owner_failures=result.failures)

    def verifying(ctx: RunContext) -> StageResult:
        """Fixable failures go to fix.json by owner, so the next build re-runs only the failing
        builders. When no failure is fixable, rebuilding cannot help: NEEDS_INPUT at once."""
        deps = get()
        deps.llm.on_usage = lambda role, i, o: ctx.store.event(
            ctx.run.id, "llm_usage", role=role, input_tokens=i, output_tokens=o
        )
        report_file, fix_file = ctx.path / "verify" / "report.json", ctx.path / "fix.json"
        previous = json.loads(report_file.read_text(encoding="utf-8")) if report_file.exists() else None
        if previous and previous["passed"]:  # verifying again after a change request: run every check
            previous = None
        report = run_verify(ctx, previous, deps.catalog, deps.llm)
        if report.passed:
            fix_file.unlink(missing_ok=True)
            return StageResult(S.AWAITING_PREVIEW_APPROVAL)
        fixable = {o: [f for f in fs if f.fixable] for o, fs in report.by_owner.items()}
        fixable = {o: fs for o, fs in fixable.items() if fs}
        if not fixable:
            fix_file.unlink(missing_ok=True)
            unfixable = [f for fs in report.by_owner.values() for f in fs]
            ctx.store.event(ctx.run.id, "unfixable_findings", findings=[f.model_dump() for f in unfixable])
            return StageResult(S.NEEDS_INPUT, note="no builder can fix: " + "; ".join(f.message for f in unfixable))
        failures = {o: [f.model_dump() for f in fs] for o, fs in fixable.items()}
        fix_file.write_text(json.dumps(failures, indent=2, default=str), encoding="utf-8")
        messages = "; ".join(f.message for fs in fixable.values() for f in fs)
        return StageResult(S.BUILDING, note=messages, owner_failures=fixable)

    return {
        S.INTAKE: intake_stage,
        S.PARSED: planner,
        S.PLANNED: plan_gate,
        S.CHANGES_REQUESTED: changes_requested,
        S.BUILDING: building,
        S.INTEGRATING: integrating,
        S.VERIFYING: verifying,
        S.AWAITING_PREVIEW_APPROVAL: lambda ctx: open_pr_stage(ctx, get().git_host),
    }
