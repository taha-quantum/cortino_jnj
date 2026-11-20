# Test fixtures

**These fixtures are synthetic.** The `sales` dashboard, its data and its PBIP files were written by hand
for tests. They are not exported from a real report. Real dashboards go in sibling folders
(`tests/fixtures/<name>/`) with the same layout:

```
<name>/
  data/*.csv           raw rows (committed text; never generated at test time)
  make_warehouse.py    build(dest) -> Path: DuckDB file, schema `analytics`, one table per CSV
  pbip/                the dashboard as Power BI Desktop saves it (PBIP: TMDL model + PBIR report)
  golden_spec.json     the DashboardSpec a careful human extracts from pbip/
  plan.json            the Plan the real planner makes for golden_spec.json (build-stage tests)
  expected/<visual_id>.csv   what each data visual shows, computed in plain Python from data/
```

`tests/conftest.py` exposes `sales_fixture` (the folder) and `warehouse` (the DuckDB file, built once
per session in a tmp dir; open it with `read_only=True`).

## Sales fixture

- `data/`: 200 sales lines (130 orders, some with several lines) over 2025-2026, 20 customers in
  4 regions (North, South, East, West) including one named `O'Brien`, 8 products in 3 categories,
  and one row per day in `dates.csv`.
- Warehouse tables: `analytics.sales`, `analytics.customers`, `analytics.products`, `analytics.dates`,
  with the CSV column names.
- `pbip/Sales.SemanticModel/definition/`: `model.tmdl`, `database.tmdl`, `relationships.tmdl`,
  `tables/{Sales,Customer,Product,Date}.tmdl`, `roles/RegionManager.tmdl` (Desktop stores roles there).
- `pbip/Sales.Report/definition/`: PBIR with one page `overview` and six visuals.

## Golden spec conventions (parsers follow these)

- `name` = `"Sales"`, `source_format` = `"pbip_tmdl"`. Every field is written out, including
  defaults, so the file equals `spec.model_dump(mode="json", by_alias=True)`.
- Order: tables, sources and partitions in `ref table` order of `model.tmdl`; columns, measures and
  relationships in file order; pages in `pages.json` `pageOrder`; visuals sorted by visual id.
- One `Source` per table partition: `id` = table name, `kind` = `"sqlserver"`, `server`/`database` from
  `Sql.Database(...)`, `schema`/`table` from `{[Schema=...,Item=...]}` (items are plural, e.g.
  `Customers`). `Table.source_id` = that id.
- Column `data_type` = the TMDL `dataType` verbatim; `source_column` = TMDL `sourceColumn`.
- Relationships: TMDL defaults give `many_to_one` and `single`.
- Measures (all on `Sales`): `expression` = the DAX with TMDL indentation removed and leading/trailing
  whitespace stripped (internal newlines kept); `depends_on` = measures referenced as `[Name]`, in order
  of first appearance (column refs like `Sales[amount]` are not measures).
- Page `id` = page folder name, `name` = `displayName`. Visual `id` = visual folder name,
  `page` = page id, position = PBIR `x`, `y`, `width`→`w`, `height`→`h`.
- Visual types: `card`→`card`, `clusteredBarChart`→`bar`, `lineChart`→`line`, `tableEx`→`table`,
  `slicer`→`slicer`; anything else → `unknown`. `original_type` is the PBIR `visualType` for every visual.
- Encodings: columns `kind="column"` with `aggregation` from the projection (`Function 0` → `"sum"`) or
  `null`; measures `kind="measure"`, `aggregation` `null`. Roles: bar/line `Category`→`x`, `Y`→`y`;
  card, table (in order) and slicer → `values`.
- An unknown visual has no encodings and adds
  `Unknown(where="visual:<id>", what="custom visual <type>", raw=<visual.json text>)`.
- A slicer adds a page filter: `level="page"`, its field, `operator="in"` (`"between"` for a
  `Between` slicer), `values=[]`, `is_slicer=true`.
- RLS: `RlsRole(name, table=<tablePermission table>, filter_expression=<DAX filter, stripped>)`.
- Sort, top N, titles and number formats are not set in the PBIR, so they are empty in the spec.

## Expected CSVs

`expected/<visual_id>.csv` for each data visual (none for the slicer or the custom visual). Header =
the field names; rows sorted by the first column (by value, so months run 1..12); measure values with
2 decimals (`Order Count` too); the card is one column, one row. `test_fixture_consistency.py`
recomputes them from `data/` in plain Python.

## Sales hard fixture (`sales_hard/`)

Hard calculations, slicers and filters at parity, on the **same warehouse** as `sales` (no `data/` or
`make_warehouse.py` of its own: `conftest.py`'s `warehouse` is built from `sales/data/`).

- `pbip/`: the sales model with measures `Total Sales` (simple), `Sales YTD` =
  `TOTALYTD([Total Sales], 'Date'[date])`, `Sales LY` = `CALCULATE([Total Sales],
  SAMEPERIODLASTYEAR('Date'[date]))`, `% of Total` = `DIVIDE([Total Sales], CALCULATE([Total Sales],
  ALL(Customer)))`; no RLS role. One page `overview`: `barShareByRegion` (region × % of Total),
  `lineSalesYtd` and `lineSalesLy` (month on the axis, year as the series), and `slicerDate`, a
  `Between` (date-range) slicer on `Date[date]`.
- `twb/SalesHard.twb`: the sales data source with calcs `Region Sales` = `{FIXED [region]:
  SUM([amount])}` and `Running Sales` = `RUNNING_SUM(SUM([amount]))`. Dashboard `Overview`: `Region
  Sales by Customer` (bar: customer name on rows, `AVG(Region Sales)`, region as color), `Running
  Sales` (line over the exact `order_date`, default Table (across) addressing) and a continuous
  date filter zone on `order_date`. Worksheets carry no `<filter>` of their own.
- `golden_spec.json` (from `pbip/`) and `golden_spec_twb.json` (from the TWB), name `"Sales Hard"`,
  same conventions as above. A `Between` slicer and a continuous (`:qk`) filter zone are page
  filters with `operator="between"`; other slicers stay `"in"`.
- `expected/<visual_id>.csv` for the five data visuals (Tableau ids are worksheet names). Rows
  sorted by the key columns (`month` then `year`; by name; by date); money with 2 decimals, `% of
  Total` with 4. A blank measure has no row (`Sales LY` exists for 2026 only). The FIXED value is
  the region's total over all dates, shown for each customer with sales. Recomputed in plain
  Python by `test_sales_hard_consistency.py`.
- The scripted planner replies that build it (using the skill patterns) are in
  `tests/e2e/scripted_hard.py`, not a `plan.json`.
