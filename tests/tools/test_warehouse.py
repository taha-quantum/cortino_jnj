import pytest

from cortino.spec.models import Source
from cortino.tools.source_catalog import SourceCatalog
from cortino.tools.warehouse import (
    DuckDBWarehouse,
    QueryTimeout,
    ReadOnlySQLError,
    WarehouseError,
    throwaway_copy,
)

MAPPINGS = {"server01.salesdb.dbo.Sales": "analytics.sales"}


@pytest.fixture
def wh(warehouse):
    w = DuckDBWarehouse(warehouse, read_only=True)
    yield w
    w.close()


@pytest.fixture
def catalog(wh):
    return SourceCatalog(wh, MAPPINGS)


def test_insert_rejected(catalog):
    with pytest.raises(ReadOnlySQLError):
        catalog.run_readonly_sql("INSERT INTO analytics.sales SELECT * FROM analytics.sales")


def test_multi_statement_rejected(catalog):
    with pytest.raises(ReadOnlySQLError):
        catalog.run_readonly_sql("SELECT 1; DROP TABLE x")


def test_untokenizable_sql_rejected(catalog):
    with pytest.raises(ReadOnlySQLError):
        catalog.run_readonly_sql("SELECT 'abc")


def test_with_select_allowed_and_semicolon_ok(catalog):
    assert catalog.run_readonly_sql("WITH a AS (SELECT 1 AS n) SELECT n FROM a;") == [{"n": 1}]


def test_limit_enforced(wh):
    assert len(wh.query("SELECT * FROM range(5000)", limit=10)) == 10
    catalog = SourceCatalog(wh, {}, limit=7)
    assert len(catalog.run_readonly_sql("SELECT * FROM range(5000)")) == 7


def test_describe_has_sample_rows_5(wh):
    info = wh.describe("analytics.sales")
    assert len(info.sample) == 5
    assert info.row_count > 5
    assert all(len(c) == 2 for c in info.columns)
    assert "analytics.sales" in wh.tables()


def test_describe_unknown_table_rejected(wh):
    with pytest.raises(ValueError):
        wh.describe("analytics.sales; DROP TABLE x")


def test_explicit_mapping_case_insensitive(catalog):
    src = Source(id="s", kind="sqlserver", server="SERVER01", database="SalesDB",
                 schema="DBO", table="sales")
    assert catalog.explicit_mapping(src) == "analytics.sales"
    other = Source(id="s", kind="sqlserver", server="x", database="y", schema="z", table="t")
    assert catalog.explicit_mapping(other) is None


def test_params_bound(wh):
    assert wh.query(
        "SELECT * FROM analytics.customers WHERE region = $region",
        {"region": "O'Brien"},
    ) == []


def test_params_bound_finds_real_quote_value(wh):
    rows = wh.query(
        "SELECT * FROM analytics.customers WHERE name = $n", {"n": "O'Brien"}
    )
    assert len(rows) >= 1


def test_timeout(warehouse):
    w = DuckDBWarehouse(warehouse, read_only=True, timeout_s=0.2)
    with pytest.raises(QueryTimeout):
        w.query("SELECT count(*) FROM range(100000000) a, range(100000000) b")
    assert w.query("SELECT 1 AS x") == [{"x": 1}]
    w.close()


def test_read_only_connection_rejects_writes(wh):
    with pytest.raises(WarehouseError):
        wh.query("CREATE TABLE t AS SELECT 1")


def test_throwaway_copy_is_writable_and_isolated(wh, tmp_path):
    copy = throwaway_copy(wh, tmp_path)
    copy.query("DELETE FROM analytics.sales")
    assert copy.query("SELECT count(*) AS n FROM analytics.sales") == [{"n": 0}]
    assert wh.describe("analytics.sales").row_count > 0
    copy.close()


def test_throwaway_copy_drops_a_stale_wal(wh, tmp_path):
    crashed = throwaway_copy(wh, tmp_path)  # a run that died before its WAL was checkpointed
    crashed.query("PRAGMA disable_checkpoint_on_shutdown; SET wal_autocheckpoint = '1TB'")
    crashed.query("DELETE FROM analytics.sales")
    crashed.close()
    assert (tmp_path / f"{wh.path.name}.wal").exists()
    copy = throwaway_copy(wh, tmp_path)
    assert copy.query("SELECT count(*) AS n FROM analytics.sales")[0]["n"] > 0
    copy.close()


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('x.csv')",
        "SELECT * FROM 'x.csv'",
        "SELECT * FROM glob('*')",
        "SELECT * FROM read_text('x.txt')",
        "SELECT * FROM read_json('x.json')",
        "SELECT * FROM read_blob('x')",
    ],
)
def test_file_reads_blocked(catalog, sql):
    with pytest.raises(WarehouseError):
        catalog.run_readonly_sql(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "COPY analytics.sales TO 'out.csv'",
        "ATTACH 'other.duckdb' AS o",
        "PRAGMA database_list",
        "SET enable_external_access = true",
        "INSTALL httpfs",
        "LOAD httpfs",
        "CALL pragma_version()",
    ],
)
def test_non_select_statements_rejected(catalog, sql):
    with pytest.raises(ReadOnlySQLError):
        catalog.run_readonly_sql(sql)


def test_external_access_cannot_be_reenabled_on_raw_query(wh):
    with pytest.raises(WarehouseError):
        wh.query("SET enable_external_access = true")
    with pytest.raises(WarehouseError):
        wh.query("SELECT * FROM read_csv('x.csv')")


def test_trailing_comment_and_semicolon(catalog):
    assert catalog.run_readonly_sql("SELECT 1 AS a -- note") == [{"a": 1}]
    assert catalog.run_readonly_sql("SELECT 1 AS a; -- note") == [{"a": 1}]


def test_sql_errors_are_warehouse_errors(catalog):
    with pytest.raises(WarehouseError):
        catalog.run_readonly_sql("SELECT * FROM no_such_table")
