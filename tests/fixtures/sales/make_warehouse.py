"""Build the sales DuckDB warehouse (schema `analytics`) from the committed CSVs."""

from pathlib import Path

import duckdb

DATA = Path(__file__).parent / "data"

TABLES = {
    "sales": {
        "order_id": "INTEGER",
        "order_date": "DATE",
        "customer_id": "INTEGER",
        "product_id": "INTEGER",
        "quantity": "INTEGER",
        "amount": "DECIMAL(12,2)",
    },
    "customers": {"customer_id": "INTEGER", "name": "VARCHAR", "region": "VARCHAR"},
    "products": {"product_id": "INTEGER", "name": "VARCHAR", "category": "VARCHAR"},
    "dates": {"date": "DATE", "year": "INTEGER", "month": "INTEGER"},
}


def build(dest: Path) -> Path:
    """Create the DuckDB file at `dest` and return its path."""
    with duckdb.connect(str(dest)) as con:
        con.execute("CREATE SCHEMA analytics")
        for table, columns in TABLES.items():
            con.execute(
                f"CREATE TABLE analytics.{table} AS SELECT * FROM read_csv(?, header = true, columns = ?)",
                [str(DATA / f"{table}.csv"), columns],
            )
    return dest
