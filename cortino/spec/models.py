from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

VisualType = Literal[
    "bar", "column", "line", "area", "pie", "donut",
    "table", "matrix", "card", "scatter", "map", "slicer", "unknown",
]  # fmt: skip


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)


class Source(_Model):
    id: str
    kind: Literal["sqlserver", "snowflake", "postgres", "csv", "excel", "other"]
    server: str | None = None
    database: str | None = None
    schema_: str | None = Field(default=None, alias="schema")
    table: str | None = None
    query: str | None = None


class Column(_Model):
    name: str
    data_type: str
    source_column: str | None = None


class Table(_Model):
    name: str
    source_id: str | None = None
    columns: list[Column] = []


class Relationship(_Model):
    from_table: str
    from_column: str
    to_table: str
    to_column: str
    cardinality: Literal["many_to_one", "one_to_one", "many_to_many"]
    cross_filter: Literal["single", "both"]


class Measure(_Model):
    name: str
    table: str
    expression: str
    language: Literal["dax", "tableau", "m"]
    depends_on: list[str] = []


class FieldRef(_Model):
    table: str | None = None
    name: str
    kind: Literal["column", "measure"]
    aggregation: str | None = None


class Position(_Model):
    x: float
    y: float
    w: float
    h: float


class Encoding(_Model):
    role: Literal["x", "y", "series", "size", "tooltip", "values", "category"]
    field: FieldRef


class Sort(_Model):
    field: FieldRef
    direction: Literal["asc", "desc"]


class Filter(_Model):
    level: Literal["report", "page", "visual"]
    field: FieldRef
    operator: Literal["in", "between", "eq", "gte", "lte", "relative_date"]
    values: list[Any] = []
    is_slicer: bool = False


class Visual(_Model):
    id: str
    page: str
    type: VisualType
    original_type: str | None = None
    position: Position | None = None
    encodings: list[Encoding] = []
    sort: list[Sort] = []
    top_n: int | None = None
    filters: list[Filter] = []
    title: str | None = None
    number_format: str | None = None


class Page(_Model):
    id: str
    name: str
    visuals: list[Visual] = []
    filters: list[Filter] = []


class Parameter(_Model):
    name: str
    data_type: str
    default: Any | None = None
    allowed_values: list[Any] | None = None


class RlsRole(_Model):
    name: str
    table: str
    filter_expression: str


def _truncate(value: Any) -> Any:
    return value[:2000] if isinstance(value, str) else value


class Unknown(_Model):
    where: str
    what: str
    raw: Annotated[str, BeforeValidator(_truncate)] = ""


class DashboardSpec(_Model):
    spec_version: Literal["0.1"]
    name: str
    source_format: Literal["pbip_tmdl", "pbip_bim", "pbit", "pbix", "twb", "twbx"]
    sources: list[Source] = []
    tables: list[Table] = []
    relationships: list[Relationship] = []
    measures: list[Measure] = []
    pages: list[Page] = []
    filters: list[Filter] = []
    parameters: list[Parameter] = []
    rls_roles: list[RlsRole] = []
    unknowns: list[Unknown] = []


def save_spec(spec: DashboardSpec, path: str | Path) -> None:
    Path(path).write_text(spec.model_dump_json(by_alias=True, indent=2), encoding="utf-8")


def load_spec(path: str | Path) -> DashboardSpec:
    return DashboardSpec.model_validate_json(Path(path).read_text(encoding="utf-8"))
