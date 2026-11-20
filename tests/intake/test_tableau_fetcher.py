import io
import json
import zipfile

import httpx
import pytest

from cortino.intake.fetchers.tableau_server import (
    FetchError,
    TableauSession,
    fetch_tableau,
    metadata_lineage,
    parse_tableau_url,
)
from cortino.intake.intake import run_intake
from cortino.orchestrator.run_store import RunStore

SERVER = "https://tab.example.com"
URL = f"{SERVER}/#/site/acme/views/Sales/Overview?:iid=1"
LUID = "aaaa-1111"
SITE_ID = "site-9"
SECRET = "s3cr3t-value"
PNG = b"\x89PNG\r\n\x1a\nfake"


def make_twbx() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("Sales.twb", "<workbook><datasources><datasource name='x'/></datasources></workbook>")
    return buf.getvalue()


class FakeAdapter:
    def __init__(self, fail_sign_in=False, missing=False, images=None, host=SERVER):
        self.fail_sign_in, self.missing, self.host = fail_sign_in, missing, host
        self.images = images if images is not None else [("Overview", PNG), ("Detail", PNG)]
        self.calls = []

    def sign_in(self, server, site, pat_name, pat_secret):
        self.calls.append(("sign_in", server, site, pat_name))
        if self.fail_sign_in:
            raise FetchError("Tableau sign-in failed; check TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET.")
        return TableauSession("tok", SITE_ID, "3.22")

    def find_workbook(self, content_url):
        self.calls.append(("find", content_url))
        if self.missing:
            raise FetchError("Tableau workbook not found (or no access).")
        return LUID, "Sales"

    def view_images(self, luid):
        return self.images

    def sign_out(self):
        self.calls.append(("sign_out",))


def make_handler(log=None, content=None, content_response=None):
    content = content if content is not None else make_twbx()

    def handler(req: httpx.Request) -> httpx.Response:
        if log is not None:
            log.append((req.method, str(req.url), dict(req.headers)))
        assert req.headers["x-tableau-auth"] == "tok"
        path = req.url.path
        if path == f"/api/3.22/sites/{SITE_ID}/workbooks/{LUID}/content":
            return content_response or httpx.Response(200, content=content)
        if path == "/api/metadata/graphql":
            body = json.loads(req.content)
            assert body["variables"] == {"luid": LUID}
            return httpx.Response(
                200, json={"data": {"workbooks": [{"name": "Sales", "upstreamTables": []}]}}
            )
        return httpx.Response(404)

    return handler


def client_for(handler):
    return httpx.Client(transport=httpx.MockTransport(handler))


def run(tmp_path, adapter=None, handler=None, **kw):
    return fetch_tableau(
        URL,
        tmp_path / "dl",
        "tokname",
        SECRET,
        adapter=adapter or FakeAdapter(),
        client=client_for(handler or make_handler()),
        **kw,
    )


def test_url_parsed_with_site():
    assert parse_tableau_url(URL) == (SERVER, "acme", "Sales", "Overview")


def test_default_site_url():
    assert parse_tableau_url(f"{SERVER}/#/views/Sales/Overview") == (
        SERVER,
        "",
        "Sales",
        "Overview",
    )
    assert parse_tableau_url("https://t.example.com:8443/#/views/W/V")[0] == (
        "https://t.example.com:8443"
    )


@pytest.mark.parametrize(
    "url",
    [
        "http://tab.example.com/#/views/W/V",
        "https://user:pw@tab.example.com/#/views/W/V",
        "https://tab.example.com/views/W/V",
        "https://tab.example.com/#/home",
        "https://tab.example.com:abc/#/views/W/V",
        "not a url",
    ],
)
def test_bad_url_rejected(url):
    with pytest.raises(FetchError):
        parse_tableau_url(url)


def test_downloads_twbx_and_images(tmp_path):
    adapter = FakeAdapter()
    refs, lin = tmp_path / "refs", tmp_path / "in" / "tableau_lineage.json"
    out = run(tmp_path, adapter, references_dir=refs, lineage_path=lin)
    assert out.name == "Sales.twbx" and zipfile.is_zipfile(out)
    assert sorted(p.name for p in refs.iterdir()) == ["tableau-Detail.png", "tableau-Overview.png"]
    assert (refs / "tableau-Overview.png").read_bytes() == PNG
    assert json.loads(lin.read_text())["workbooks"][0]["name"] == "Sales"
    assert adapter.calls[0] == ("sign_in", SERVER, "acme", "tokname")
    assert ("find", "Sales") in adapter.calls and adapter.calls[-1] == ("sign_out",)


def test_bad_credentials_needs_input(tmp_path, monkeypatch):
    from cortino.intake import intake as mod

    monkeypatch.setenv("TABLEAU_PAT_NAME", "n")
    monkeypatch.setenv("TABLEAU_PAT_SECRET", SECRET)
    monkeypatch.setattr(
        mod,
        "fetch_tableau",
        lambda url, dest, n, s, **kw: fetch_tableau(
            url, dest, n, s, adapter=FakeAdapter(fail_sign_in=True), **kw
        ),
    )
    store = RunStore(tmp_path / "runs")
    r = store.create("x", {})
    res = run_intake(store, r.id, URL, "", "org/repo", [])
    events = (store.path(r.id) / "events.jsonl")
    ev = events.read_text() if events.exists() else ""
    store.close()
    assert not res.ok and "sign-in failed" in res.needs_input
    assert SECRET not in res.needs_input and SECRET not in ev


def test_missing_credentials_needs_input(tmp_path, monkeypatch):
    monkeypatch.delenv("TABLEAU_PAT_NAME", raising=False)
    monkeypatch.delenv("TABLEAU_PAT_SECRET", raising=False)
    store = RunStore(tmp_path / "runs")
    r = store.create("x", {})
    res = run_intake(store, r.id, URL, "", "org/repo", [])
    store.close()
    assert not res.ok and "TABLEAU_PAT_NAME" in res.needs_input


def test_workbook_not_found_signs_out(tmp_path):
    adapter = FakeAdapter(missing=True)
    with pytest.raises(FetchError, match="not found"):
        run(tmp_path, adapter)
    assert adapter.calls[-1] == ("sign_out",)


def test_redirect_is_fetch_error_and_not_followed(tmp_path):
    log = []
    h = make_handler(
        log, content_response=httpx.Response(302, headers={"Location": "https://evil.com/x"})
    )
    with pytest.raises(FetchError, match="redirect"):
        run(tmp_path, handler=h)
    assert all("evil.com" not in u for _, u, _ in log)
    assert not list((tmp_path / "dl").glob("*.twbx"))


def test_foreign_host_never_gets_token(tmp_path):
    log = []
    api_server = "https://other.example.com"
    adapter = FakeAdapter()
    adapter.sign_in = lambda *a: TableauSession("tok", "../../x", "3.22")
    with pytest.raises(FetchError):
        run(tmp_path, adapter, make_handler(log))
    assert api_server not in str(log) and not log


def test_lineage_refuses_other_host():
    from cortino.intake.fetchers.tableau_server import _Api

    api = _Api(SERVER, "tok", client_for(make_handler()), 1000)
    for bad in ("https://evil.com/api", "http://tab.example.com/api", "https://tab.example.com:8443/a"):
        with pytest.raises(FetchError):
            api.json("GET", bad)


def test_download_over_limit_rejected_and_removed(tmp_path):
    with pytest.raises(FetchError, match="larger"):
        run(tmp_path, max_bytes=10)
    assert not list((tmp_path / "dl").glob("*.twbx"))


def test_network_error_is_fetch_error(tmp_path):
    def h(req):
        raise httpx.ConnectError("boom")

    with pytest.raises(FetchError, match="Could not reach"):
        run(tmp_path, handler=h)


def test_lineage_and_image_failures_only_warn(tmp_path):
    warnings = []
    inner = make_handler()

    def h(req):
        if req.url.path == "/api/metadata/graphql":
            return httpx.Response(200, content=b"not json")
        return inner(req)

    adapter = FakeAdapter()
    adapter.view_images = lambda luid: (_ for _ in ()).throw(RuntimeError(f"bad {SECRET}"))
    out = run(
        tmp_path,
        adapter,
        h,
        references_dir=tmp_path / "refs",
        lineage_path=tmp_path / "lin.json",
        on_warning=warnings.append,
    )
    assert out.exists() and len(warnings) == 2
    assert all(SECRET not in w for w in warnings)


def test_metadata_lineage_returns_data():
    data = metadata_lineage(SERVER, LUID, "tok", client_for(make_handler()))
    assert data["workbooks"][0]["name"] == "Sales"


def test_intake_continues_as_twbx_upload(tmp_path, monkeypatch):
    from cortino.intake import intake as mod

    monkeypatch.setenv("TABLEAU_PAT_NAME", "n")
    monkeypatch.setenv("TABLEAU_PAT_SECRET", SECRET)
    monkeypatch.setattr(
        mod,
        "fetch_tableau",
        lambda url, dest, n, s, **kw: fetch_tableau(
            url, dest, n, s, adapter=FakeAdapter(), client=client_for(make_handler()), **kw
        ),
    )
    store = RunStore(tmp_path / "runs")
    r = store.create("x", {})
    ref = tmp_path / "mine.png"
    ref.write_bytes(b"mine")
    res = run_intake(store, r.id, URL, "", "org/repo", [ref])
    d = store.path(r.id)
    meta = json.loads((d / "intake.json").read_text())
    st = store.get(r.id).meta["source_type"]
    store.close()
    assert res.ok and st == "twbx" and meta["model_present"]
    assert (d / "input" / "Sales.twbx").exists()
    assert (d / "input" / "tableau_lineage.json").exists()
    assert "input/references/tableau-Overview.png" in meta["reference_images"]
    assert "input/references/mine.png" in meta["reference_images"]
    assert not (d / "download").exists()


def test_other_url_still_needs_input(tmp_path):
    store = RunStore(tmp_path / "runs")
    r = store.create("x", {})
    res = run_intake(store, r.id, "https://example.com/nope", "", "org/repo", [])
    store.close()
    assert not res.ok and res.needs_input


# --- real TscAdapter over a faked requests transport (no network) ---

NS = 'xmlns="http://tableau.com/api"'


def _xml(body):
    import requests

    r = requests.Response()
    r.status_code = 200
    r._content = f"<tsResponse {NS}>{body}</tsResponse>".encode()
    r.headers["Content-Type"] = "application/xml"
    return r


class FakeNet:
    def __init__(self, signin_status=200, redirect_to=None):
        self.requests, self.signin_status, self.redirect_to = [], signin_status, redirect_to
        self.timeouts = []

    def send(self, request, **kw):
        import requests

        self.requests.append((request.method, request.url, request.body))
        self.timeouts.append(kw.get("timeout"))
        u = request.url
        r = requests.Response()
        r.url = u
        if "/serverInfo" in u:
            return _xml("<serverInfo><productVersion build='b'>2023.1</productVersion><restApiVersion>3.19</restApiVersion></serverInfo>")
        if u.endswith("/auth/signin"):
            if self.redirect_to:
                r.status_code = 307
                r.headers["Location"] = self.redirect_to
                return r
            if self.signin_status != 200:
                r.status_code = self.signin_status
                r._content = (
                    f'<tsResponse {NS}><error code="401001"><summary>x</summary>'
                    "<detail>bad</detail></error></tsResponse>"
                ).encode()
                return r
            return _xml(
                f'<credentials token="tok"><site id="{SITE_ID}" contentUrl="acme"/>'
                '<user id="u1"/></credentials>'
            )
        if u.endswith("/auth/signout"):
            r.status_code = 204
            return r
        if u.split("?")[0].endswith("/workbooks"):
            return _xml(
                '<pagination pageNumber="1" pageSize="100" totalAvailable="1"/>'
                f'<workbooks><workbook id="{LUID}" name="Sales" contentUrl="Sales"/></workbooks>'
            )
        if u.endswith(f"/workbooks/{LUID}"):
            return _xml(f'<workbook id="{LUID}" name="Sales" contentUrl="Sales"/>')
        if u.endswith(f"/workbooks/{LUID}/views"):
            return _xml(
                '<views><view id="v1" name="Overview" contentUrl="Sales/sheets/Overview"/></views>'
            )
        if "/views/v1/image" in u:
            r.status_code = 200
            r._content = PNG
            r.headers["Content-Type"] = "image/png"
            return r
        r.status_code = 404
        return r


@pytest.fixture
def net(monkeypatch):
    pytest.importorskip("tableauserverclient")
    pytest.importorskip("requests")
    from requests.adapters import HTTPAdapter

    n = FakeNet()
    monkeypatch.setattr(HTTPAdapter, "send", lambda self, request, **kw: n.send(request, **kw))
    return n


def test_tsc_sign_in_lookup_images_sign_out(net):
    from cortino.intake.fetchers.tableau_server import TscAdapter

    a = TscAdapter()
    sess = a.sign_in(SERVER, "acme", "tokname", SECRET)
    assert (sess.token, sess.site_id, sess.api_version) == ("tok", SITE_ID, "3.19")
    assert a.find_workbook("Sales") == (LUID, "Sales")
    assert a.view_images(LUID) == [("Overview", PNG)]
    a.sign_out()
    assert net.requests[-1][1].endswith("/auth/signout")
    assert all(u.startswith(SERVER) for _, u, _ in net.requests)


def test_tsc_sign_in_redirect_to_foreign_host_never_leaves(net):
    from cortino.intake.fetchers.tableau_server import TscAdapter

    net.redirect_to = "https://evil.example.com/steal"
    with pytest.raises(FetchError) as ei:
        TscAdapter().sign_in(SERVER, "acme", "tokname", SECRET)
    assert SECRET not in str(ei.value)
    assert all("evil.example.com" not in u for _, u, _ in net.requests)


def test_tsc_same_host_redirect_also_refused(net):
    from cortino.intake.fetchers.tableau_server import TscAdapter

    net.redirect_to = f"{SERVER}/other"
    with pytest.raises(FetchError):
        TscAdapter().sign_in(SERVER, "acme", "tokname", SECRET)
    assert not any(u.endswith("/other") for _, u, _ in net.requests)


def test_tsc_bad_credentials_no_secret_in_error(net):
    from cortino.intake.fetchers.tableau_server import TscAdapter

    net.signin_status = 401
    with pytest.raises(FetchError, match="sign-in failed") as ei:
        TscAdapter().sign_in(SERVER, "acme", "tokname", SECRET)
    assert SECRET not in str(ei.value)


def test_tsc_guard_refuses_foreign_and_downgraded_urls():
    pytest.importorskip("tableauserverclient")
    pytest.importorskip("requests")
    from requests import Request

    from cortino.intake.fetchers.tableau_server import _guard_adapter

    g = _guard_adapter(SERVER, 7)
    for url in ("https://evil.example.com/x", "http://tab.example.com/x"):
        with pytest.raises(FetchError):
            g.send(Request("GET", url).prepare())


def test_tsc_timeout_always_applied(net):
    from cortino.intake.fetchers.tableau_server import TscAdapter

    TscAdapter(timeout=7).sign_in(SERVER, "acme", "tokname", SECRET)
    assert net.timeouts and set(net.timeouts) == {7}
