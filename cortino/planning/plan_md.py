from cortino.plan.models import Plan
from cortino.spec.models import DashboardSpec


def _cell(value) -> str:
    return str(value if value is not None else "—").replace("|", "\\|").replace("\n", " ")


def _table(header: list[str], rows: list[list]) -> list[str]:
    if not rows:
        return ["None."]
    lines = ["| " + " | ".join(header) + " |", "|" + " --- |" * len(header)]
    return lines + ["| " + " | ".join(_cell(v) for v in row) + " |" for row in rows]


def render_plan_md(plan: Plan, spec: DashboardSpec) -> str:
    titles = {v.id: v.title or v.id for p in spec.pages for v in p.visuals}
    out = [f"# Plan: {spec.name}", "", "## Source mapping", ""]
    out += _table(
        ["Spec table", "Target table", "Confidence", "Method", "Evidence"],
        [[m.spec_table, m.target_table, f"{m.confidence:.2f}", m.method, m.evidence]
         for m in plan.source_map],
    )  # fmt: skip
    out += ["", "## Column mapping", ""]
    out += _table(
        ["Spec column", "Target column", "Types compatible"],
        [[f"{c.spec_table}.{c.spec_column}", f"{c.target_table}.{c.target_column}",
          "yes" if c.types_compatible else "NO"] for c in plan.column_map],
    )  # fmt: skip
    out += ["", "## Measures", ""]
    if not plan.measures:
        out.append("None.")
    for m in sorted(plan.measures, key=lambda m: m.difficulty != "hard"):
        flag = " (flagged for review)" if m.flagged else ""
        out += [f"### {m.name} — {m.difficulty}{flag}", "", "Original:", "", "```",
                m.original, "```", "", "SQL:", "", "```sql", m.sql_expression, "```", ""]  # fmt: skip
        if m.test_query:
            out += ["Test query:", "", "```sql", m.test_query, "```", ""]
    out += ["", "## Datasets", ""]
    if plan.views:
        out += ["Views:", ""]
        for v in plan.views:
            out += [f"- `{v.name}` (grain: {v.grain})", "", "```sql", v.sql, "```", ""]
    if not plan.datasets:
        out.append("None.")
    for d in plan.datasets:
        params = ", ".join(f"${p.name} ({p.type})" for p in d.params) or "none"
        out += [f"### {d.id}", "", f"Visuals: {', '.join(d.visual_ids)}. Parameters: {params}.",
                "", "```sql", d.sql, "```", ""]  # fmt: skip
    out += ["", "## Visuals", ""]
    out += _table(
        ["Visual", "Title", "Component", "Dataset"],
        [[e.visual_id, titles.get(e.visual_id), e.component, e.dataset_id] for e in plan.visual_map],
    )
    out += ["", "## Unsupported", ""]
    out += _table(
        ["Item", "Reason", "Fallback"], [[u.item, u.reason, u.fallback] for u in plan.unsupported]
    )
    out += ["", "## Questions", ""]
    out += [f"- {q}" for q in plan.questions] or ["None."]
    return "\n".join(out) + "\n"
