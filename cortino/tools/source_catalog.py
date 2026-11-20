import sqlglot
from sqlglot import exp

from cortino import config
from cortino.spec.models import Source
from cortino.tools.warehouse import ReadOnlySQLError, TableInfo, Warehouse


class SourceCatalog:
    def __init__(self, warehouse: Warehouse, mappings: dict, limit: int | None = None):
        self.warehouse = warehouse
        self.mappings = {k.lower(): v for k, v in mappings.items()}
        self.limit = (
            limit if limit is not None else config.load("thresholds")["readonly_sql_limit"]
        )

    def list_sources(self) -> list[str]:
        return self.warehouse.tables()

    def describe_table(self, table: str) -> TableInfo:
        return self.warehouse.describe(table)

    def run_readonly_sql(self, sql: str) -> list[dict]:
        try:
            statements = [s for s in sqlglot.parse(sql, read="duckdb") if s is not None and not isinstance(s, exp.Semicolon)]
        except sqlglot.errors.SqlglotError as e:
            raise ReadOnlySQLError(f"cannot parse SQL: {e}") from e
        if len(statements) != 1:
            raise ReadOnlySQLError("exactly one statement is allowed")
        if not isinstance(statements[0], exp.Query):
            raise ReadOnlySQLError("only SELECT or WITH...SELECT is allowed")
        return self.warehouse.query(sql, limit=self.limit)

    def explicit_mapping(self, source: Source) -> str | None:
        key = ".".join(
            p for p in (source.server, source.database, source.schema_, source.table) if p
        )
        return self.mappings.get(key.lower())
