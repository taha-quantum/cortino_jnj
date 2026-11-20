"""The Cortino API and web UI: upload, progress, the plan and preview gates. It drives the Runner;
every Runner call runs in one background worker thread per run."""

import asyncio
import json
import re
import shutil
import threading
import time
import uuid
from collections.abc import Callable
from contextlib import ExitStack, asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    UploadFile,
)
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse
from pydantic import BaseModel, StringConstraints
from starlette.middleware.trustedhost import TrustedHostMiddleware

from cortino import config as cortino_config
from cortino.building.datasets import dashboard_dir
from cortino.building.worktree import (
    create_worktree,
    remove_worktree,
    run_worktree,
    slugify,
)
from cortino.orchestrator.run_store import Run, RunStore
from cortino.orchestrator.runner import GATES, Runner, StageError, WrongState
from cortino.orchestrator.stages import StageDeps, real_deps
from cortino.orchestrator.states import END_STATES, RunState
from cortino.plan.models import Plan
from cortino.platform.preview_app import create_preview_app
from cortino.platform.serving import Note, serve_app, throwaway_warehouse_with_views
from cortino.spec.models import load_spec
from cortino.tools.warehouse import Warehouse

S = RunState
STATIC = Path(__file__).resolve().parent / "static"
STOPS = GATES | END_STATES | {S.NEEDS_INPUT}
SECRET_KEY = re.compile(r"secret|token|password|credential", re.IGNORECASE)
RESERVED = re.compile(r"^(con|prn|aux|nul|com[1-9]|lpt[1-9])$", re.IGNORECASE)
LOOPBACK_HOSTS = ["127.0.0.1", "localhost", "[::1]"]
POLL_S, KEEPALIVE_S = 0.25, 15.0

PreviewFactory = Callable[[Run, Path, Note], tuple[FastAPI, Callable[[], None]]]


class Busy(Exception):
    """Another action is still running for this run."""


class Workers:
    """One background thread per run at a time, so a run never has two concurrent advances."""

    def __init__(self, store: RunStore):
        self.store = store
        self._guard = threading.Lock()
        self._locks: dict[str, threading.Lock] = {}
        self._threads: dict[str, threading.Thread] = {}

    def _lock(self, run_id: str) -> threading.Lock:
        with self._guard:
            return self._locks.setdefault(run_id, threading.Lock())

    def busy(self, run_id: str) -> bool:
        return self._lock(run_id).locked()

    def submit(
        self, run_id: str, check: Callable[[], None], work: Callable[[], object]
    ) -> None:
        """Run `check` now (it raises WrongState to refuse) and then `work` in the background."""
        lock = self._lock(run_id)
        if not lock.acquire(blocking=False):
            raise Busy(
                f"run {run_id} is busy; try again when the current step finishes"
            )
        try:
            check()
        except BaseException:
            lock.release()
            raise

        def run() -> None:
            try:
                work()
            except Exception as e:  # noqa: BLE001  recorded for the UI; the runner already records stage failures
                self.store.event(
                    run_id, "api_action_failed", error=f"{type(e).__name__}: {e}"
                )
            finally:
                lock.release()

        thread = threading.Thread(
            target=run, daemon=True, name=f"cortino-run-{run_id[:8]}"
        )
        self._threads[run_id] = thread
        thread.start()

    def join(self, run_id: str, timeout: float | None = None) -> None:
        if thread := self._threads.get(run_id):
            thread.join(timeout)


@dataclass
class _Preview:
    url: str
    started: float
    stack: ExitStack


class Previews:
    """Preview apps by run, each on a free 127.0.0.1 port; stopped after `ttl_s`."""

    def __init__(
        self,
        store: RunStore,
        factory: PreviewFactory,
        ttl_s: float,
        clock: Callable[[], float],
    ):
        self.store, self.factory, self.ttl_s, self.clock = store, factory, ttl_s, clock
        self._lock = (
            threading.Lock()
        )  # guards the dicts only, never held while building
        self._building: dict[str, threading.Lock] = {}
        self._running: dict[str, _Preview] = {}

    def url(self, run: Run) -> str:
        with self._lock:
            building = self._building.setdefault(run.id, threading.Lock())
        with building:  # only requests for this run wait while its preview is built
            with self._lock:
                if preview := self._running.get(run.id):
                    return preview.url

            def note(problem: str) -> None:
                self.store.event(run.id, "preview_teardown", problem=problem)

            stack = ExitStack()
            try:
                app, cleanup = self.factory(run, self.store.path(run.id), note)
                stack.callback(cleanup)
                url = stack.enter_context(serve_app(app, note))
            except BaseException:
                stack.close()
                raise
            with self._lock:
                self._running[run.id] = _Preview(url, self.clock(), stack)
            return url

    def stop(self, run_id: str) -> None:
        with self._lock:
            preview = self._running.pop(run_id, None)
        if preview:
            preview.stack.close()

    def expire(self) -> None:
        now = self.clock()
        with self._lock:
            old = [i for i, p in self._running.items() if now - p.started > self.ttl_s]
        for run_id in old:
            self.stop(run_id)

    def stop_all(self) -> None:
        with self._lock:
            running = list(self._running)
        for run_id in running:
            self.stop(run_id)


def build_preview(
    run: Run, run_dir: Path, warehouse: Warehouse, note: Note
) -> tuple[FastAPI, Callable[[], None]]:
    """The preview app for a run, rebuilt from its branch (the commit verify passed) on a fresh
    throwaway copy of `warehouse` with views.sql applied, as run_verify does."""
    meta = run.meta
    spec = load_spec(run_dir / "spec.json")
    plan = Plan.model_validate_json((run_dir / "plan.json").read_text(encoding="utf-8"))
    wt = create_worktree(
        Path(meta["target_repo"]), run_dir, slugify(meta["name"]), run.id
    )
    dash = wt.path / dashboard_dir(spec)
    db, cleanup = throwaway_warehouse_with_views(
        warehouse, dash, run_dir / "preview_db", note
    )
    try:
        app = create_preview_app(
            dash,
            db,
            run_dir / "input" / "references",
            run_dir / "verify" / "report.json",
            plan,
        )
    except BaseException:
        cleanup()
        raise
    return app, cleanup


def _deps_preview(deps: StageDeps) -> PreviewFactory:
    return lambda run, run_dir, note: build_preview(run, run_dir, deps.catalog.warehouse, note)


def _stage_deps_preview() -> PreviewFactory:
    """build_preview on the source warehouse of the stages' own deps (config/sources.yaml)."""
    deps: list[StageDeps] = []
    lock = threading.Lock()

    def factory(run: Run, run_dir: Path, note: Note):
        with lock:
            if not deps:
                deps.append(real_deps())
        return build_preview(run, run_dir, deps[0].catalog.warehouse, note)

    return factory


def safe_name(filename: str | None) -> str:
    """The basename of a client filename; 400 for empty, absolute, `..`, dots-only, over 200
    characters or a Windows device name (CON, NUL, COM1...)."""
    name = (filename or "").replace("\\", "/")
    parts = name.split("/")
    if (
        not name.strip()
        or name.startswith("/")
        or re.match(r"^[A-Za-z]:", name)
        or ".." in parts
    ):
        raise HTTPException(400, f"unsafe file name {filename!r}")
    base = re.sub(r"[^A-Za-z0-9._ -]", "_", parts[-1]).strip()
    if (
        not base.strip(".")
        or len(base) > 200
        or RESERVED.match(base.split(".")[0].strip())
    ):
        raise HTTPException(400, f"unsafe file name {filename!r}")
    return base


async def _reply(send, status: int, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send({"type": "http.response.start", "status": status,
                "headers": [(b"content-type", b"application/json")]})  # fmt: skip
    await send({"type": "http.response.body", "body": body})


class _PostGuard:
    """Every POST needs the `X-Cortino: 1` header (a cross-site form cannot send it) and, when
    the browser sends Origin, this server's own origin: 403 otherwise. A POST /runs body over
    `limit` bytes is 413, checked while it streams in."""

    def __init__(self, app, limit: int):
        self.app, self.limit = app, limit

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["method"] != "POST":
            return await self.app(scope, receive, send)
        headers = dict(scope["headers"])
        if headers.get(b"x-cortino") != b"1":
            return await _reply(send, 403, "missing X-Cortino header")
        origin = headers.get(b"origin")
        own = f"{scope['scheme']}://{headers.get(b'host', b'').decode('latin-1')}"
        if origin is not None and origin.decode("latin-1") != own:
            return await _reply(send, 403, "cross-origin request refused")
        if scope["path"] != "/runs":
            return await self.app(scope, receive, send)
        length = headers.get(b"content-length")
        if length is not None and length.isdigit() and int(length) > self.limit:
            return await _reply(send, 413, "upload is larger than max_upload_mb")
        seen = 0

        async def limited():
            nonlocal seen
            message = await receive()
            seen += len(message.get("body", b""))
            if seen > self.limit:  # FastAPI re-raises HTTPException from body parsing
                raise HTTPException(413, "upload is larger than max_upload_mb")
            return message

        await self.app(scope, limited, send)


Text = Annotated[
    str, StringConstraints(strip_whitespace=True, min_length=1, max_length=5000)
]


class ChangeRequest(BaseModel):
    text: Text
    visual_id: Annotated[str, StringConstraints(max_length=200)] | None = None


def _read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8")) if path.is_file() else None


def _complete_lines(path: Path) -> list[str]:
    if not path.is_file():
        return []
    text = path.read_text(encoding="utf-8")
    return text.splitlines() if text.endswith("\n") else text.splitlines()[:-1]


def create_app(
    store: RunStore,
    runner_factory: Callable[[], Runner],
    uploads_dir: Path,
    *,
    config: dict | None = None,
    preview_factory: PreviewFactory | None = None,
    clock: Callable[[], float] = time.monotonic,
    allowed_hosts: list[str] | None = None,
    deps: StageDeps | None = None,
) -> FastAPI:
    """`runner_factory` makes a Runner per action. Uploads stream into `uploads_dir`, then move
    into runs/<id>/input/upload/ once the run exists. Requests must name a loopback host or one of
    `allowed_hosts` (DNS rebinding); previews default to build_preview on the warehouse of `deps`
    (else of the stages' own deps). `deps` is closed when the app shuts down."""
    config = config if config is not None else cortino_config.load("thresholds")
    uploads_dir = Path(uploads_dir)
    workers = Workers(store)
    previews = Previews(
        store,
        preview_factory
        or (_deps_preview(deps) if deps else _stage_deps_preview()),
        config["preview_ttl_hours"] * 3600,
        clock,
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        yield
        previews.stop_all()
        if deps:
            deps.close()

    app = FastAPI(
        title="Cortino", lifespan=lifespan, dependencies=[Depends(previews.expire)]
    )
    app.add_middleware(_PostGuard, limit=config["max_upload_mb"] * 1024 * 1024)
    app.add_middleware(
        TrustedHostMiddleware, allowed_hosts=[*LOOPBACK_HOSTS, *(allowed_hosts or [])]
    )  # outermost: checked first
    app.state.workers, app.state.previews = workers, previews

    def get_run(run_id: str) -> Run:
        try:
            return store.get(run_id)
        except KeyError:
            raise HTTPException(404, f"unknown run {run_id!r}") from None

    def require(run_id: str, allowed: set[RunState], action: str) -> Callable[[], None]:
        def check() -> None:
            state = store.get(run_id).state
            if state not in allowed:
                raise WrongState(f"cannot {action} a run in state {state.value}")

        return check

    def submit(
        run_id: str, check: Callable[[], None], work: Callable[[], object]
    ) -> dict:
        get_run(run_id)
        try:
            workers.submit(run_id, check, work)
        except (Busy, WrongState) as e:
            raise HTTPException(409, str(e)) from None
        return {"id": run_id, "accepted": True}

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.post("/runs", status_code=201)
    def create_run(
        file: Annotated[UploadFile, File()],
        name: Annotated[str, Form()],
        target_repo: Annotated[str, Form()],
        notes: Annotated[str, Form()] = "",
        references: Annotated[list[UploadFile], File()] = [],  # noqa: B006  FastAPI copies defaults
        expected: Annotated[list[UploadFile], File()] = [],  # noqa: B006  ground truth: <visual id>.csv
    ) -> dict:
        if not name.strip():
            raise HTTPException(422, "name is required")
        if not Path(target_repo).is_dir():
            raise HTTPException(422, f"target_repo {target_repo!r} is not a directory")
        main = safe_name(file.filename)
        if main.lower() in ("references", "expected"):  # those folders sit beside it
            raise HTTPException(400, f"the dashboard file cannot be named {main!r}")
        refs = [safe_name(r.filename) for r in references]
        if len({r.lower() for r in refs}) != len(refs):
            raise HTTPException(400, "reference file names must be unique")
        truth = [safe_name(e.filename) for e in expected]
        if len({t.lower() for t in truth}) != len(truth) or not all(t.lower().endswith(".csv") for t in truth):
            raise HTTPException(400, "expected files must be uniquely named .csv files")
        staging = uploads_dir / uuid.uuid4().hex
        try:
            (staging / "references").mkdir(parents=True)
            (staging / "expected").mkdir()
            for upload, dest in [(file, staging / main), *((r, staging / "references" / n) for r, n in zip(references, refs, strict=True)),
                                 *((e, staging / "expected" / n) for e, n in zip(expected, truth, strict=True))]:  # fmt: skip
                with open(dest, "wb") as out:
                    shutil.copyfileobj(upload.file, out, 1 << 20)
            run = store.create(
                main, {"name": name, "target_repo": target_repo, "notes": notes}
            )
            folder = store.path(run.id) / "input" / "upload"
            shutil.move(staging / "expected", store.path(run.id) / "expected")
            shutil.move(staging, folder)
        finally:
            shutil.rmtree(staging, ignore_errors=True)
        folder = folder.resolve()
        store.set_meta(
            run.id,
            upload=str(folder / main),
            references=[str(folder / "references" / n) for n in refs],
        )
        workers.submit(run.id, lambda: None, lambda: runner_factory().advance(run.id))
        return {"id": run.id, "state": run.state.value}

    @app.get("/runs")
    def list_runs() -> list[dict]:
        return [
            {"id": r.id, "state": r.state.value, "name": r.meta.get("name", ""),
             "created_at": r.created_at, "updated_at": r.updated_at}
            for r in reversed(store.list())
        ]  # fmt: skip

    @app.get("/runs/{run_id}")
    def run_detail(run_id: str) -> dict:
        run = get_run(run_id)
        path = store.path(run_id)
        plan_md = path / "plan.md"
        report = _read_json(path / "verify" / "report.json")
        verify = report and {
            "passed": report.get("passed"),
            "checks": [{"name": c.get("name"), "passed": bool(c.get("passed")),
                        "skipped": bool(c.get("skipped"))} for c in report.get("checks", [])],
        }  # fmt: skip
        return {
            "id": run.id,
            "state": run.state.value,
            "created_at": run.created_at,
            "updated_at": run.updated_at,
            "verify_loops": run.verify_loops,
            "busy": workers.busy(run_id),
            "meta": {k: v for k, v in run.meta.items() if not SECRET_KEY.search(k)},
            "plan_md": plan_md.read_text(encoding="utf-8")
            if plan_md.is_file()
            else None,
            "verify": verify,
            "pr": _read_json(path / "pr.json"),
        }

    @app.get("/runs/{run_id}/events")
    async def events(
        run_id: str, request: Request, after: Annotated[int, Query(ge=0)] = 0
    ):
        await asyncio.to_thread(get_run, run_id)
        last_id = request.headers.get("last-event-id", "")
        sent = int(last_id) if last_id.isdigit() else after
        log = store.path(run_id) / "events.jsonl"

        async def stream():
            nonlocal sent
            quiet_since = time.monotonic()
            while True:
                # Read the state before the log, so every line written before a stop is sent.
                state = (await asyncio.to_thread(store.get, run_id)).state
                stopped = state in STOPS and not workers.busy(run_id)
                lines = await asyncio.to_thread(_complete_lines, log)
                for i in range(sent, len(lines)):
                    yield f"id: {i + 1}\ndata: {lines[i]}\n\n"
                    quiet_since = time.monotonic()
                sent = max(sent, len(lines))
                if stopped:
                    yield f"event: end\ndata: {state.value}\n\n"
                    return
                if time.monotonic() - quiet_since > KEEPALIVE_S:
                    yield ": keep-alive\n\n"
                    quiet_since = time.monotonic()
                await asyncio.sleep(POLL_S)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache"},
        )

    @app.post("/runs/{run_id}/approve", status_code=202)
    def approve(run_id: str) -> dict:
        def work() -> None:
            previews.stop(run_id)
            runner_factory().approve(run_id)

        return submit(run_id, require(run_id, GATES, "approve"), work)

    @app.post("/runs/{run_id}/request-changes", status_code=202)
    def request_changes(run_id: str, body: ChangeRequest) -> dict:
        def work() -> None:
            previews.stop(run_id)
            runner_factory().request_changes(run_id, body.text, body.visual_id)

        check = require(run_id, {S.AWAITING_PREVIEW_APPROVAL}, "request changes")
        return submit(run_id, check, work)

    @app.post("/runs/{run_id}/reject", status_code=202)
    def reject(run_id: str) -> dict:
        def work() -> None:
            runner_factory().reject(run_id)
            previews.stop(run_id)
            meta = store.get(run_id).meta
            wt = run_worktree(
                Path(meta["target_repo"]),
                store.path(run_id),
                slugify(meta["name"]),
                run_id,
            )
            remove_worktree(
                wt, delete_branch=True
            )  # also when worktree/ is already gone

        return submit(run_id, require(run_id, GATES, "reject"), work)

    @app.post("/runs/{run_id}/resume", status_code=202)
    def resume(run_id: str) -> dict:
        """Continue a run from NEEDS_INPUT, or one a crash left mid-stage; 409 at a gate or end."""
        resumable = set(RunState) - GATES - END_STATES
        check = require(run_id, resumable, "resume")
        return submit(run_id, check, lambda: runner_factory().resume(run_id))

    @app.get("/runs/{run_id}/preview")
    def preview(run_id: str, page: Literal["", "compare"] = "") -> RedirectResponse:
        run = get_run(run_id)
        if run.state != S.AWAITING_PREVIEW_APPROVAL:
            raise HTTPException(409, f"no preview for a run in state {run.state.value}")
        try:
            url = previews.url(run)
        except StageError as e:  # no warehouse configured
            raise HTTPException(503, str(e)) from None
        return RedirectResponse(url + page, status_code=307)

    return app
