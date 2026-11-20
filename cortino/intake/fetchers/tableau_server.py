"""Fetch a Tableau Server / Cloud workbook (.twbx) + view images from a view URL.

Supported links: https://<host>/#/site/<site>/views/<workbook>/<view> and the default-site
form https://<host>/#/views/<workbook>/<view>.

Pieces (NOT verified against a live server in this build -- re-confirm before relying on them):

- Sign-in, workbook lookup by content URL and view images use ``tableauserverclient`` (optional
  'tableau' extra, imported lazily) behind the small ``TableauAdapter`` interface so tests inject
  a fake. TSC follows redirects itself and ignores allow_redirects, so the real adapter gives it
  a requests session whose transport adapter refuses any request off the user's origin, raises on
  any 3xx, sets max_redirects=0 and applies a request timeout.
- The .twbx download and the Metadata API GraphQL lineage call go through ``_Api`` (httpx):
  GET  <server>/api/<ver>/sites/<site-luid>/workbooks/<luid>/content
  POST <server>/api/metadata/graphql
  with the ``X-Tableau-Auth`` token.

Security: the server is whatever host the user's link names (customer hosts, no fixed
allowlist). https only; every request -- including any URL the server hands back -- must stay on
that exact scheme+host+port; redirects are never followed (3xx -> FetchError); the PAT is only
ever sent to that host and never appears in messages. Downloads are streamed with a byte limit.
Env: TABLEAU_PAT_NAME, TABLEAU_PAT_SECRET.
"""

import contextlib
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

import httpx

from cortino.config import load
from cortino.intake.fetchers.powerbi_service import FetchError

_FRAGMENT_RE = re.compile(r"^/(?:site/([^/?]+)/)?views/([^/?]+)/([^/?]+)")
LINEAGE_QUERY = (
    "query($luid: String!) { workbooks(filter: {luid: $luid}) { name "
    "upstreamTables { name schema fullName database { name connectionType } } "
    "upstreamDatasources { name } } }"
)
MAX_IMAGES = 200
TSC_TIMEOUT = 60


def looks_like_tableau(url: str) -> bool:
    try:
        frag = urlparse(url.strip()).fragment
    except ValueError:
        return False
    return frag.startswith("/") and "views/" in frag


def _same_origin(url: str, origin: str) -> bool:
    try:
        p, o = urlparse(url), urlparse(origin)
        return (p.scheme, p.hostname, p.port or 443) == (o.scheme, o.hostname, o.port or 443) and not (
            p.username or p.password
        )
    except ValueError:
        return False


def _guard_adapter(origin: str, timeout: float):
    """requests adapter that only talks to origin, never accepts a 3xx, and always has a timeout."""
    from requests.adapters import HTTPAdapter

    class Guard(HTTPAdapter):
        def send(self, request, **kw):
            if not _same_origin(request.url, origin):
                raise FetchError("Tableau returned an unexpected URL; refusing to follow it.")
            if kw.get("timeout") is None:
                kw["timeout"] = timeout
            r = super().send(request, **kw)
            if 300 <= r.status_code < 400:
                r.close()
                raise FetchError(f"Tableau sent an unexpected redirect ({r.status_code}).")
            return r

    return Guard()


def _origin(host: str, port: int | None) -> str:
    return f"https://{host}" + (f":{port}" if port and port != 443 else "")


def parse_tableau_url(url: str) -> tuple[str, str, str, str]:
    """Return (server origin, site, workbook, view); site is '' for the default site."""
    bad = FetchError(
        "Not a Tableau view link. Expected https://<server>/#/site/<site>/views/<workbook>/<view> "
        "or https://<server>/#/views/<workbook>/<view>."
    )
    try:
        p = urlparse(url.strip())
        host, port = p.hostname, p.port
    except ValueError as e:
        raise bad from e
    m = _FRAGMENT_RE.match(p.fragment)
    if p.scheme != "https" or not host or p.username or p.password or not m:
        raise bad
    return _origin(host, port), m.group(1) or "", m.group(2), m.group(3)


@dataclass
class TableauSession:
    token: str
    site_id: str
    api_version: str


class TableauAdapter(Protocol):
    def sign_in(self, server: str, site: str, pat_name: str, pat_secret: str) -> TableauSession: ...
    def find_workbook(self, content_url: str) -> tuple[str, str]: ...  # (luid, name)
    def view_images(self, workbook_luid: str) -> list[tuple[str, bytes]]: ...
    def sign_out(self) -> None: ...


class TscAdapter:
    """tableauserverclient-backed adapter (imported lazily)."""

    def __init__(self, timeout: float = TSC_TIMEOUT):
        self.timeout = timeout
        try:
            import tableauserverclient as tsc
        except ImportError as e:
            raise FetchError("Tableau links need the optional 'tableau' extra.") from e
        self.tsc = tsc
        self.server = None

    def sign_in(self, server, site, pat_name, pat_secret):
        tsc = self.tsc
        try:
            guard = _guard_adapter(server, self.timeout)

            def session_factory():
                import requests

                sess = requests.session()
                sess.max_redirects = 0
                sess.mount("https://", guard)
                sess.mount("http://", guard)
                return sess

            self.server = tsc.Server(server, session_factory=session_factory)
            self.server.add_http_options({"allow_redirects": False})
            self.server.use_server_version()
            self.server.auth.sign_in(tsc.PersonalAccessTokenAuth(pat_name, pat_secret, site))
            return TableauSession(
                self.server.auth_token, self.server.site_id, str(self.server.version)
            )
        except Exception as e:
            raise FetchError(
                "Tableau sign-in failed; check TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET and the "
                "site name."
            ) from e

    def find_workbook(self, content_url):
        tsc = self.tsc
        req = tsc.RequestOptions()
        req.filter.add(
            tsc.Filter(
                tsc.RequestOptions.Field.ContentUrl,
                tsc.RequestOptions.Operator.Equals,
                content_url,
            )
        )
        try:
            items, _ = self.server.workbooks.get(req)
        except Exception as e:
            raise FetchError("Could not look up the Tableau workbook.") from e
        wb = next((w for w in items if w.content_url == content_url), None)
        if wb is None or not wb.id:
            raise FetchError(f"Tableau workbook {content_url!r} not found (or no access).")
        return wb.id, wb.name or content_url

    def view_images(self, workbook_luid):
        wb = self.server.workbooks.get_by_id(workbook_luid)
        self.server.workbooks.populate_views(wb)
        out = []
        for v in list(wb.views)[:MAX_IMAGES]:
            self.server.views.populate_image(v)
            out.append((v.name or "view", bytes(v.image)))
        return out

    def sign_out(self):
        if self.server is not None:
            with contextlib.suppress(Exception):
                self.server.auth.sign_out()


class _Api:
    """httpx client bound to one origin; the token never goes anywhere else."""

    def __init__(self, origin: str, token: str, client: httpx.Client, max_bytes: int):
        self.origin, self.token, self.client, self.max_bytes = origin, token, client, max_bytes

    def _check(self, url: str) -> None:
        if not _same_origin(url, self.origin):
            raise FetchError("Tableau returned an unexpected URL; refusing to follow it.")

    def request(self, method: str, url: str, sink: Callable[[bytes], None], **kw) -> int:
        """Stream a response through sink (bounded by max_bytes); return the status code."""
        self._check(url)
        try:
            req = self.client.build_request(
                method, url, headers={"X-Tableau-Auth": self.token}, **kw
            )
            r = self.client.send(req, stream=True, follow_redirects=False)
            try:
                size = 0
                if r.status_code < 300:
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > self.max_bytes:
                            raise FetchError("Tableau response is larger than the allowed size.")
                        sink(chunk)
            finally:
                r.close()
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise FetchError(f"Could not reach Tableau: {type(e).__name__}") from e
        code = r.status_code
        if code in (401, 403):
            raise FetchError(f"Tableau refused access ({code}); check the token's permissions.")
        if code == 404:
            raise FetchError("Tableau workbook not found.")
        if 300 <= code < 400:
            raise FetchError(f"Tableau sent an unexpected redirect ({code}).")
        if code >= 400:
            raise FetchError(f"Tableau request failed ({code}).")
        return code

    def json(self, method: str, url: str, **kw) -> dict:
        chunks: list[bytes] = []
        self.request(method, url, chunks.append, **kw)
        try:
            data = json.loads(b"".join(chunks))
        except ValueError as e:
            raise FetchError("Tableau returned a non-JSON response.") from e
        if not isinstance(data, dict):
            raise FetchError("Tableau returned an unexpected response shape.")
        return data


def metadata_lineage(
    server: str, workbook_luid: str, token: str, client=None, max_bytes: int | None = None
) -> dict:
    """Upstream tables/datasources of a workbook via the Metadata API (evidence for the planner)."""
    p = urlparse(server)
    api = _Api(
        _origin(p.hostname or "", p.port),
        token,
        client or httpx.Client(timeout=60),
        max_bytes or 10 * 1024 * 1024,
    )
    body = {"query": LINEAGE_QUERY, "variables": {"luid": workbook_luid}}
    data = api.json("POST", f"{api.origin}/api/metadata/graphql", json=body)
    if data.get("errors") or not isinstance(data.get("data"), dict):
        raise FetchError("Tableau Metadata API returned an error.")
    return data["data"]


def _safe(name: str, default: str) -> str:
    return re.sub(r"[^\w\- ]", "_", name).strip(" .") or default


def fetch_tableau(
    url: str,
    dest: Path,
    pat_name: str,
    pat_secret: str,
    adapter: TableauAdapter | None = None,
    client: httpx.Client | None = None,
    references_dir: Path | None = None,
    lineage_path: Path | None = None,
    max_bytes: int | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> Path:
    """Download the workbook .twbx into dest and return its path.

    View PNGs go to references_dir as tableau-<name>.png; lineage JSON (if available) to
    lineage_path. Image/lineage failures only warn.
    """
    server, site, workbook, _view = parse_tableau_url(url)
    if not pat_name or not pat_secret:
        raise FetchError(
            "Tableau credentials missing: set TABLEAU_PAT_NAME and TABLEAU_PAT_SECRET."
        )
    if max_bytes is None:
        max_bytes = load("thresholds")["max_upload_mb"] * 1024 * 1024
    warn = on_warning or (lambda m: None)

    def scrub(e: Exception) -> str:
        return str(e).replace(pat_secret, "***")

    adapter = adapter or TscAdapter()
    try:
        try:
            session = adapter.sign_in(server, site, pat_name, pat_secret)
        except FetchError:
            raise
        except Exception as e:
            raise FetchError(
                "Tableau sign-in failed; check TABLEAU_PAT_NAME / TABLEAU_PAT_SECRET."
            ) from e
        try:
            luid, name = adapter.find_workbook(workbook)
        except FetchError:
            raise
        except Exception as e:
            raise FetchError(f"Could not look up the Tableau workbook: {scrub(e)}") from e
        if not re.fullmatch(r"[\w\-]+", str(luid)) or not re.fullmatch(
            r"[\w\-]+", str(session.site_id)
        ):
            raise FetchError("Tableau returned unusable workbook or site ids.")
        api = _Api(server, session.token, client or httpx.Client(timeout=120), max_bytes)
        dest = Path(dest)
        dest.mkdir(parents=True, exist_ok=True)
        out = dest / f"{_safe(str(name), 'Workbook')}.twbx"
        content = (
            f"{server}/api/{session.api_version}/sites/{session.site_id}/workbooks/{luid}/content"
        )
        try:
            with open(out, "wb") as f:
                api.request("GET", content, f.write)
        except BaseException:
            out.unlink(missing_ok=True)
            raise
        if references_dir is not None:
            try:
                images = adapter.view_images(luid)
                Path(references_dir).mkdir(parents=True, exist_ok=True)
                for i, (vname, data) in enumerate(images, 1):
                    if len(data) > max_bytes:
                        raise FetchError("a view image is larger than the allowed size")
                    stem = _safe(vname, f"view-{i}")
                    (Path(references_dir) / f"tableau-{stem}.png").write_bytes(data)
            except Exception as e:  # noqa: BLE001 - best effort, adapter errors are arbitrary
                warn(f"view image download skipped: {scrub(e)}")
        if lineage_path is not None:
            try:
                lineage = metadata_lineage(server, luid, session.token, client, max_bytes)
                Path(lineage_path).parent.mkdir(parents=True, exist_ok=True)
                Path(lineage_path).write_text(json.dumps(lineage, indent=2), encoding="utf-8")
            except Exception as e:  # noqa: BLE001 - best effort, adapter errors are arbitrary
                warn(f"lineage skipped: {scrub(e)}")
        return out
    finally:
        with contextlib.suppress(Exception):
            adapter.sign_out()
