"""Run SQL Server in Docker as the *source* system for a demo dashboard, loaded with the same sales
rows as the DuckDB warehouse (make_warehouse.py), so Power BI Desktop or Tableau can render the
original dashboard. Cortino itself never connects to it: it reads the dashboard file and rebuilds on
the DuckDB warehouse.

    uv run python tests/fixtures/sales/make_sqlserver.py          # start (or reuse) and load
    uv run python tests/fixtures/sales/make_sqlserver.py --reset  # remove the container first

The sa password comes from MSSQL_SA_PASSWORD (shell or the repo's .env). If neither has one, a
random password is generated and appended to .env, which git ignores; it is never printed.
Connect from Power BI / Tableau with server `localhost`, database `salesdb`, user `sa`.
"""

import argparse
import csv
import importlib.util
import os
import secrets
import subprocess
import sys
import tempfile
import time
from decimal import Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[2]
CONTAINER = "cortino-salesdb"
IMAGE = "mcr.microsoft.com/mssql/server:2022-latest"
DATABASE = "salesdb"
SQLCMD = "/opt/mssql-tools18/bin/sqlcmd"
T_SQL_TYPES = {"INTEGER": "INT", "DATE": "DATE", "VARCHAR": "NVARCHAR(200)"}  # DECIMAL(p,s) is kept


def _tables() -> dict[str, dict[str, str]]:
    """The column definitions make_warehouse.py builds DuckDB from, so the two never drift apart."""
    spec = importlib.util.spec_from_file_location("make_warehouse", HERE / "make_warehouse.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.TABLES


def _docker(*args: str, check: bool = True, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], check=check, capture_output=capture, text=True)


def _password() -> str:
    sys.path.insert(0, str(REPO))
    from cortino.config import load_env_file

    load_env_file()
    if os.environ.get("MSSQL_SA_PASSWORD"):
        return os.environ["MSSQL_SA_PASSWORD"]
    # SQL Server wants 8+ characters from at least three of: upper, lower, digit, symbol.
    password = "Ct1!" + secrets.token_urlsafe(18)
    env = REPO / ".env"
    text = env.read_text(encoding="utf-8") if env.exists() else ""
    sep = "" if not text or text.endswith("\n") else "\n"
    with env.open("a", encoding="utf-8") as f:
        f.write(f"{sep}\n# Local SQL Server source for demo dashboards (make_sqlserver.py)\nMSSQL_SA_PASSWORD={password}\n")
    os.environ["MSSQL_SA_PASSWORD"] = password
    print(f"Generated an sa password and saved it to {env} as MSSQL_SA_PASSWORD.")
    return password


def _sqlcmd(query: str, database: str = "master") -> str:
    """Run T-SQL in the container. The password travels as an inherited environment variable
    (`-e SQLCMDPASSWORD`), never on a command line."""
    r = subprocess.run(
        ["docker", "exec", "-e", "SQLCMDPASSWORD", CONTAINER, SQLCMD, "-S", "localhost", "-U", "sa",
         "-C", "-b", "-h", "-1", "-W", "-d", database, "-Q", "SET NOCOUNT ON; " + query],
        check=False, capture_output=True, text=True,
        env={**os.environ, "SQLCMDPASSWORD": os.environ["MSSQL_SA_PASSWORD"]},
    )  # fmt: skip
    if r.returncode != 0:
        raise RuntimeError((r.stdout + r.stderr).strip())
    return r.stdout.strip()


def _start(reset: bool) -> None:
    exists = _docker("ps", "-a", "--filter", f"name=^{CONTAINER}$", "--format", "{{.Names}}").stdout.strip()
    if exists and reset:
        _docker("rm", "-f", CONTAINER)
        exists = ""
    if not exists:
        print(f"Starting {IMAGE} as {CONTAINER} on localhost:1433 (the first pull takes a few minutes)...")
        subprocess.run(
            ["docker", "run", "-d", "--name", CONTAINER, "-e", "ACCEPT_EULA=Y", "-e", "MSSQL_SA_PASSWORD",
             "-p", "1433:1433", IMAGE],
            check=True, env=os.environ,
        )  # fmt: skip
    else:
        _docker("start", CONTAINER)
    deadline = time.monotonic() + 180
    while True:  # the server needs ~10-30 s after the container starts
        try:
            _sqlcmd("SELECT 1")
            return
        except RuntimeError as e:
            if "Login failed" in str(e):
                raise SystemExit(
                    f"{CONTAINER} rejected the sa password: it was created with a different one. "
                    "Re-run with --reset to recreate it with MSSQL_SA_PASSWORD."
                ) from None
            if time.monotonic() > deadline:
                raise SystemExit(f"SQL Server did not become ready within 180 s:\n{e}") from None
            time.sleep(3)


def _load(tables: dict[str, dict[str, str]]) -> None:
    _sqlcmd(f"IF DB_ID('{DATABASE}') IS NULL CREATE DATABASE [{DATABASE}];")
    _docker("exec", "-u", "0", CONTAINER, "mkdir", "-p", "/tmp/sales")
    staging = Path(tempfile.mkdtemp())
    for table, columns in tables.items():
        cols = ", ".join(f"[{c}] {T_SQL_TYPES.get(t, t)}" for c, t in columns.items())
        # A Windows checkout has CRLF line endings; BULK INSERT below splits rows on LF only.
        lf = staging / f"{table}.csv"
        lf.write_bytes((HERE / "data" / f"{table}.csv").read_bytes().replace(b"\r\n", b"\n"))
        _docker("cp", str(lf), f"{CONTAINER}:/tmp/sales/{table}.csv")
        _docker("exec", "-u", "0", CONTAINER, "chmod", "644", f"/tmp/sales/{table}.csv")
        _sqlcmd(
            f"DROP TABLE IF EXISTS dbo.[{table}]; CREATE TABLE dbo.[{table}] ({cols}); "
            f"BULK INSERT dbo.[{table}] FROM '/tmp/sales/{table}.csv' "
            "WITH (FORMAT = 'CSV', FIRSTROW = 2, FIELDTERMINATOR = ',', ROWTERMINATOR = '0x0a', TABLOCK);",
            DATABASE,
        )


def _verify(tables: dict[str, dict[str, str]]) -> None:
    """Row counts and the sales total must equal the CSVs that DuckDB is built from."""
    for table in tables:
        with (HERE / "data" / f"{table}.csv").open(encoding="utf-8", newline="") as f:
            want = sum(1 for _ in csv.DictReader(f))
        got = int(_sqlcmd(f"SELECT COUNT(*) FROM dbo.[{table}]", DATABASE))
        if got != want:
            raise SystemExit(f"dbo.{table}: {got} rows loaded, the CSV has {want}")
        print(f"  dbo.{table}: {got} rows")
    with (HERE / "data" / "sales.csv").open(encoding="utf-8", newline="") as f:
        want = sum(Decimal(r["amount"]) for r in csv.DictReader(f))
    got = Decimal(_sqlcmd("SELECT SUM(amount) FROM dbo.[sales]", DATABASE))
    if got != want:
        raise SystemExit(f"SUM(sales.amount) is {got}, the CSV gives {want}")
    print(f"  SUM(sales.amount) = {got}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--reset", action="store_true", help="remove and recreate the container")
    args = parser.parse_args()
    if _docker("info", check=False).returncode != 0:
        raise SystemExit("Docker is not running: start Docker Desktop and try again.")
    _password()
    tables = _tables()
    _start(args.reset)
    _load(tables)
    print(f"Loaded {DATABASE} in {CONTAINER}:")
    _verify(tables)
    print(
        "\nConnect from Power BI Desktop (Get data > SQL Server) or Tableau (Connect > Microsoft SQL Server):\n"
        f"  server: localhost   database: {DATABASE}   user: sa   password: MSSQL_SA_PASSWORD in .env\n"
        "Type the server as just `localhost` so it matches these config/sources.yaml mappings:\n"
        + "".join(f'  "localhost.{DATABASE}.dbo.{t}": analytics.{t}\n' for t in tables)
    )


if __name__ == "__main__":
    main()
