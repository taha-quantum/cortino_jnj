# DAX to SQL (DuckDB)

Simple aggregations are translated by code: `SUM`, `COUNT`, `AVERAGE`, `MIN`, `MAX`,
`DISTINCTCOUNT`, `DISTINCTCOUNTNOBLANK`, `COUNTROWS(<home table>)` and `DIVIDE` of two such
measures. Everything else is a hard measure.

| DAX | SQL |
| --- | --- |
| `SUM(T[c])` | `SUM(t.c)` |
| `AVERAGE(T[c])` | `AVG(t.c)` |
| `DISTINCTCOUNT(T[c])` | `COUNT(DISTINCT t.c)` plus 1 when any `t.c` is NULL (DAX counts BLANK) |
| `DIVIDE(a, b)` | `(a) / NULLIF(b, 0)` |
| `CALCULATE(m, T[c] = "x")` | `m` with `FILTER (WHERE t.c = 'x')` or `SUM(CASE WHEN t.c = 'x' THEN ... END)` |
| `IF(cond, a, b)` | `CASE WHEN cond THEN a ELSE b END` |
| `BLANK()` | `NULL` |

Rules:

- DAX `BLANK` and SQL `NULL` differ: `BLANK + 1 = 1` in DAX. Use `COALESCE` where DAX would
  treat BLANK as zero.
- Measures are evaluated in the filter context of the visual. In a dataset, the visual's
  group-by columns and the dashboard filters are that context.
- Every hard measure needs a `test_query`: a complete SELECT returning the measure's total
  over the whole warehouse with no filters.
- A row whose measures are all BLANK is not shown by Power BI: do not return it.

## Hard patterns

Each pattern: the DAX, the SQL at the visual's grain, the grain it assumes, and the test query.
Write the window over the aggregate (`SUM(SUM(s.amount)) OVER (...)`) so the measure's
aggregation stays in the dataset column.

**% of total: `CALCULATE` + `ALL` / `REMOVEFILTERS`.**
`DIVIDE([Total Sales], CALCULATE([Total Sales], ALL(Customer)))`
→ `SUM(s.amount) / NULLIF(SUM(SUM(s.amount)) OVER (), 0)`.
Grain: one row per value of the removed table's columns (e.g. `region`). `ALL(Customer)` drops
only the Customer filters: dashboard filters on other tables (a date) still apply to the
denominator, so put them in `WHERE`. With other group-by columns kept, partition by them:
`OVER (PARTITION BY d.year)`. Equivalent: a total subquery with the same `WHERE`.
`ALL(Customer)` also ignores a slicer on Customer (the share of the whole); `ALLSELECTED(Customer)`
keeps it (the share of what is selected), so the denominator then takes the slicer's filter too.
Test query: `SELECT SUM(s.amount) / NULLIF((SELECT SUM(amount) FROM analytics.sales), 0)
FROM analytics.sales s` (= 1).

**Year to date: `TOTALYTD` / `CALCULATE(m, DATESYTD(...))`.**
`TOTALYTD([Total Sales], 'Date'[date])`
→ `SUM(SUM(s.amount)) OVER (PARTITION BY d.year ORDER BY d.month)`, grouped by year and month.
Grain: one row per year and month (or per date: `ORDER BY d.date`). Join sales to the date
table on the date; take the months from the date table (the spine), so a month with no sales
still shows its YTD. A filter on a single date does not reset the start of the year: YTD for a
date runs from 1 January to that date, so it cannot be computed over rows the filter removed.
Test query (no filter: the last year in the date table):
`SELECT SUM(s.amount) FROM analytics.sales s JOIN analytics.dates d ON s.order_date = d.date
WHERE d.year = (SELECT MAX(year) FROM analytics.dates)`.

**Same period last year: `SAMEPERIODLASTYEAR`.**
`CALCULATE([Total Sales], SAMEPERIODLASTYEAR('Date'[date]))`
Month or year grain: monthly totals `m AS (SELECT d.year, d.month, SUM(s.amount) AS amount
... GROUP BY 1, 2)` joined to the date table's months on the earlier year:
`(SELECT DISTINCT year, month FROM analytics.dates) cur JOIN m ly ON ly.year = cur.year - 1 AND
ly.month = cur.month`. A month whose earlier month has no sales is BLANK: no row. `LAG(amount,
12) OVER (ORDER BY year, month)` is equivalent only over a complete year-month spine.
Date grain only: join `ly.order_date = d.date - INTERVAL 1 YEAR`. Never use the date join at month
grain: 2024-02-29 has no date a year later, so February's LY loses the leap day.
Test query: the monthly join over every month of the date table, summed.

**Running total: `CALCULATE(m, FILTER(ALL('Date'), 'Date'[date] <= MAX('Date'[date])))`.**
→ `SUM(SUM(s.amount)) OVER (ORDER BY d.date)`. Grain: one row per date (or month).

**Rank: `RANKX(ALL(T[c]), [m])`.** → `RANK() OVER (ORDER BY SUM(s.amount) DESC)`.
Grain: one row per `T[c]`.
