# Chart mapping

The `component` of a `visual_map` entry is set by the spec visual's `type`
(`config/chart_map.yaml`):

| spec type | component |
| --- | --- |
| bar | bar |
| column | column |
| line | line |
| area | area |
| pie | pie |
| donut | donut |
| table | table |
| matrix | table |
| card | card |
| scatter | scatter |
| map | table (no standard equivalent; listed as unsupported) |
| unknown | no entry (listed as unsupported) |
| slicer | no entry: it becomes a dashboard filter control |

Encodings: `x` and `category` are the dataset's group-by column; `y` and `values` measures are
the dataset's value columns. Keep the visual's sort and top N in the dataset SQL or the chart.
