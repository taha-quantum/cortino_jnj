from pathlib import Path

import pytest
from pipeline import git

from cortino import config
from cortino.orchestrator.run_store import RunStore
from cortino.tools.browser import PlaywrightBrowser
from cortino.tools.source_catalog import SourceCatalog
from cortino.tools.warehouse import DuckDBWarehouse


@pytest.fixture(scope="module")
def chromium() -> bool:
    """Whether Chromium launches here; without it the browser checks must be reported skipped."""
    try:
        PlaywrightBrowser().close()
    except Exception:  # noqa: BLE001 - any launch failure means no browser here
        return False
    return True


@pytest.fixture
def repo(tmp_path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.name", "Test")
    git(repo, "config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# target\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-m", "init")
    return repo


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runs")
    yield s
    s.close()


@pytest.fixture
def catalog(warehouse):
    source = DuckDBWarehouse(warehouse, read_only=True)
    yield SourceCatalog(source, config.load("sources")["mappings"])
    source.close()
