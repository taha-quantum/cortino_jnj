import importlib.util
from pathlib import Path

import pytest

from cortino.plan.models import Plan
from cortino.spec.models import load_spec

SALES = Path(__file__).parent / "fixtures" / "sales"


def _make_warehouse():
    spec = importlib.util.spec_from_file_location(
        "make_warehouse", SALES / "make_warehouse.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="session")
def sales_fixture() -> Path:
    return SALES


@pytest.fixture(scope="session")
def warehouse(tmp_path_factory) -> Path:
    """Path to the sales DuckDB warehouse; open it with read_only=True."""
    return _make_warehouse().build(
        tmp_path_factory.mktemp("warehouse") / "sales.duckdb"
    )


@pytest.fixture
def spec(sales_fixture):
    return load_spec(sales_fixture / "golden_spec.json")


@pytest.fixture
def plan(sales_fixture) -> Plan:
    """The plan the real planner makes for the golden spec (tests/planning reply())."""
    return Plan.model_validate_json((sales_fixture / "plan.json").read_text(encoding="utf-8"))


@pytest.fixture
def sales_dashboard(tmp_path, spec, plan, warehouse):
    """The built sales dashboard dir and a throwaway warehouse copy with its views applied."""
    from cortino.building.data_builder import build_data
    from cortino.building.ui_builder import build_ui
    from cortino.llm import FakeLLM
    from cortino.tools.repo_tools import RepoTools
    from cortino.tools.warehouse import DuckDBWarehouse, throwaway_copy

    tools = RepoTools(tmp_path / "wt")
    build_data(plan, spec, tools, FakeLLM({}), None)
    ids = [m.visual_id for m in plan.visual_map]
    titles = {"charts": [{"id": i, "title": i, "number_format": None} for i in ids]}
    build_ui(plan, spec, tools, FakeLLM({"builder": [titles]}), None)
    source = DuckDBWarehouse(warehouse, read_only=True)
    db = throwaway_copy(source, tmp_path / "db")
    source.close()
    dashboard = tools.root / "dashboards" / "sales"
    views = (dashboard / "views.sql").read_text(encoding="utf-8")
    if views.strip():
        db.query(views)
    yield dashboard, db
    db.close()
