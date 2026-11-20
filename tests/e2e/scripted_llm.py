"""Scripted FakeLLM replies that make a correct build of the sales fixture end to end.

planner: the datasets, visual map and tasks of tests/fixtures/sales/plan.json (the plan the real
planner makes for the golden spec); the fixed parts (source, column and measure maps,
unsupported) are filled in by code. builder: chart titles for the first build, plus the
corrected data files for a fix loop. checker: one independent SELECT per visual, written from
the spec only (never the plan's SQL), answered per prompt because data_values and the
functional check ask in an order that depends on the plan. cheap: none, because the fixture has
no reference images, so the visual check is layout only.
"""

import json
import re
from pathlib import Path

from cortino.llm import FakeLLM, FakeReply

SALES = Path(__file__).resolve().parent.parent / "fixtures" / "sales"
_PLAN = json.loads((SALES / "plan.json").read_text(encoding="utf-8"))

VISUALS = [m["visual_id"] for m in _PLAN["visual_map"]]
TITLES = {"charts": [{"id": v, "title": v, "number_format": None} for v in VISUALS]}
CORRECT_DATA = {"views_sql": "", "datasets": {d["id"]: d["sql"] for d in _PLAN["datasets"]}}

_FROM = "FROM analytics.sales s JOIN analytics.customers c ON s.customer_id = c.customer_id"
CHECKER_SQL = {  # (select ... from ..., group by): the filter goes between them
    "barSalesByRegion": (f"SELECT c.region, SUM(s.amount) AS total_sales {_FROM}", " GROUP BY 1"),
    "cardTotalSales": (f"SELECT SUM(s.amount) AS total_sales {_FROM}", ""),
    "lineSalesByMonth": (f"SELECT d.month, SUM(s.amount) AS total_sales {_FROM} "
                         + "JOIN analytics.dates d ON s.order_date = d.date", " GROUP BY 1"),
    "tableByCategory": ("SELECT p.category, SUM(s.amount) AS total_sales, COUNT(DISTINCT s.order_id) AS order_count "
                        + f"{_FROM} JOIN analytics.products p ON s.product_id = p.product_id", " GROUP BY 1"),
}  # fmt: skip
_ALIAS = {"analytics.sales": "s", "analytics.customers": "c", "analytics.dates": "d", "analytics.products": "p"}


def planner_reply(wrong_measure: bool = False) -> dict:
    """The planner's reply; with `wrong_measure` the Total Sales card's dataset uses AVG, not SUM."""
    reply = {k: _PLAN[k] for k in ("plan_version", "views", "datasets", "visual_map", "tasks")}
    if wrong_measure:
        reply = json.loads(json.dumps(reply))
        (card,) = [d for d in reply["datasets"] if d["id"] == "total_sales"]
        card["sql"] = card["sql"].replace("SUM(s.amount)", "AVG(s.amount)")
    return reply


class ScriptedLLM(FakeLLM):
    """FakeLLM with scripted planner/builder/cheap replies and per-visual checker SQL:
    `checker[visual_id](where)`, where `where` is "" or " WHERE <alias>.<column> = $<param>" when
    the prompt binds a filter (`aliases` maps warehouse tables to the aliases the SQL uses)."""

    def __init__(self, script: dict, checker: dict, aliases: dict[str, str]):
        super().__init__(script)
        self.checker, self.aliases = checker, aliases

    def _complete(self, role, system, prompt, tools, images):
        if role != "checker":
            return super()._complete(role, system, prompt, tools, images)
        self.calls.append({"role": role, "system": system, "prompt": prompt, "tools": tools, "images": images})
        visual = re.search(r'Visual:\n\{"id":\s*"([^"]+)"', prompt).group(1)
        bound = re.search(r"the warehouse column (\S+) equals the parameter \$(\w+)\.", prompt)
        where = ""
        if bound:
            table, column = bound.group(1).rsplit(".", 1)
            where = f" WHERE {self.aliases[table]}.{column} = ${bound.group(2)}"
        return FakeReply(self.checker[visual](where))

    def unused(self) -> dict[str, int]:
        """Scripted replies left over, by role."""
        return {role: len(replies) for role, replies in self.script.items() if replies}


def sales_llm(wrong_measure: bool = False) -> ScriptedLLM:
    builder = [TITLES, CORRECT_DATA] if wrong_measure else [TITLES]
    checker = {v: (lambda where, head=head, group=group: head + where + group) for v, (head, group) in CHECKER_SQL.items()}
    return ScriptedLLM({"planner": [planner_reply(wrong_measure)], "builder": builder}, checker, _ALIAS)
