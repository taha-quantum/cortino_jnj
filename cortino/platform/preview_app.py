"""The reference platform's preview: serves a built dashboard and runs its datasets."""

import html
import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cortino import config as cortino_config
from cortino.checking.functional import pick_values
from cortino.plan.models import Plan
from cortino.platform.config_schema import DashboardConfig
from cortino.tools.warehouse import Warehouse, WarehouseError

STATIC = Path(__file__).resolve().parent / "static"
IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg"}


def image_files(folder: Path | None) -> list[Path]:
    """Image files directly inside `folder` (never below it)."""
    if folder is None or not folder.is_dir():
        return []
    return sorted(p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES)


def _image(folder: Path | None, name: str) -> FileResponse:
    match = next((p for p in image_files(folder) if p.name == name), None)
    if match is None:
        raise HTTPException(404, "no such image")
    return FileResponse(match)


def _summary(report: Path | None) -> str:
    if report is None or not report.is_file():
        return "<p>No verify report yet.</p>"
    data = json.loads(report.read_text(encoding="utf-8"))
    items = "".join(
        f"<li>{html.escape(str(c.get('name')))}: {'passed' if c.get('passed') else 'failed'}"
        + "".join(f"<br>- {html.escape(str(f.get('message')))}" for f in c.get("findings", []))
        + "</li>"
        for c in data.get("checks", [])
    )
    return f"<p>Verify: {'passed' if data.get('passed') else 'failed'}</p><ul>{items}</ul>"


def create_preview_app(
    dashboard_dir: Path,
    warehouse: Warehouse,
    reference_dir: Path | None = None,
    verify_report: Path | None = None,
    plan: Plan | None = None,
) -> FastAPI:
    """The preview of the dashboard in `dashboard_dir`. `warehouse` must be a throwaway copy with
    the dashboard's views.sql already applied; datasets run on it with bound parameters only.
    Live screenshots for /compare are read from `verify_report`'s sibling `screenshots/` folder.
    Filter options are the values of the column `plan` maps the filter field to (the values the
    functional check picks); without a mapping, the field's values in the target datasets."""
    dashboard_dir = Path(dashboard_dir)
    config = DashboardConfig.model_validate_json((dashboard_dir / "dashboard.json").read_text(encoding="utf-8"))
    datasets = {d.id: d for d in config.datasets}
    filters = {f.id: f for f in config.filters}
    charts = {c.id: c for p in config.pages for c in p.charts}
    limit = cortino_config.load("thresholds")["readonly_sql_limit"]
    screenshots = verify_report.parent / "screenshots" if verify_report else None
    app = FastAPI(title=f"Preview: {config.title}")
    # Loopback names only: a rebound DNS name must not read warehouse rows.
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost", "[::1]"])

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/render.js")
    def render_js() -> FileResponse:
        return FileResponse(STATIC / "render.js", media_type="text/javascript")

    @app.get("/config")
    def get_config() -> dict:
        return config.model_dump()

    @app.get("/datasets/{dataset_id}")
    def run_dataset(dataset_id: str, request: Request) -> JSONResponse:
        ds = datasets.get(dataset_id)
        if ds is None:
            raise HTTPException(404, f"unknown dataset {dataset_id!r}")
        declared = [p.name for p in ds.params]
        unknown = sorted(set(request.query_params) - set(declared))
        if unknown:
            raise HTTPException(400, f"unknown parameters {unknown} for dataset {dataset_id!r}")
        params = {name: request.query_params.get(name) for name in declared}
        return JSONResponse(jsonable_encoder(_rows(ds, params)))  # decimals as numbers, dates as ISO strings

    def _rows(ds, params: dict) -> list[dict]:
        path = (dashboard_dir / ds.file).resolve()
        if path.parent != (dashboard_dir / "datasets").resolve():
            raise HTTPException(404, f"unknown dataset {ds.id!r}")
        return warehouse.query(path.read_text(encoding="utf-8"), params, limit=limit)

    @app.get("/filters/{filter_id}/options")
    def filter_options(filter_id: str) -> list[str]:
        f = filters.get(filter_id)
        if f is None:
            raise HTTPException(404, f"unknown filter {filter_id!r}")
        if plan is not None and any(m.target_column == f.field for m in plan.column_map):
            return pick_values(warehouse, plan, f.field, limit=limit)
        targets = charts if f.applies_to == "all" else f.applies_to
        found = set()
        for d in {charts[c].dataset for c in targets}:
            try:  # every declared param unbound (NULL), as /datasets does; a failing dataset adds nothing
                rows = _rows(datasets[d], {p.name: None for p in datasets[d].params})
            except WarehouseError:
                continue
            found |= {str(r[f.field]) for r in rows if r.get(f.field) is not None}
        return sorted(found)

    @app.get("/compare", response_class=HTMLResponse)
    def compare() -> str:
        def gallery(kind: str, folder: Path | None, empty: str) -> str:
            imgs = "".join(
                f'<figure><img src="/compare/{kind}/{html.escape(p.name)}" alt="">'
                f"<figcaption>{html.escape(p.name)}</figcaption></figure>"
                for p in image_files(folder)
            )
            return imgs or f"<p>{empty}</p>"

        return (
            f"<!doctype html><html><head><meta charset='utf-8'><title>Compare</title>"
            "<style>body{font-family:sans-serif;margin:16px}.side{display:flex;gap:16px}"
            ".side>section{flex:1}img{max-width:100%;border:1px solid #ccc}</style></head><body>"
            f"<h1>{html.escape(config.title)}</h1>{_summary(verify_report)}<div class='side'>"
            f"<section><h2>Reference</h2>{gallery('reference', reference_dir, 'No reference images.')}</section>"
            f"<section><h2>Rebuilt</h2>{gallery('screenshot', screenshots, 'No screenshots yet.')}</section>"
            "</div></body></html>"
        )

    @app.get("/compare/reference/{name}")
    def reference_image(name: str) -> FileResponse:
        return _image(reference_dir, name)

    @app.get("/compare/screenshot/{name}")
    def screenshot_image(name: str) -> FileResponse:
        return _image(screenshots, name)

    return app
