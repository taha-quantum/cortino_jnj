import base64
import json
from pathlib import Path

import httpx
import pytest

from cortino.intake.fetchers.powerbi_service import (
    FetchError,
    execute_dax,
    fetch_powerbi,
    parse_powerbi_url,
)
from cortino.intake.intake import run_intake
from cortino.orchestrator.run_store import RunStore
from cortino.parsers.dispatch import parse

WS = "11111111-1111-1111-1111-111111111111"
REP = "22222222-2222-2222-2222-222222222222"
DS = "33333333-3333-3333-3333-333333333333"
URL = f"https://app.powerbi.com/groups/{WS}/reports/{REP}/ReportSection1"
PBIP = Path(__file__).parent.parent / "fixtures" / "sales" / "pbip"
PNG = b"\x89PNG\r\n\x1a\nfake"


def parts_of(folder: Path) -> list[dict]:
    return [
        {
            "path": p.relative_to(folder).as_posix(),
            "payload": base64.b64encode(p.read_bytes()).decode(),
            "payloadType": "InlineBase64",
        }
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    ]


def make_handler(
    report_parts=None,
    model_parts=None,
    log=None,
    poll_runs=2,
    location="https://api.fabric.microsoft.com/v1/operations/op1",
    retry_after="3",
    meta=None,
):
    report_parts = report_parts if report_parts is not None else parts_of(PBIP / "Sales.Report")
    model_parts = (
        model_parts if model_parts is not None else parts_of(PBIP / "Sales.SemanticModel")
    )
    state = {"polls": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        full = str(req.url)
        url = str(req.url.copy_with(query=None))
        if log is not None:
            log.append((req.method, full))
        assert req.headers["authorization"].startswith("Bearer tok")
        if url.endswith(f"/groups/{WS}/reports/{REP}"):
            if meta is not None:
                return meta
            return httpx.Response(200, json={"name": "Sales", "datasetId": DS})
        if url.endswith("/reports/" + REP + "/getDefinition"):
            return httpx.Response(200, json={"definition": {"parts": report_parts}})
        if "/semanticModels/" in url and url.endswith("getDefinition"):
            return httpx.Response(
                202,
                headers={
                    "Location": location,
                    "Retry-After": retry_after,
                },
            )
        if url.endswith("/operations/op1"):
            state["polls"] += 1
            done = state["polls"] >= poll_runs
            return httpx.Response(200, json={"status": "Succeeded" if done else "Running"})
        if url.endswith("/operations/op1/result"):
            return httpx.Response(200, json={"definition": {"parts": model_parts}})
        if url.endswith("/ExportTo"):
            return httpx.Response(202, json={"id": "ex1"})
        if url.endswith("/exports/ex1"):
            return httpx.Response(200, json={"status": "Succeeded"})
        if url.endswith("/exports/ex1/file"):
            return httpx.Response(200, content=PNG)
        if url.endswith("/executeQueries"):
            return httpx.Response(
                200, json={"results": [{"tables": [{"rows": [{"[Total]": 5}]}]}]}
            )
        return httpx.Response(404)

    return handler


def client_for(handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler))


def test_url_parsed():
    assert parse_powerbi_url(URL) == (WS, REP)
    assert parse_powerbi_url(f"https://app.powerbi.com/groups/{WS}/reports/{REP}") == (WS, REP)


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com/groups/x/reports/y",
        f"https://evil.com/groups/{WS}/reports/{REP}",
        "https://app.powerbi.com/home",
        f"http://app.powerbi.com/groups/{WS}/reports/{REP}",
        "not a url",
    ],
)
def test_bad_url_rejected(url):
    with pytest.raises(FetchError):
        parse_powerbi_url(url)


def test_bad_url_needs_input(tmp_path):
    store = RunStore(tmp_path / "runs")
    run = store.create("x", {})
    res = run_intake(store, run.id, "https://example.com/nope", "", "org/repo", [])
    store.close()
    assert not res.ok and "Power BI report link" in res.needs_input


def test_fetch_auth_failure_needs_input(tmp_path, monkeypatch):
    from cortino.intake import intake as mod

    def boom(*a, **k):
        raise FetchError("Power BI refused access (403)")

    monkeypatch.setattr(mod, "fetch_powerbi", boom)
    store = RunStore(tmp_path / "runs")
    run = store.create("x", {})
    res = run_intake(store, run.id, URL, "", "org/repo", [])
    store.close()
    assert not res.ok and "refused access" in res.needs_input


def test_intake_url_writes_pbip_and_continues(tmp_path, monkeypatch):
    from cortino.intake import intake as mod

    def fake(url, dest, references_dir=None, **kw):
        fetch_powerbi(
            url,
            dest,
            lambda scope: "tok",
            client_for(make_handler()),
            references_dir,
            sleep=lambda s: None,
        )

    monkeypatch.setattr(mod, "fetch_powerbi", fake)
    store = RunStore(tmp_path / "runs")
    run = store.create("x", {})
    res = run_intake(store, run.id, URL, "", "org/repo", [])
    d = store.path(run.id)
    meta = json.loads((d / "intake.json").read_text())
    st = store.get(run.id).meta["source_type"]
    store.close()
    assert res.ok and st == "pbip_zip" and meta["model_present"]
    assert (d / "input" / "references" / "powerbi-page-1.png").read_bytes() == PNG
    assert parse(d / "extracted", "pbip_zip", "Sales").tables


def test_definition_parts_written_as_pbip(tmp_path):
    out = fetch_powerbi(
        URL,
        tmp_path / "extracted",
        lambda scope: "tok",
        client_for(make_handler()),
        tmp_path / "refs",
        sleep=lambda s: None,
    )
    assert (out / "Sales.SemanticModel" / "definition" / "model.tmdl").exists()
    assert (out / "Sales.Report" / "definition" / "report.json").exists()
    spec = parse(out, "pbip_zip", "Sales")
    assert spec.tables and spec.pages
    assert (tmp_path / "refs" / "powerbi-page-1.png").exists()


def test_long_running_operation_polled(tmp_path):
    log, sleeps = [], []
    fetch_powerbi(
        URL,
        tmp_path / "e",
        lambda scope: "tok",
        client_for(make_handler(log=log, poll_runs=3)),
        sleep=sleeps.append,
    )
    assert sum(1 for _, u in log if u.endswith("/operations/op1")) == 3
    assert sleeps[0] == 3.0 and len(sleeps) == 3


def test_polling_is_bounded(tmp_path):
    sleeps = []
    with pytest.raises(FetchError, match="Timed out"):
        fetch_powerbi(
            URL,
            tmp_path / "e",
            lambda scope: "tok",
            client_for(make_handler(poll_runs=10**6)),
            sleep=sleeps.append,
        )
    assert len(sleeps) <= 60


@pytest.mark.parametrize("bad", ["../evil.txt", "/abs.txt", "a/../../evil.txt", "C:/x.txt"])
def test_unsafe_part_path_rejected(tmp_path, bad):
    evil = [{"path": bad, "payload": base64.b64encode(b"x").decode()}]
    with pytest.raises(FetchError):
        fetch_powerbi(
            URL,
            tmp_path / "e",
            lambda scope: "tok",
            client_for(make_handler(report_parts=evil)),
            sleep=lambda s: None,
        )
    assert not (tmp_path / "evil.txt").exists()


def test_http_403_is_fetch_error(tmp_path):
    with pytest.raises(FetchError, match="refused access"):
        fetch_powerbi(
            URL,
            tmp_path / "e",
            lambda scope: "tok",
            client_for(lambda r: httpx.Response(403)),
            sleep=lambda s: None,
        )


def test_execute_dax():
    rows = execute_dax(DS, "EVALUATE ROW(\"Total\", 5)", lambda scope: "tok", client_for(make_handler()))
    assert rows == [{"[Total]": 5}]


def run(tmp_path, handler, **kw):
    kw.setdefault("sleep", lambda s: None)
    return fetch_powerbi(URL, tmp_path / "e", lambda scope: "tok", client_for(handler), **kw)


@pytest.mark.parametrize(
    "loc",
    [
        "https://evil.example.com/v1/operations/op1",
        "http://api.fabric.microsoft.com/v1/operations/op1",
        "https://api.fabric.microsoft.com.evil.com/x",
    ],
)
def test_foreign_location_never_gets_token(tmp_path, loc):
    log = []
    with pytest.raises(FetchError, match="unexpected URL"):
        run(tmp_path, make_handler(location=loc, log=log))
    assert all("evil" not in u and not u.startswith("http://") for _, u in log)


def test_redirect_is_fetch_error(tmp_path):
    h = httpx.Response(302, headers={"Location": "https://evil.example.com/"})
    with pytest.raises(FetchError, match="redirect"):
        run(tmp_path, make_handler(meta=h))


def test_result_url_keeps_query_string(tmp_path):
    log = []
    loc = "https://api.fabric.microsoft.com/v1/operations/op1?x=1"
    run(tmp_path, make_handler(location=loc, log=log))
    assert any(u.endswith("/operations/op1/result?x=1") for _, u in log)


def test_scopes_differ_for_fabric_and_pbi(tmp_path):
    calls = []
    fetch_powerbi(
        URL,
        tmp_path / "e",
        lambda scope: calls.append(scope) or "tok",
        client_for(make_handler()),
        sleep=lambda s: None,
    )
    assert "https://api.fabric.microsoft.com/.default" in calls
    assert "https://analysis.windows.net/powerbi/api/.default" in calls


def test_oversize_definition_rejected(tmp_path):
    with pytest.raises(FetchError, match="larger than the allowed|oversized"):
        run(tmp_path, make_handler(), max_bytes=100)


def test_oversize_response_stream_rejected(tmp_path):
    big = [{"path": "a.txt", "payload": base64.b64encode(b"x" * 5000).decode()}]
    with pytest.raises(FetchError, match="larger than the allowed"):
        run(tmp_path, make_handler(report_parts=big), max_bytes=1000)


def test_non_json_200_is_fetch_error(tmp_path):
    with pytest.raises(FetchError, match="non-JSON"):
        run(tmp_path, make_handler(meta=httpx.Response(200, content=b"<html>")))


def test_meta_not_a_dict_is_fetch_error(tmp_path):
    with pytest.raises(FetchError, match="unexpected response shape"):
        run(tmp_path, make_handler(meta=httpx.Response(200, json=[1])))


def test_oversize_definition_is_needs_input(tmp_path, monkeypatch):
    from cortino.intake import intake as mod

    def fake(url, dest, **kw):
        fetch_powerbi(
            url,
            dest,
            lambda scope: "tok",
            client_for(make_handler()),
            sleep=lambda s: None,
            max_bytes=100,
        )

    monkeypatch.setattr(mod, "fetch_powerbi", fake)
    store = RunStore(tmp_path / "runs")
    run_ = store.create("x", {})
    res = run_intake(store, run_.id, URL, "", "org/repo", [])
    store.close()
    assert not res.ok and res.needs_input


def test_non_json_200_is_needs_input(tmp_path, monkeypatch):
    from cortino.intake import intake as mod

    def fake(url, dest, **kw):
        fetch_powerbi(
            url,
            dest,
            lambda scope: "tok",
            client_for(make_handler(meta=httpx.Response(200, content=b"nope"))),
        )

    monkeypatch.setattr(mod, "fetch_powerbi", fake)
    store = RunStore(tmp_path / "runs")
    run_ = store.create("x", {})
    res = run_intake(store, run_.id, URL, "", "org/repo", [])
    store.close()
    assert not res.ok and "non-JSON" in res.needs_input


def test_retry_after_capped(tmp_path):
    sleeps = []
    run(tmp_path, make_handler(retry_after="3600"), sleep=sleeps.append)
    assert sleeps[0] == 30.0


def test_retry_after_http_date_falls_back(tmp_path):
    sleeps = []
    run(
        tmp_path,
        make_handler(retry_after="Wed, 21 Oct 2015 07:28:00 GMT"),
        sleep=sleeps.append,
    )
    assert sleeps[0] == 1.0


def test_deadline_checked_before_sleeping(tmp_path):
    sleeps = []
    with pytest.raises(FetchError, match="Timed out"):
        run(tmp_path, make_handler(poll_runs=10**6, retry_after="30"), sleep=sleeps.append)
    assert sum(sleeps) <= 300


def test_image_failure_warns_and_references_not_overwritten(tmp_path):
    warnings = []

    inner = make_handler()

    def h(req):
        if str(req.url).endswith("/ExportTo"):
            return httpx.Response(500)
        return inner(req)

    run(tmp_path, h, references_dir=tmp_path / "refs", on_warning=warnings.append)
    assert warnings and "image export skipped" in warnings[0]
    refs = tmp_path / "refs"
    refs.mkdir(exist_ok=True)
    (refs / "page-1.png").write_bytes(b"mine")
    run(tmp_path, make_handler(), references_dir=refs)
    assert (refs / "page-1.png").read_bytes() == b"mine"
    assert (refs / "powerbi-page-1.png").read_bytes() == PNG
