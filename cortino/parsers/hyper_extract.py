"""Read a Tableau Hyper extract (.hyper) as ground-truth data. Needs the optional `tableau` extra."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass
class ExtractTable:
    columns: list[str]
    rows: list[list[Any]]


def read_extract(path: Path) -> dict[str, ExtractTable]:
    """Every table of the extract, keyed by table name (schema ignored)."""
    from tableauhyperapi import Connection, HyperProcess, Telemetry

    tables: dict[str, ExtractTable] = {}
    with (
        HyperProcess(
            Telemetry.DO_NOT_SEND_USAGE_DATA_TO_TABLEAU, parameters={"log_config": ""}
        ) as hyper,
        Connection(hyper.endpoint, str(path)) as conn,
    ):
        for schema in conn.catalog.get_schema_names():
            for table_name in conn.catalog.get_table_names(schema):
                definition = conn.catalog.get_table_definition(table_name)
                rows = conn.execute_list_query(f"SELECT * FROM {table_name}")
                tables[table_name.name.unescaped] = ExtractTable(
                    columns=[c.name.unescaped for c in definition.columns], rows=rows
                )
    return tables
