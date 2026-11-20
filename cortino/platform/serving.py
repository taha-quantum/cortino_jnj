"""Serving a preview app: the throwaway warehouse it reads and the local server it runs on.
Shared by the verify stage and the API's preview gate."""

import shutil
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

import uvicorn

from cortino.tools.warehouse import DuckDBWarehouse, throwaway_copy

Note = Callable[[str], None]


@contextmanager
def serve_app(app, note: Note) -> Iterator[str]:
    """Serve `app` on a free 127.0.0.1 port in a background thread; yields its url. A server
    that does not stop in time is reported to `note` rather than raised."""
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    server = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 10
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("the preview app did not start")
            time.sleep(0.05)
        yield f"http://127.0.0.1:{port}/"
    finally:
        server.should_exit = True
        thread.join(10)
        if thread.is_alive():
            note(f"the preview app on port {port} did not stop within 10s")


def throwaway_warehouse_with_views(
    warehouse: DuckDBWarehouse, dashboard_dir: Path, db_dir: Path, note: Note
) -> tuple[DuckDBWarehouse, Callable[[], None]]:
    """A writable copy of `warehouse` in `db_dir` with the dashboard's views.sql applied, and the
    cleanup that closes and deletes it. A folder that cannot be deleted is reported to `note`
    (the next copy overwrites it) rather than raised."""
    try:
        views = (Path(dashboard_dir) / "views.sql").read_text(encoding="utf-8")
    except (
        OSError,
        UnicodeDecodeError,
    ):  # as checking.files.read_text: the checks report it
        views = ""
    db = throwaway_copy(warehouse, db_dir)

    def cleanup() -> None:
        db.close()
        try:
            shutil.rmtree(db_dir)
        except OSError as e:  # never replaces the error that ended the caller
            note(f"could not delete {db_dir}: {e}")

    if views.strip():
        try:
            db.query(views)
        except BaseException:
            cleanup()
            raise
    return db, cleanup
