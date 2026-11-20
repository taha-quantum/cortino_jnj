import json
import shutil
from pathlib import Path

from cortino import config
from cortino.building.worktree import commit_all, create_worktree, slugify
from cortino.orchestrator.runner import RunContext, StageError, StageResult
from cortino.orchestrator.states import RunState as S
from cortino.plan.models import Plan
from cortino.pr.github import GitHost
from cortino.spec.models import load_spec

LABELS = ["cortino", "needs-review"]


def pr_title(name: str, source_format: str) -> str:
    tool = "Tableau" if source_format.startswith("tw") else "Power BI"
    return f"Cortino: rebuild {name} from {tool}"


def _cell(value) -> str:
    return str(value).replace("|", r"\|").replace("\r", "").replace("\n", "<br>")


def _table(header: list[str], rows: list[list]) -> list[str]:
    lines = ["| " + " | ".join(header) + " |", "|" + "---|" * len(header)]
    return lines + ["| " + " | ".join(_cell(c) for c in row) + " |" for row in rows]


def _check_row(check: dict) -> list[str]:
    evidence = check.get("evidence", {})
    messages = [f.get("message", "") for f in check.get("findings", [])]
    notes = []
    if check.get("skipped"):
        status = "skipped"
        reason = evidence.get("reason") or "; ".join(messages) or "no reason recorded"
        notes.append(f"skipped: {reason}")
    else:
        status = "passed" if check["passed"] else "failed"
        notes += messages
    per_visual = {v: i["ground_truth"] for v, i in evidence.get("visuals", {}).items() if "ground_truth" in i}
    if per_visual:
        distinct = set(per_visual.values())
        notes.append(
            f"ground_truth: {distinct.pop()}"
            if len(distinct) == 1
            else "ground_truth: " + "; ".join(f"{v}={g}" for v, g in per_visual.items())
        )
    elif "ground_truth" in evidence:
        notes.append(f"ground_truth: {evidence['ground_truth']}")
    return [check["name"], status, "; ".join(notes)]


def _measure_note(m) -> str:
    if not (m.flagged or m.difficulty == "hard"):
        return ""
    return f"hard, test query: {m.test_query}" if m.test_query else "hard"


def _body(ctx, spec, plan, report, build, branch, slug, screenshots) -> str:
    out = ["## Summary", "",
           f"- Run: `{ctx.run.id}`", f"- Dashboard: {spec.name}",
           f"- Source format: {spec.source_format}", f"- Branch: `{branch}`",
           f"- Verify loops: {report.get('loop', '?')}", ""]  # fmt: skip
    out += ["## Source mapping", ""]
    out += _table(
        ["Original source", "Target table", "Confidence", "Method"],
        [[m.spec_table, m.target_table or "(none)", m.confidence, m.method] for m in plan.source_map],
    )
    out += ["", "## Measure translations", ""]
    out += _table(
        ["Measure", "Original", "SQL", "Notes"],
        [[m.name, m.original, m.sql_expression, _measure_note(m)] for m in plan.measures],
    )
    out += ["", "## Verification results", ""]
    out += _table(["Check", "Status", "Notes"], [_check_row(c) for c in report["checks"]])
    out += ["", "## Screenshots", ""]
    out += [f"![{n}](docs/cortino/{slug}/screenshots/{n})" for n in screenshots] or ["None captured."]
    out += ["", "## Deviations and unsupported", ""]
    seen, items = set(), []
    for u in [*(u.model_dump() for u in plan.unsupported), *build.get("skipped", [])]:
        if u["item"] not in seen:
            seen.add(u["item"])
            items.append([u["item"], u["reason"], u["fallback"]])
    out += _table(["Item", "Reason", "Fallback"], items) if items else ["None."]
    if plan.questions:
        out += ["", "## Questions", ""] + [f"- {q}" for q in plan.questions]
    return "\n".join(out) + "\n"


def open_pr_stage(ctx: RunContext, host: GitHost) -> StageResult:
    run_dir = ctx.path
    report_file = run_dir / "verify" / "report.json"
    if not report_file.exists():
        raise StageError("no verification report: refusing to open a PR without verification evidence")
    meta = ctx.store.get(ctx.run.id).meta
    spec = load_spec(run_dir / "spec.json")
    plan = Plan.model_validate_json((run_dir / "plan.json").read_text(encoding="utf-8"))
    report = json.loads(report_file.read_text(encoding="utf-8"))
    build_file = run_dir / "build_report.json"
    build = json.loads(build_file.read_text(encoding="utf-8")) if build_file.exists() else {}

    slug = slugify(meta["name"])
    wt = create_worktree(Path(meta["target_repo"]), run_dir, slug, ctx.run.id)
    evidence = wt.path / "docs" / "cortino" / slug
    shots = evidence / "screenshots"
    shots.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(run_dir / "spec.json", evidence / "spec.json")
    shutil.copyfile(run_dir / "plan.md", evidence / "plan.md")
    shutil.copyfile(report_file, evidence / "verify_report.json")
    names = []
    sources = [*sorted((run_dir / "verify" / "screenshots").glob("*.png")),
               *sorted((run_dir / "input" / "references").glob("*"))]  # fmt: skip
    for src in sources:
        if src.is_file():
            shutil.copyfile(src, shots / src.name)
            names.append(src.name)
    commit_all(wt, f"cortino: evidence for {meta['name']}")

    title = pr_title(spec.name, spec.source_format)
    body = _body(ctx, spec, plan, report, build, wt.branch, slug, names)
    host.push(wt)
    info = host.open_pr(wt, title, body, config.load("git")["base_branch"], LABELS, host.reviewers(wt))
    (run_dir / "pr.json").write_text(
        json.dumps({"url": info.url, "number": info.number, "branch": wt.branch, "title": title}, indent=2),
        encoding="utf-8",
    )
    ctx.store.set_meta(ctx.run.id, pr_url=info.url, pr_number=info.number)
    return StageResult(S.PR_OPEN)
