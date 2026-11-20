import pytest
from pydantic import ValidationError

from cortino.platform.config_schema import DashboardConfig


def _cfg(**over):
    chart = {
        "id": "c1", "component": "bar", "dataset": "d1",
        "x": 0, "y": 0, "w": 4, "h": 3, "title": "T",
        "encodings": {"x": "region", "y": ["sales"]},
    }
    cfg = {
        "config_version": "0.1", "title": "D",
        "pages": [{"name": "P", "width": 1280, "height": 720, "charts": [chart]}],
        "filters": [],
        "datasets": [{"id": "d1", "file": "datasets/d1.sql",
                      "params": [{"name": "region", "type": "string"}]}],
    }
    cfg.update(over)
    return cfg


def test_valid_config():
    assert DashboardConfig.model_validate(_cfg()).title == "D"


def test_config_rejects_unknown_component():
    cfg = _cfg()
    cfg["pages"][0]["charts"][0]["component"] = "radar"
    with pytest.raises(ValidationError):
        DashboardConfig.model_validate(cfg)


def test_chart_dataset_must_exist():
    with pytest.raises(ValidationError, match="dataset"):
        DashboardConfig.model_validate(_cfg(datasets=[]))


def test_chart_ids_unique():
    cfg = _cfg()
    cfg["pages"][0]["charts"].append(dict(cfg["pages"][0]["charts"][0]))
    with pytest.raises(ValidationError, match="unique"):
        DashboardConfig.model_validate(cfg)


def test_filter_applies_to_existing_charts():
    f = {"id": "f", "label": "L", "field": "region", "control": "dropdown",
         "param": "region", "applies_to": ["nope"]}
    with pytest.raises(ValidationError, match="applies_to"):
        DashboardConfig.model_validate(_cfg(filters=[f]))
    f["applies_to"] = "all"
    DashboardConfig.model_validate(_cfg(filters=[f]))
    f["applies_to"] = ["c1"]
    DashboardConfig.model_validate(_cfg(filters=[f]))


def _filter(**over):
    return {"id": "f", "label": "L", "field": "region", "control": "multi_select",
            "param": "region", "applies_to": "all", **over}


@pytest.mark.parametrize(
    "path, value",
    [((), {"extra": 1}), (("pages", 0), {"extra": 1}), (("pages", 0, "charts", 0), {"extra": 1}),
     (("pages", 0, "charts", 0, "encodings"), {"extra": 1}), (("datasets", 0), {"extra": 1})],
)
def test_config_forbids_unknown_keys(path, value):
    cfg = _cfg(filters=[_filter()])
    node = cfg
    for key in path:
        node = node[key]
    node.update(value)
    with pytest.raises(ValidationError, match="extra"):
        DashboardConfig.model_validate(cfg)
    cfg = _cfg(filters=[_filter(extra=1)])
    with pytest.raises(ValidationError, match="extra"):
        DashboardConfig.model_validate(cfg)


def test_dataset_and_filter_ids_unique():
    cfg = _cfg()
    cfg["datasets"].append(dict(cfg["datasets"][0]))
    with pytest.raises(ValidationError, match="dataset ids must be unique"):
        DashboardConfig.model_validate(cfg)
    with pytest.raises(ValidationError, match="filter ids must be unique"):
        DashboardConfig.model_validate(_cfg(filters=[_filter(), _filter()]))


def test_dataset_file_matches_id():
    cfg = _cfg()
    cfg["datasets"][0]["file"] = "../d1.sql"
    with pytest.raises(ValidationError, match="datasets/d1.sql"):
        DashboardConfig.model_validate(cfg)


def test_filter_param_declared_by_every_chart_dataset():
    cfg = _cfg(filters=[_filter(param="year")])
    with pytest.raises(ValidationError, match="year"):
        DashboardConfig.model_validate(cfg)
    cfg = _cfg(filters=[_filter(param="year", applies_to=["c1"])])
    with pytest.raises(ValidationError, match="year"):
        DashboardConfig.model_validate(cfg)
