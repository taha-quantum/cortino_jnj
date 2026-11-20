from typing import Literal

from pydantic import BaseModel, ConfigDict


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceMapping(_Model):
    source_id: str | None
    spec_table: str
    target_table: str | None
    confidence: float
    method: Literal["explicit", "overlap", "llm", "user"]
    evidence: str


class ColumnMapping(_Model):
    spec_table: str
    spec_column: str
    target_table: str
    target_column: str
    types_compatible: bool


class MeasureTranslation(_Model):
    name: str
    original: str
    sql_expression: str
    difficulty: Literal["simple", "hard"]
    test_query: str | None = None
    flagged: bool = False


class ViewDef(_Model):
    name: str
    sql: str
    grain: str


class Param(_Model):
    name: str
    type: str


class Dataset(_Model):
    id: str
    visual_ids: list[str]
    sql: str
    params: list[Param] = []
    columns: list[tuple[str, str]] = []


class VisualMapping(_Model):
    visual_id: str
    component: str
    dataset_id: str


class Unsupported(_Model):
    item: str
    reason: str
    fallback: str


class BuildTask(_Model):
    owner: Literal["data", "ui"]
    description: str
    depends_on: list[str] = []


class Plan(_Model):
    plan_version: Literal["0.1"]
    source_map: list[SourceMapping] = []
    column_map: list[ColumnMapping] = []
    measures: list[MeasureTranslation] = []
    views: list[ViewDef] = []
    datasets: list[Dataset] = []
    visual_map: list[VisualMapping] = []
    unsupported: list[Unsupported] = []
    questions: list[str] = []
    tasks: list[BuildTask] = []
