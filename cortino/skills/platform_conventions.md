# Reference platform conventions

- A dashboard is one config (`title`, `pages`, `filters`, `datasets`). Each chart has an `id`
  (the spec visual id), a `component`, a `dataset`, a position and a `title`.
- A dataset is one SQL file holding a single SELECT in DuckDB SQL, read-only, over the
  warehouse tables (`schema.table`, for example `analytics.sales`).
- Filters are dashboard controls (`dropdown`, `multi_select`, `date_range`) bound to a dataset
  parameter. In SQL a parameter is `$name`; a NULL value means "no filter". Never build SQL
  from filter values.
- Dataset column names are lower snake case and stable: the charts refer to them by name.
