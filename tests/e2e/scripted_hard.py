"""Scripted LLM replies that build the sales_hard fixture correctly, using the skill patterns.

planner: datasets, visual map, tasks and the hard measures (each with a test query); the fixed
parts are filled in by code. Power BI: `Sales YTD` (TOTALYTD: SUM() OVER (PARTITION BY year ORDER
BY month)), `Sales LY` (SAMEPERIODLASTYEAR: monthly totals joined on year - 1), `% of Total`
(CALCULATE + ALL: SUM() OVER () as the denominator); the date slicer is bound to the % of Total
chart only, because the platform's date control is one date, and one day does not move a
year-to-date line (the time-intelligence charts are listed as skipped for that filter). Tableau:
`{FIXED [region]: SUM([amount])}` (GROUP BY subquery without the filter, joined back) and
`RUNNING_SUM(SUM([amount]))` (SUM() OVER (ORDER BY date), after the filter); the date filter
reaches both. checker: one independent SELECT per visual, written differently from the plan.
"""

from pathlib import Path

from scripted_llm import ScriptedLLM

HARD = Path(__file__).resolve().parent.parent / "fixtures" / "sales_hard"
ALIASES = {"analytics.sales": "s", "analytics.customers": "c", "analytics.dates": "d", "analytics.products": "p"}

_SC = "FROM analytics.sales s JOIN analytics.customers c ON s.customer_id = c.customer_id"
_SD = "JOIN analytics.dates d ON s.order_date = d.date"
_REGION_TOTALS = ("SELECT c2.region, SUM(s2.amount) AS region_sales FROM analytics.sales s2 "
                  "JOIN analytics.customers c2 ON s2.customer_id = c2.customer_id GROUP BY 1")  # fmt: skip
# SAMEPERIODLASTYEAR at month grain: monthly totals joined on (year - 1, month) over the date table's
# months (a date join loses 29 February).
_MONTHLY = (f"m AS (SELECT d.year, d.month, SUM(s.amount) AS amount FROM analytics.sales s {_SD} GROUP BY 1, 2)")
_SPINE_LY = ("(SELECT DISTINCT d.year, d.month FROM analytics.dates d) cur "
             "JOIN m ly ON ly.year = cur.year - 1 AND ly.month = cur.month")  # fmt: skip
_TASKS = [{"owner": "data", "description": "write the datasets"},
          {"owner": "ui", "description": "lay out the page", "depends_on": ["data"]}]  # fmt: skip


def _ds(ds_id: str, visual_id: str, sql: str, param: str | None = None) -> dict:
    return {"id": ds_id, "visual_ids": [visual_id], "sql": sql,
            "params": [{"name": param, "type": "date"}] if param else []}  # fmt: skip


def _hard(name: str, original: str, sql_expression: str, test_query: str) -> dict:
    return {"name": name, "original": original, "sql_expression": sql_expression,
            "difficulty": "hard", "test_query": test_query}  # fmt: skip


PBIP_VISUALS = ["barShareByRegion", "lineSalesYtd", "lineSalesLy"]
PBIP_REPLY = {
    "plan_version": "0.1",
    "measures": [
        _hard("Sales YTD", "TOTALYTD([Total Sales], 'Date'[date])",
              "SUM(SUM(s.amount)) OVER (PARTITION BY d.year ORDER BY d.month)",
              f"SELECT SUM(s.amount) AS sales_ytd FROM analytics.sales s {_SD} "
              "WHERE d.year = (SELECT MAX(d2.year) FROM analytics.dates d2)"),
        _hard("Sales LY", "CALCULATE(\n    [Total Sales],\n    SAMEPERIODLASTYEAR('Date'[date])\n)",
              "SUM(s.amount)",  # of the same month a year earlier
              f"WITH {_MONTHLY} SELECT SUM(ly.amount) AS sales_ly FROM {_SPINE_LY}"),
        _hard("% of Total", "DIVIDE(\n    [Total Sales],\n    CALCULATE([Total Sales], ALL(Customer))\n)",
              "SUM(s.amount) / NULLIF(SUM(SUM(s.amount)) OVER (), 0)",
              "SELECT SUM(s.amount) / NULLIF((SELECT SUM(t.amount) FROM analytics.sales t), 0) AS of_total "
              "FROM analytics.sales s"),
    ],
    "datasets": [
        _ds("share_by_region", "barShareByRegion",
            f"SELECT c.region, SUM(s.amount) / NULLIF(SUM(SUM(s.amount)) OVER (), 0) AS of_total {_SC} {_SD} "
            "WHERE ($date IS NULL OR d.date = $date) GROUP BY 1", "date"),
        _ds("sales_ytd", "lineSalesYtd",
            "SELECT d.month, d.year, SUM(SUM(s.amount)) OVER (PARTITION BY d.year ORDER BY d.month) AS sales_ytd "
            f"FROM analytics.sales s {_SD} GROUP BY 1, 2"),
        _ds("sales_ly", "lineSalesLy", f"WITH {_MONTHLY} SELECT cur.month, cur.year, ly.amount AS sales_ly FROM {_SPINE_LY}"),
    ],
    "visual_map": [
        {"visual_id": "barShareByRegion", "component": "bar", "dataset_id": "share_by_region"},
        {"visual_id": "lineSalesYtd", "component": "line", "dataset_id": "sales_ytd"},
        {"visual_id": "lineSalesLy", "component": "line", "dataset_id": "sales_ly"},
    ],
    "tasks": _TASKS,
}
PBIP_CHECKER = {
    "barShareByRegion": lambda w: (
        f"SELECT c.region, SUM(s.amount) / (SELECT SUM(s.amount) FROM analytics.sales s {_SD}{w}) AS of_total "
        f"{_SC} {_SD}{w} GROUP BY 1"),
    "lineSalesYtd": lambda w: (
        "SELECT m.month, m.year, (SELECT SUM(t.amount) FROM analytics.sales t WHERE year(t.order_date) = m.year "
        "AND month(t.order_date) <= m.month) AS sales_ytd "
        f"FROM (SELECT DISTINCT d.month, d.year FROM analytics.sales s {_SD}{w}) m"),
    "lineSalesLy": lambda w: (
        f"SELECT month(s.order_date) AS month, year(s.order_date) + 1 AS year, SUM(s.amount) AS sales_ly {_SC}{w} "
        "GROUP BY 1, 2 HAVING year(s.order_date) + 1 IN (SELECT year FROM analytics.dates)"),
}  # fmt: skip

TWB_VISUALS = ["Region Sales by Customer", "Running Sales"]
TWB_REPLY = {
    "plan_version": "0.1",
    "measures": [
        _hard("Region Sales", "{FIXED [region]: SUM([amount])}",
              "(SELECT SUM(s2.amount) FROM analytics.sales s2 JOIN analytics.customers c2 "
              "ON s2.customer_id = c2.customer_id WHERE c2.region = c.region)",
              f"SELECT c.region, SUM(s.amount) AS region_sales {_SC} GROUP BY 1"),
        _hard("Running Sales", "RUNNING_SUM(SUM([amount]))",
              "SUM(SUM(s.amount)) OVER (ORDER BY s.order_date)",
              "SELECT s.order_date, SUM(SUM(s.amount)) OVER (ORDER BY s.order_date) AS running_sales "
              "FROM analytics.sales s GROUP BY 1"),
    ],
    "datasets": [
        _ds("region_sales_by_customer", "Region Sales by Customer",
            f"SELECT c.name, c.region, AVG(f.region_sales) AS region_sales {_SC} "
            f"JOIN ({_REGION_TOTALS}) f ON f.region = c.region "
            "WHERE ($order_date IS NULL OR s.order_date = $order_date) GROUP BY 1, 2", "order_date"),
        _ds("running_sales", "Running Sales",
            "SELECT s.order_date, SUM(SUM(s.amount)) OVER (ORDER BY s.order_date) AS running_sales "
            "FROM analytics.sales s WHERE ($order_date IS NULL OR s.order_date = $order_date) GROUP BY 1",
            "order_date"),
    ],
    "visual_map": [
        {"visual_id": "Region Sales by Customer", "component": "bar", "dataset_id": "region_sales_by_customer"},
        {"visual_id": "Running Sales", "component": "line", "dataset_id": "running_sales"},
    ],
    "tasks": _TASKS,
}
TWB_CHECKER = {
    "Region Sales by Customer": lambda w: (
        f"WITH rt AS (SELECT c.region, SUM(s.amount) AS total {_SC} GROUP BY 1) "
        f"SELECT c.name, c.region, MAX(rt.total) AS region_sales {_SC} JOIN rt ON rt.region = c.region{w} "
        "GROUP BY 1, 2"),
    "Running Sales": lambda w: (
        f"WITH f AS (SELECT s.order_date, s.amount FROM analytics.sales s{w}) "
        "SELECT x.order_date, (SELECT SUM(y.amount) FROM f y WHERE y.order_date <= x.order_date) AS running_sales "
        "FROM f x GROUP BY 1"),
}  # fmt: skip


def titles(visuals: list[str]) -> dict:
    return {"charts": [{"id": v, "title": v, "number_format": None} for v in visuals]}


def hard_llm(source: str) -> ScriptedLLM:
    """`source` is "pbip" or "twb"."""
    reply, checker, visuals = (PBIP_REPLY, PBIP_CHECKER, PBIP_VISUALS) if source == "pbip" else (
        TWB_REPLY, TWB_CHECKER, TWB_VISUALS)  # fmt: skip
    return ScriptedLLM({"planner": [reply], "builder": [titles(visuals)]}, checker, ALIASES)
