import json
import shutil
import subprocess
import threading
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from cortino.api.server import create_app
from cortino.building.worktree import create_worktree, slugify
from cortino.config import load
from cortino.orchestrator.run_store import RunStore
from cortino.orchestrator.runner import Runner, StageResult
from cortino.orchestrator.states import RunState as S

HOUR = 3600


def go(nxt):
    return lambda ctx: StageResult(nxt)


def build(ctx):
    meta = ctx.store.get(ctx.run.id).meta
    create_worktree(
        Path(meta["target_repo"]), ctx.path, slugify(meta["name"]), ctx.run.id
    )
    return StageResult(S.INTEGRATING)


def fake_stages(**override):
    stages = {
        S.INTAKE: go(S.PARSED),
        S.PARSED: go(S.PLANNED),
        S.PLANNED: go(S.AWAITING_PLAN_APPROVAL),
        S.BUILDING: build,
        S.INTEGRATING: go(S.VERIFYING),
        S.VERIFYING: go(S.AWAITING_PREVIEW_APPROVAL),
        S.AWAITING_PREVIEW_APPROVAL: go(S.PR_OPEN),
        S.CHANGES_REQUESTED: go(S.PLANNED),
    }
    return {**stages, **override}


@pytest.fixture
def tmp_git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "target"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

    git("init", "-b", "main")
    git("config", "user.name", "Test")
    git("config", "user.email", "test@example.com")
    (repo / "README.md").write_text("# target\n")
    git("add", "-A")
    git("commit", "-m", "init")
    return repo


class Api:
    def __init__(self, tmp_path, repo, stages):
        self.repo = repo
        self.store = RunStore(tmp_path / "runs")
        self.now = [1000.0]
        self.built: list[str] = []
        self.stopped: list[str] = []
        config = {**load("thresholds"), "max_upload_mb": 1, "preview_ttl_hours": 8}
        self.app = create_app(
            self.store,
            lambda: Runner(self.store, stages, config),
            tmp_path / "uploads",
            config=config,
            preview_factory=self.preview_factory,
            clock=lambda: self.now[0],
        )
        self.client = TestClient(
            self.app, base_url="http://127.0.0.1:8000", headers={"X-Cortino": "1"}
        )

    def preview_factory(self, run, run_dir, note):
        self.built.append(run.id)
        app = FastAPI()

        @app.get("/{path:path}", response_class=PlainTextResponse)
        def page(path: str) -> str:
            return f"preview of {run.id} /{path}"

        return app, lambda: self.stopped.append(run.id)

    def upload(self, filename="sales.zip", content=b"PK\x03\x04data", refs=(), expected=(), **form):
        data = {
            "name": "Sales Overview",
            "target_repo": str(self.repo),
            "notes": "n",
            **form,
        }
        files = [("file", (filename, content))] + [("references", r) for r in refs]
        files += [("expected", e) for e in expected]
        return self.client.post("/runs", data=data, files=files)

    def wait(self, rid):
        self.app.state.workers.join(rid, timeout=10)
        return self.store.get(rid).state

    def to_plan_gate(self):
        r = self.upload()
        assert r.status_code == 201, r.text
        rid = r.json()["id"]
        assert self.wait(rid) == S.AWAITING_PLAN_APPROVAL
        return rid

    def to_preview_gate(self):
        rid = self.to_plan_gate()
        assert self.client.post(f"/runs/{rid}/approve").status_code == 202
        assert self.wait(rid) == S.AWAITING_PREVIEW_APPROVAL
        return rid


@pytest.fixture
def api(tmp_path, tmp_git_repo):
    a = Api(tmp_path, tmp_git_repo, fake_stages())
    yield a
    for run in a.store.list():  # no worker may outlive the store
        a.app.state.workers.join(run.id, timeout=10)
    a.store.close()


def test_upload_creates_run(api):
    r = api.upload(refs=[("ref.png", b"\x89PNG")])
    assert r.status_code == 201
    body = r.json()
    rid = body["id"]
    assert body["state"] == "CREATED"
    assert api.wait(rid) == S.AWAITING_PLAN_APPROVAL
    meta = api.store.get(rid).meta
    upload = Path(meta["upload"])
    assert upload.read_bytes() == b"PK\x03\x04data"
    assert upload.is_relative_to(api.store.path(rid) / "input")
    assert [Path(p).read_bytes() for p in meta["references"]] == [b"\x89PNG"]
    assert all(
        Path(p).is_relative_to(api.store.path(rid) / "input")
        for p in meta["references"]
    )
    assert meta["name"] == "Sales Overview"
    assert meta["target_repo"] == str(api.repo)
    assert meta["notes"] == "n"
    assert meta["input_name"] == "sales.zip"
    listed = api.client.get("/runs").json()
    assert [(x["id"], x["state"]) for x in listed] == [(rid, "AWAITING_PLAN_APPROVAL")]
    detail = api.client.get(f"/runs/{rid}").json()
    assert detail["state"] == "AWAITING_PLAN_APPROVAL"
    assert detail["meta"]["name"] == "Sales Overview"
    assert detail["plan_md"] is None


@pytest.mark.parametrize(
    "name",
    [
        "../evil.zip",
        "..\\..\\evil.zip",
        "/etc/abs.zip",
        "C:/abs.zip",
        "..",
        "a/../b.zip",
        "...",
        "x" * 201 + ".zip",
        "CON",
        "nul.zip",
        "com1.txt",
        "LPT9",
    ],
)
def test_upload_filename_traversal_rejected(api, name):
    r = api.upload(filename=name)
    assert r.status_code == 400, r.text
    r = api.upload(refs=[(name, b"x")])
    assert r.status_code == 400, r.text
    r = api.upload(expected=[(name, b"x")])
    assert r.status_code == 400, r.text
    assert api.store.list() == []


def test_upload_expected_csvs_land_in_the_run(api):
    r = api.upload(expected=[("barSalesByRegion.csv", b"region,total_sales\nEast,1\n")])
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    api.wait(rid)
    assert (api.store.path(rid) / "expected" / "barSalesByRegion.csv").read_bytes() == b"region,total_sales\nEast,1\n"
    assert not (api.store.path(rid) / "input" / "upload" / "expected").exists()
    for bad in ([("notes.txt", b"x")], [("a.csv", b"x"), ("A.csv", b"y")]):
        assert api.upload(expected=bad).status_code == 400
    assert api.upload(filename="expected").status_code == 400
    assert len(api.store.list()) == 1


def test_main_file_named_references_rejected(api):
    assert api.upload(filename="references").status_code == 400
    assert api.upload(filename="References").status_code == 400
    assert api.store.list() == []


def test_chunked_oversize_upload_413(api):
    boundary = "cortinoboundary"
    head = (
        f"--{boundary}\r\n"
        'Content-Disposition: form-data; name="file"; filename="big.zip"\r\n\r\n'
    ).encode()

    def body():
        yield head
        for _ in range(3):
            yield b"x" * (512 * 1024)
        yield f"\r\n--{boundary}--\r\n".encode()

    r = api.client.post(
        "/runs",
        content=body(),
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
    )
    assert "content-length" not in {k.lower() for k in r.request.headers}
    assert r.status_code == 413
    assert api.store.list() == []


def test_oversize_upload_413(api):
    r = api.upload(content=b"x" * (1024 * 1024 + 1))
    assert r.status_code == 413
    assert api.store.list() == []


def test_upload_requires_name_and_existing_repo(api, tmp_path):
    assert api.upload(name="").status_code == 422
    assert api.upload(target_repo=str(tmp_path / "missing")).status_code == 422
    assert api.store.list() == []


def test_approve_only_valid_at_gate(api):
    created = api.store.create("x.zip", {"name": "x", "target_repo": str(api.repo)}).id
    r = api.client.post(f"/runs/{created}/approve")
    assert r.status_code == 409
    assert "CREATED" in r.json()["detail"]
    rid = api.to_preview_gate()
    assert api.client.post(f"/runs/{rid}/approve").status_code == 202
    assert api.wait(rid) == S.PR_OPEN
    assert api.client.post(f"/runs/{rid}/approve").status_code == 409
    assert api.client.post(f"/runs/{rid}/reject").status_code == 409


def test_request_changes_records_text(api):
    rid = api.to_plan_gate()
    assert (
        api.client.post(f"/runs/{rid}/request-changes", json={"text": "x"}).status_code
        == 409
    )
    assert api.client.post(f"/runs/{rid}/approve").status_code == 202
    assert api.wait(rid) == S.AWAITING_PREVIEW_APPROVAL
    for bad in ({}, {"text": ""}, {"text": "   "}, {"text": "x" * 5001}):
        assert (
            api.client.post(f"/runs/{rid}/request-changes", json=bad).status_code == 422
        )
    r = api.client.post(
        f"/runs/{rid}/request-changes",
        json={"text": "make the bars blue", "visual_id": "v1"},
    )
    assert r.status_code == 202
    assert api.wait(rid) == S.AWAITING_PLAN_APPROVAL
    lines = (
        (api.store.path(rid) / "change_requests.jsonl")
        .read_text(encoding="utf-8")
        .splitlines()
    )
    record = json.loads(lines[-1])
    assert (record["text"], record["visual_id"]) == ("make the bars blue", "v1")


def _branches(repo):
    args = ["branch", "--list", "--format=%(refname:short)", "cortino/*"]
    out = subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )
    return out.stdout.strip()


def test_reject_removes_branch(api):
    rid = api.to_preview_gate()
    wt = api.store.path(rid) / "worktree"
    assert wt.is_dir()
    assert _branches(api.repo) == f"cortino/sales-overview-{rid[:8]}"
    api.client.get(f"/runs/{rid}/preview", follow_redirects=False)
    assert api.client.post(f"/runs/{rid}/reject").status_code == 202
    assert api.wait(rid) == S.REJECTED
    assert not wt.exists()
    assert _branches(api.repo) == ""
    assert api.stopped == [rid]


def test_reject_deletes_branch_when_worktree_is_gone(api):
    rid = api.to_preview_gate()
    wt = api.store.path(rid) / "worktree"
    shutil.rmtree(wt)
    assert api.client.post(f"/runs/{rid}/reject").status_code == 202
    assert api.wait(rid) == S.REJECTED
    assert _branches(api.repo) == ""


def test_foreign_host_rejected(api):
    r = api.client.get("/runs", headers={"Host": "evil.example:8000"})
    assert r.status_code == 400
    rid = api.to_plan_gate()
    r = api.client.post(f"/runs/{rid}/approve", headers={"Host": "evil.example:8000"})
    assert r.status_code == 400
    assert api.store.get(rid).state == S.AWAITING_PLAN_APPROVAL


def test_post_without_cortino_header_rejected(api):
    rid = api.to_plan_gate()
    bare = TestClient(api.app, base_url="http://127.0.0.1:8000")
    r = bare.post(
        f"/runs/{rid}/approve",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    assert r.status_code == 403
    files = [("file", ("sales.zip", b"PK"))]
    data = {"name": "x", "target_repo": str(api.repo)}
    assert bare.post("/runs", data=data, files=files).status_code == 403
    assert api.store.get(rid).state == S.AWAITING_PLAN_APPROVAL
    assert len(api.store.list()) == 1


def test_cross_origin_post_rejected(api):
    rid = api.to_plan_gate()
    r = api.client.post(
        f"/runs/{rid}/reject", headers={"Origin": "http://evil.example"}
    )
    assert r.status_code == 403
    assert api.store.get(rid).state == S.AWAITING_PLAN_APPROVAL
    same = api.client.post(
        f"/runs/{rid}/approve", headers={"Origin": "http://127.0.0.1:8000"}
    )
    assert same.status_code == 202
    assert api.wait(rid) == S.AWAITING_PREVIEW_APPROVAL


def test_extra_allowed_host(tmp_path, tmp_git_repo):
    store = RunStore(tmp_path / "runs")
    try:
        app = create_app(
            store,
            lambda: Runner(store, fake_stages()),
            tmp_path / "uploads",
            allowed_hosts=["10.0.0.5"],
        )
        client = TestClient(app, base_url="http://10.0.0.5:8000")
        assert client.get("/runs").status_code == 200
        assert TestClient(app).get("/runs").status_code == 400  # "testserver"
    finally:
        store.close()


def test_unknown_run_404(api):
    c = api.client
    assert c.get("/runs/nope").status_code == 404
    assert c.get("/runs/nope/events").status_code == 404
    assert c.get("/runs/nope/preview").status_code == 404
    assert c.post("/runs/nope/approve").status_code == 404
    assert c.post("/runs/nope/reject").status_code == 404
    assert c.post("/runs/nope/request-changes", json={"text": "x"}).status_code == 404


def _sse(text):
    events = []
    for block in text.strip().split("\n\n"):
        fields = dict(
            line.split(": ", 1)
            for line in block.splitlines()
            if not line.startswith(":")
        )
        if fields:
            events.append(fields)
    return events


def test_events_stream_in_order_and_ends_at_gate(api):
    rid = api.to_plan_gate()
    r = api.client.get(f"/runs/{rid}/events")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/event-stream")
    events = _sse(r.text)
    assert events[-1] == {"event": "end", "data": "AWAITING_PLAN_APPROVAL"}
    data = [e for e in events if "id" in e]
    assert [int(e["id"]) for e in data] == list(range(1, len(data) + 1))
    moves = [(d["from"], d["to"]) for d in (json.loads(e["data"]) for e in data)
             if d["kind"] == "transition"]  # fmt: skip
    assert moves == [("CREATED", "INTAKE"), ("INTAKE", "PARSED"), ("PARSED", "PLANNED"),
                     ("PLANNED", "AWAITING_PLAN_APPROVAL")]  # fmt: skip
    later = _sse(api.client.get(f"/runs/{rid}/events?after=2").text)
    assert [int(e["id"]) for e in later if "id" in e] == list(range(3, len(data) + 1))


def test_preview_redirect_starts_server_and_reuses_it(api):
    rid = api.to_plan_gate()
    assert (
        api.client.get(f"/runs/{rid}/preview", follow_redirects=False).status_code
        == 409
    )
    assert api.client.post(f"/runs/{rid}/approve").status_code == 202
    assert api.wait(rid) == S.AWAITING_PREVIEW_APPROVAL
    with api.client:
        r = api.client.get(f"/runs/{rid}/preview", follow_redirects=False)
        assert r.status_code == 307
        url = r.headers["location"]
        assert url.startswith("http://127.0.0.1:")
        assert httpx.get(url).text == f"preview of {rid} /"
        again = api.client.get(f"/runs/{rid}/preview", follow_redirects=False)
        assert again.headers["location"] == url
        compare = api.client.get(
            f"/runs/{rid}/preview?page=compare", follow_redirects=False
        )
        assert compare.headers["location"] == url + "compare"
        assert api.built == [rid]
    assert api.stopped == [rid]  # shutdown stops every preview


def test_preview_ttl_expiry_stops_and_rebuilds(api):
    rid = api.to_preview_gate()
    url = api.client.get(f"/runs/{rid}/preview", follow_redirects=False).headers[
        "location"
    ]
    api.now[0] += 8 * HOUR - 1
    api.client.get("/runs")
    assert api.stopped == []
    api.now[0] += 2
    api.client.get("/runs")
    assert api.stopped == [rid]
    with pytest.raises(httpx.TransportError):
        httpx.get(url, timeout=2)
    r = api.client.get(f"/runs/{rid}/preview", follow_redirects=False)
    assert r.status_code == 307
    assert api.built == [rid, rid]
    assert httpx.get(r.headers["location"]).text == f"preview of {rid} /"
    api.app.state.previews.stop_all()


def test_no_two_concurrent_advances(tmp_path, tmp_git_repo):
    release, entered = threading.Event(), threading.Event()
    active, peak = [0], [0]

    def slow_intake(ctx):
        active[0] += 1
        peak[0] = max(peak[0], active[0])
        entered.set()
        release.wait(10)
        active[0] -= 1
        return StageResult(S.PARSED)

    api = Api(tmp_path, tmp_git_repo, fake_stages(**{S.INTAKE: slow_intake}))
    try:
        rid = api.upload().json()["id"]
        assert entered.wait(10)
        r = api.client.post(f"/runs/{rid}/reject")
        assert r.status_code == 409
        assert "busy" in r.json()["detail"]
        assert api.client.get(f"/runs/{rid}").json()["busy"] is True
        release.set()
        assert api.wait(rid) == S.AWAITING_PLAN_APPROVAL
        assert peak[0] == 1
    finally:
        release.set()
        api.store.close()


def test_run_detail_includes_plan_verify_and_pr(api):
    rid = api.to_preview_gate()
    path = api.store.path(rid)
    (path / "plan.md").write_text("# Plan\n<script>x</script>", encoding="utf-8")
    (path / "verify").mkdir()
    report = {"passed": True, "checks": [
        {"name": "data_values", "passed": True, "findings": [{"message": "m"}]},
        {"name": "visual", "passed": False, "skipped": True, "findings": []}]}  # fmt: skip
    (path / "verify" / "report.json").write_text(json.dumps(report), encoding="utf-8")
    (path / "pr.json").write_text(
        json.dumps({"url": "https://x/pr/1", "number": 1}), encoding="utf-8"
    )
    api.store.set_meta(rid, api_token="t0ps3cret")
    d = api.client.get(f"/runs/{rid}").json()
    assert d["plan_md"] == "# Plan\n<script>x</script>"
    assert d["verify"] == {"passed": True, "checks": [
        {"name": "data_values", "passed": True, "skipped": False},
        {"name": "visual", "passed": False, "skipped": True}]}  # fmt: skip
    assert d["pr"]["url"] == "https://x/pr/1"
    assert "api_token" not in d["meta"]


def test_index_page_served(api):
    r = api.client.get("/")
    assert r.status_code == 200
    assert "EventSource" in r.text
    assert "innerHTML" not in r.text
    assert 'headers["X-Cortino"] = "1"' in r.text


def test_build_preview_serves_the_built_dashboard(tmp_path, spec, plan, warehouse):
    from cortino.api.server import build_preview
    from cortino.building.data_builder import build_data
    from cortino.building.ui_builder import build_ui
    from cortino.llm import FakeLLM
    from cortino.tools.repo_tools import RepoTools
    from cortino.tools.warehouse import DuckDBWarehouse

    store = RunStore(tmp_path / "runs")
    source = DuckDBWarehouse(warehouse, read_only=True)
    try:
        run = store.create("x.zip", {"name": "Sales", "target_repo": str(tmp_path)})
        run_dir = store.path(run.id)
        (run_dir / "spec.json").write_text(spec.model_dump_json(), encoding="utf-8")
        (run_dir / "plan.json").write_text(plan.model_dump_json(), encoding="utf-8")
        tools = RepoTools(run_dir / "worktree")  # an existing worktree is reused as-is
        build_data(plan, spec, tools, FakeLLM({}), None)
        charts = [
            {"id": m.visual_id, "title": m.visual_id, "number_format": None}
            for m in plan.visual_map
        ]
        build_ui(plan, spec, tools, FakeLLM({"builder": [{"charts": charts}]}), None)
        notes = []
        app, cleanup = build_preview(run, run_dir, source, notes.append)
        try:
            client = TestClient(app, base_url="http://127.0.0.1")
            cfg = client.get("/config").json()
            dataset = cfg["datasets"][0]["id"]
            r = client.get(f"/datasets/{dataset}")
            assert r.status_code == 200, r.text
            assert r.json()
            assert (run_dir / "preview_db").is_dir()
        finally:
            cleanup()
        assert not (run_dir / "preview_db").exists()
        assert notes == []
    finally:
        source.close()
        store.close()


def test_resume_route(tmp_path, tmp_git_repo):
    outcomes = iter([S.BUILDING] * 4 + [S.AWAITING_PREVIEW_APPROVAL])
    api = Api(tmp_path, tmp_git_repo, fake_stages(**{S.VERIFYING: lambda ctx: StageResult(next(outcomes))}))
    try:
        rid = api.to_plan_gate()
        assert api.client.post(f"/runs/{rid}/resume").status_code == 409  # at a gate: approve instead
        assert api.client.post(f"/runs/{rid}/approve").status_code == 202
        assert api.wait(rid) == S.NEEDS_INPUT
        bare = TestClient(api.app, base_url="http://127.0.0.1:8000")
        assert bare.post(f"/runs/{rid}/resume").status_code == 403
        r = api.client.post(f"/runs/{rid}/resume", headers={"Origin": "http://evil.example"})
        assert r.status_code == 403
        r = api.client.post(f"/runs/{rid}/resume", headers={"Host": "evil.example:8000"})
        assert r.status_code == 400
        assert api.store.get(rid).state == S.NEEDS_INPUT
        assert api.client.post(f"/runs/{rid}/resume").status_code == 202
        assert api.wait(rid) == S.AWAITING_PREVIEW_APPROVAL
        assert api.client.post("/runs/nope/resume").status_code == 404
    finally:
        for run in api.store.list():
            api.app.state.workers.join(run.id, timeout=10)
        api.store.close()


def test_index_page_has_resume_button(api):
    text = api.client.get("/").text
    assert 'id="resume"' in text and 'act("resume")' in text
