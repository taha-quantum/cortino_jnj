import re
import shutil
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

import duckdb

from cortino import config

SYSTEM_SCHEMAS = ("information_schema", "pg_catalog")


class WarehouseError(Exception):
    pass


class ReadOnlySQLError(WarehouseError):
    pass


class QueryTimeout(WarehouseError):
    pass


@dataclass
class TableInfo:
    name: str
    columns: list[tuple[str, str]]
    row_count: int
    sample: list[dict]


class Warehouse(Protocol):
    def query(
        self, sql: str, params: dict | None = None, limit: int | None = None
    ) -> list[dict]: ...

    def tables(self) -> list[str]: ...

    def describe(self, table: str) -> TableInfo: ...


def _strip_semicolon(sql: str) -> str:
    """Drop one trailing `;` (a following `-- comment` is kept; the wrapper's
    newline after the SQL keeps it from swallowing the closing paren)."""
    sql = sql.strip()
    if sql.endswith(";"):
        return sql[:-1]
    m = re.search(r";(\s*--[^\n]*)$", sql)
    return sql[: m.start()] + m.group(1) if m else sql


class DuckDBWarehouse:
    def __init__(self, path, read_only: bool = True, timeout_s: float | None = None):
        self.path = Path(path)
        self.read_only = read_only
        self.timeout_s = (
            timeout_s
            if timeout_s is not None
            else config.load("thresholds")["readonly_sql_timeout_s"]
        )
        # No file/network access from SQL, no extension auto-install/load; this
        # cannot be re-enabled from inside a session. Applies to the throwaway
        # copy too: it only needs to mutate its own tables.
        self._conn = duckdb.connect(
            str(self.path),
            read_only=read_only,
            config={
                "enable_external_access": False,
                "autoinstall_known_extensions": False,
                "autoload_known_extensions": False,
            },
        )

    def close(self) -> None:
        self._conn.close()

    def query(
        self, sql: str, params: dict | None = None, limit: int | None = None
    ) -> list[dict]:
        if limit is not None:
            sql = f"SELECT * FROM (\n{_strip_semicolon(sql)}\n) LIMIT {int(limit)}"
        cur = self._conn.cursor()

        def _interrupt():
            cur.interrupt()

        timer = threading.Timer(self.timeout_s, _interrupt)
        timer.start()
        try:
            result = cur.execute(sql, params or {})
            columns = [d[0] for d in result.description or []]
            return [dict(zip(columns, row)) for row in result.fetchall()]
        except duckdb.InterruptException as e:
            raise QueryTimeout(f"query exceeded {self.timeout_s}s timeout") from e
        except duckdb.Error as e:
            raise WarehouseError(str(e)) from e
        finally:
            timer.cancel()
            cur.close()

    def tables(self) -> list[str]:
        rows = self.query(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'pg_catalog') "
            "ORDER BY 1, 2"
        )
        return [f"{r['table_schema']}.{r['table_name']}" for r in rows]

    def describe(self, table: str) -> TableInfo:
        if table not in self.tables():
            raise ValueError(f"unknown table: {table!r}")
        schema, name = table.split(".", 1)
        cols = self.query(
            "SELECT column_name, data_type FROM information_schema.columns "
            "WHERE table_schema = $s AND table_name = $t ORDER BY ordinal_position",
            {"s": schema, "t": name},
        )
        quoted = f'"{schema}"."{name}"'
        count = self.query(f"SELECT count(*) AS n FROM {quoted}")[0]["n"]
        return TableInfo(
            name=table,
            columns=[(c["column_name"], c["data_type"]) for c in cols],
            row_count=count,
            sample=self.query(f"SELECT * FROM {quoted}", limit=5),
        )


def throwaway_copy(src: DuckDBWarehouse, dest_dir) -> DuckDBWarehouse:
    """Writable copy of the source database file; writes never touch the original. A WAL left
    by an earlier copy is removed, or DuckDB would replay it onto the fresh copy."""
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / src.path.name
    dest.with_name(dest.name + ".wal").unlink(missing_ok=True)
    shutil.copyfile(src.path, dest)
    return DuckDBWarehouse(dest, read_only=False, timeout_s=src.timeout_s)
