"""Reference platform config format (assumption A2): the target of the build stage."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Encodings(_Model):
    x: str | None = None
    y: list[str] | None = None
    series: str | None = None
    value: str | None = None


class Chart(_Model):
    id: str
    component: Literal[
        "bar", "column", "line", "area", "pie", "donut", "table", "card", "scatter"
    ]
    dataset: str
    x: int
    y: int
    w: int
    h: int
    title: str
    encodings: Encodings = Encodings()
    sort: str | None = None
    top_n: int | None = None
    number_format: str | None = None


class Page(_Model):
    name: str
    width: int
    height: int
    charts: list[Chart] = []


class Filter(_Model):
    id: str
    label: str
    field: str
    control: Literal["dropdown", "multi_select", "date_range"]
    param: str
    applies_to: list[str] | Literal["all"]
    default: str | list[str] | None = None


class DatasetParam(_Model):
    name: str
    type: str


class Dataset(_Model):
    id: str
    file: str
    params: list[DatasetParam] = []


class DashboardConfig(_Model):
    config_version: Literal["0.1"] = "0.1"
    title: str
    pages: list[Page] = []
    filters: list[Filter] = []
    datasets: list[Dataset] = []

    @model_validator(mode="after")
    def _check_references(self) -> "DashboardConfig":
        chart_ids = [c.id for p in self.pages for c in p.charts]
        if len(chart_ids) != len(set(chart_ids)):
            raise ValueError("chart ids must be unique")
        dataset_ids = [d.id for d in self.datasets]
        if len(dataset_ids) != len(set(dataset_ids)):
            raise ValueError("dataset ids must be unique")
        filter_ids = [f.id for f in self.filters]
        if len(filter_ids) != len(set(filter_ids)):
            raise ValueError("filter ids must be unique")
        for d in self.datasets:
            if d.file != f"datasets/{d.id}.sql":
                raise ValueError(f"dataset {d.id}: file must be 'datasets/{d.id}.sql'")
        for p in self.pages:
            for c in p.charts:
                if c.dataset not in dataset_ids:
                    raise ValueError(f"chart {c.id}: unknown dataset {c.dataset!r}")
        charts = {c.id: c for p in self.pages for c in p.charts}
        params = {d.id: {p.name for p in d.params} for d in self.datasets}
        for f in self.filters:
            if f.applies_to != "all":
                missing = [i for i in f.applies_to if i not in charts]
                if missing:
                    raise ValueError(f"filter {f.id}: applies_to unknown charts {missing}")
            targets = charts if f.applies_to == "all" else f.applies_to
            unbound = [i for i in targets if f.param not in params[charts[i].dataset]]
            if unbound:
                raise ValueError(f"filter {f.id}: param {f.param!r} is not declared by the datasets of {unbound}")
        return self
