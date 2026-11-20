# Tableau calculations to SQL (DuckDB)

Every Tableau calculated field is a hard measure: write `sql_expression` and a `test_query`.
Field references are local names: `[amount]` is the mapped column of the table that has it.

| Tableau | SQL |
| --- | --- |
| `SUM([c])`, `AVG([c])`, `MIN`, `MAX` | `SUM(t.c)`, `AVG(t.c)`, `MIN(t.c)`, `MAX(t.c)` |
| `COUNTD([c])` | `COUNT(DISTINCT t.c)` (Tableau ignores NULL) |
| `COUNT([c])` | `COUNT(t.c)` |
| `a / b` | `(a) / NULLIF(b, 0)` (Tableau returns NULL on division by zero) |
| `IF c THEN a ELSE b END`, `IIF(c, a, b)` | `CASE WHEN c THEN a ELSE b END` |
| `ZN(x)` | `COALESCE(x, 0)` |

Rules:

- A calc on a shelf is aggregated as its column instance says: `[sum:Calc:qk]` is `SUM`,
  `[avg:Calc:qk]` is `AVG`, `[usr:Calc:qk]` is the calc's own aggregation.
- The view's dimensions and the dashboard filters are the context. Dimension filters apply
  before LOD `INCLUDE`/`EXCLUDE` and table calculations, but not to `FIXED`.
- `test_query`: a complete SELECT over the warehouse; for an LOD or a table calculation, return
  the values at its own grain (one row per fixed dimension, or along the addressing order).

## Hard patterns

Each pattern: the Tableau form, the SQL, the grain it assumes, and the test query.

**FIXED LOD: `{FIXED [region]: SUM([amount])}`.**
A `GROUP BY` subquery at the fixed grain, joined back on the fixed dimensions:
`JOIN (SELECT c2.region, SUM(s2.amount) AS region_sales FROM analytics.sales s2 JOIN
analytics.customers c2 ON s2.customer_id = c2.customer_id GROUP BY 1) f ON f.region =
c.region`, then aggregate `f.region_sales` as the shelf says (`AVG(f.region_sales)`).
Grain: the subquery is at the fixed dimensions; the view can be finer (per customer). FIXED
ignores the dashboard's dimension filters: keep them out of the subquery, put them in the outer
`WHERE`. `sql_expression`: the row-level form, a correlated subquery
`(SELECT SUM(s2.amount) ... WHERE c2.region = c.region)`.
Test query: `SELECT c.region, SUM(s.amount) FROM ... GROUP BY 1`.

**INCLUDE / EXCLUDE LOD.**
`{INCLUDE [customer]: SUM([amount])}`: aggregate at the view's dimensions plus the included
ones in a subquery or CTE, then aggregate again at the view's grain (`AVG(x)` for
`[avg:...]`). `{EXCLUDE [month]: SUM([amount])}`: the view's grain without the excluded
dimension, e.g. `SUM(SUM(s.amount)) OVER (PARTITION BY <view dims except month>)`.
Both see the dashboard filters, so the filters go in the inner query.

**RUNNING_SUM: `RUNNING_SUM(SUM([amount]))`.**
→ `SUM(SUM(s.amount)) OVER (ORDER BY s.order_date)`, grouped by the view's dimensions.
Grain: one row per mark. Default addressing is Table (across): order by the dimension on
Columns; with a dimension on Rows or Color as the partition, add `PARTITION BY` it. Table
calculations run after the dashboard filters: put them in `WHERE` so a filtered view restarts.
Test query: the same SELECT over the whole warehouse.

**RANK: `RANK(SUM([amount]))`.** → `RANK() OVER (ORDER BY SUM(s.amount) DESC)` (Tableau ranks
descending by default; `RANK(x, 'asc')` is `ASC`). Grain: one row per mark.

**WINDOW_SUM / TOTAL: `SUM([amount]) / TOTAL(SUM([amount]))`.**
→ `SUM(s.amount) / NULLIF(SUM(SUM(s.amount)) OVER (), 0)`: the share of the filtered total.
