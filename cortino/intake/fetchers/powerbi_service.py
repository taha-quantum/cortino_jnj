"""Fetch a Power BI service report + semantic model as a PBIP folder.

Endpoints used (from knowledge of the Fabric / Power BI REST APIs; NOT verified live
or against current docs in this build -- re-confirm before relying on them):

- Report metadata (name, datasetId):
  GET  https://api.powerbi.com/v1.0/myorg/groups/{ws}/reports/{report}
- Report definition (PBIR parts), long-running:
  POST https://api.fabric.microsoft.com/v1/workspaces/{ws}/reports/{report}/getDefinition
- Semantic model definition (TMDL parts), long-running:
  POST https://api.fabric.microsoft.com/v1/workspaces/{ws}/semanticModels/{dataset}/getDefinition?format=TMDL
  202 + Location (+ x-ms-operation-id, Retry-After); poll GET Location until
  {"status": "Succeeded"} then GET {Location}/result -> {"definition": {"parts":
  [{"path", "payload" (base64), "payloadType": "InlineBase64"}]}}.
  A 200 reply carries the definition directly.
- Page images (best effort, needs capacity support):
  POST https://api.powerbi.com/v1.0/myorg/groups/{ws}/reports/{report}/ExportTo {"format": "PNG"}
  GET  .../reports/{report}/exports/{exportId}            (poll "status")
  GET  .../reports/{report}/exports/{exportId}/file       (PNG, or a zip of PNGs)
- Ground-truth queries:
  POST https://api.powerbi.com/v1.0/myorg/datasets/{dataset}/executeQueries

Auth: bearer tokens from MSAL client credentials (env PBI_TENANT_ID / PBI_CLIENT_ID /
PBI_CLIENT_SECRET; msal is an optional extra). The token provider takes the scope:
https://api.fabric.microsoft.com/.default for Fabric calls and
https://analysis.windows.net/powerbi/api/.default for Power BI REST (also unverified live).

The bearer token is only ever sent to https hosts in ALLOWED_HOSTS; redirects are never
followed; response sizes, part counts and decoded bytes are capped.
"""

import base64
import binascii
import json
import math
import os
import re
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx

from cortino.config import load
from cortino.intake.unpack import UnsafeArchive, _check_name, safe_unzip

PBI_API = "https://api.powerbi.com/v1.0/myorg"
FABRIC_API = "https://api.fabric.microsoft.com/v1"
PBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"
FABRIC_SCOPE = "https://api.fabric.microsoft.com/.default"
ALLOWED_HOSTS = {"api.fabric.microsoft.com", "api.powerbi.com"}
MAX_POLLS = 60
MAX_POLL_SECONDS = 300.0
MAX_RETRY_AFTER = 30.0
DEFAULT_DELAY = 1.0
MAX_PARTS = 5000
_GUID = r"[0-9a-fA-F]{8}-(?:[0-9a-fA-F]{4}-){3}[0-9a-fA-F]{12}"
_URL_RE = re.compile(rf"^/groups/({_GUID})/reports/({_GUID})(?:/[^/]*)*/?$")

TokenProvider = Callable[[str], str]


class FetchError(Exception):
    """A Power BI link could not be turned into a PBIP folder; the message is user-facing."""


def parse_powerbi_url(url: str) -> tuple[str, str]:
    p = urlparse(url.strip())
    m = _URL_RE.match(p.path)
    if p.scheme != "https" or p.hostname != "app.powerbi.com" or not m:
        raise FetchError(
            "Not a Power BI report link. Expected "
            "https://app.powerbi.com/groups/<workspace-id>/reports/<report-id>."
        )
    return m.group(1), m.group(2)


def msal_token_provider() -> TokenProvider:
    try:
        tenant, client, secret = (
            os.environ["PBI_TENANT_ID"],
            os.environ["PBI_CLIENT_ID"],
            os.environ["PBI_CLIENT_SECRET"],
        )
    except KeyError as e:
        raise FetchError(
            f"Power BI credentials missing: set {e.args[0]} "
            "(and PBI_TENANT_ID, PBI_CLIENT_ID, PBI_CLIENT_SECRET)."
        ) from e
    try:
        import msal
    except ImportError as e:
        raise FetchError("Power BI links need the optional 'powerbi' extra (msal).") from e
    try:
        app = msal.ConfidentialClientApplication(
            client,
            authority=f"https://login.microsoftonline.com/{tenant}",
            client_credential=secret,
        )
    except Exception as e:
        raise FetchError(f"Power BI sign-in setup failed: {e}") from e

    def token(scope: str) -> str:
        try:
            res = app.acquire_token_for_client(scopes=[scope])
        except Exception as e:
            raise FetchError(f"Power BI sign-in failed: {e}") from e
        if "access_token" not in res:
            raise FetchError(f"Power BI sign-in failed: {res.get('error_description', res)}")
        return res["access_token"]

    return token


@dataclass
class _Resp:
    status_code: int
    headers: httpx.Headers
    content: bytes

    def json(self):
        try:
            return json.loads(self.content)
        except ValueError as e:
            raise FetchError("Power BI returned a non-JSON response.") from e

    def obj(self) -> dict:
        data = self.json()
        if not isinstance(data, dict):
            raise FetchError("Power BI returned an unexpected response shape.")
        return data


def _retry_after(headers, default: float = DEFAULT_DELAY) -> float:
    try:
        v = float(headers.get("Retry-After", default))
    except (TypeError, ValueError):
        return default
    if math.isnan(v):
        return default
    return min(max(v, 0.0), MAX_RETRY_AFTER)


def _check_host(url: str) -> None:
    try:
        p = urlparse(url)
        ok = p.scheme == "https" and p.hostname in ALLOWED_HOSTS
    except ValueError:
        ok = False
    if not ok:
        raise FetchError("Power BI returned an unexpected URL; refusing to follow it.")


class _Api:
    def __init__(self, token_provider, client, sleep, max_bytes):
        self.token_provider = token_provider
        self.client = client
        self.sleep = sleep
        self.max_bytes = max_bytes

    def request(
        self, method: str, url: str, scope: str, limit: int | None = None, **kw
    ) -> _Resp:
        _check_host(url)
        limit = limit if limit is not None else self.max_bytes * 2
        headers = {"Authorization": f"Bearer {self.token_provider(scope)}"}
        try:
            req = self.client.build_request(method, url, headers=headers, **kw)
            r = self.client.send(req, stream=True, follow_redirects=False)
            try:
                chunks, size = [], 0
                if r.status_code < 300:
                    for chunk in r.iter_bytes():
                        size += len(chunk)
                        if size > limit:
                            raise FetchError("Power BI response is larger than the allowed size.")
                        chunks.append(chunk)
            finally:
                r.close()
        except (httpx.HTTPError, httpx.InvalidURL) as e:
            raise FetchError(f"Could not reach Power BI: {e}") from e
        code = r.status_code
        if code in (401, 403):
            raise FetchError(
                f"Power BI refused access ({code}); check the service principal "
                "has access to this workspace."
            )
        if code == 404:
            raise FetchError("Power BI report or workspace not found.")
        if 300 <= code < 400:
            raise FetchError(f"Power BI sent an unexpected redirect ({code}).")
        if code >= 400:
            raise FetchError(f"Power BI request failed ({code}): {url}")
        return _Resp(code, r.headers, b"".join(chunks))

    def poll(self, location: str, first_delay: float, scope: str) -> None:
        """Poll an operation URL until it succeeds; bounded by attempts and total time."""
        waited, delay = 0.0, first_delay
        for _ in range(MAX_POLLS):
            if waited + delay > MAX_POLL_SECONDS:
                break
            self.sleep(delay)
            waited += delay
            r = self.request("GET", location, scope)
            body = r.obj()
            status = str(body.get("status", ""))
            if status == "Succeeded":
                return
            if status == "Failed":
                raise FetchError(f"Power BI operation failed: {body.get('error', body)}")
            delay = _retry_after(r.headers)
        raise FetchError("Timed out waiting for Power BI.")

    def definition_parts(self, url: str) -> list[dict]:
        r = self.request("POST", url, FABRIC_SCOPE)
        if r.status_code == 202:
            location = r.headers.get("Location")
            if not location:
                raise FetchError("Power BI long-running operation gave no Location header.")
            _check_host(location)
            self.poll(location, _retry_after(r.headers), FABRIC_SCOPE)
            sp = urlsplit(location)
            result = urlunsplit(sp._replace(path=sp.path.rstrip("/") + "/result"))
            r = self.request("GET", result, FABRIC_SCOPE)
        try:
            parts = r.obj()["definition"]["parts"]
        except (KeyError, TypeError) as e:
            raise FetchError("Power BI returned an unexpected definition payload.") from e
        if not isinstance(parts, list) or not all(isinstance(x, dict) for x in parts):
            raise FetchError("Power BI returned an unexpected definition payload.")
        if len(parts) > MAX_PARTS:
            raise FetchError("Power BI definition has too many parts.")
        return parts


def _write_parts(parts: list[dict], folder: Path, budget: list[int]) -> None:
    """Write decoded parts under folder; budget[0] is the decoded bytes still allowed."""
    root = folder.resolve()
    for part in parts:
        path = str(part.get("path", ""))
        try:
            _check_name(path)
        except UnsafeArchive as e:
            raise FetchError(f"Refusing unsafe path from Power BI: {e}") from e
        target = (root / path.replace("\\", "/")).resolve()
        if target == root or not target.is_relative_to(root):
            raise FetchError(f"Refusing path that escapes the project: {path!r}")
        payload = part.get("payload")
        if not isinstance(payload, str) or len(payload) * 3 // 4 > budget[0] + 3:
            raise FetchError(f"Bad or oversized payload for part {path!r}")
        try:
            data = base64.b64decode(payload, validate=True)
        except (binascii.Error, ValueError) as e:
            raise FetchError(f"Bad payload for part {path!r}") from e
        budget[0] -= len(data)
        if budget[0] < 0:
            raise FetchError("Power BI definition is larger than the allowed size.")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _safe_name(name: str) -> str:
    return re.sub(r"[^\w\- ]", "_", name).strip(" .") or "Report"


def _export_images(api: _Api, ws: str, report: str, out_dir: Path) -> None:
    """Page PNGs land in out_dir as powerbi-<name> so user references are never overwritten."""
    base = f"{PBI_API}/groups/{ws}/reports/{report}"
    r = api.request("POST", f"{base}/ExportTo", PBI_SCOPE, json={"format": "PNG"})
    export_id = r.obj().get("id")
    if not isinstance(export_id, str) or not re.fullmatch(r"[\w\-]+", export_id):
        raise FetchError("Power BI export gave no usable id.")
    api.poll(f"{base}/exports/{export_id}", _retry_after(r.headers), PBI_SCOPE)
    data = api.request("GET", f"{base}/exports/{export_id}/file", PBI_SCOPE).content
    out_dir.mkdir(parents=True, exist_ok=True)
    if data[:2] == b"PK":
        tmp = out_dir / "_export"
        tmp.mkdir(exist_ok=True)
        zpath = tmp / "export.zip"
        zpath.write_bytes(data)
        try:
            for f in safe_unzip(zpath, tmp / "x", api.max_bytes):
                shutil.move(f, out_dir / f"powerbi-{f.name}")
        except UnsafeArchive as e:
            raise FetchError(f"Unsafe image export: {e}") from e
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    else:
        (out_dir / "powerbi-page-1.png").write_bytes(data)


def fetch_powerbi(
    url: str,
    dest: Path,
    token_provider: TokenProvider | None = None,
    client: httpx.Client | None = None,
    references_dir: Path | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_bytes: int | None = None,
    on_warning: Callable[[str], None] | None = None,
) -> Path:
    """Write <Name>.Report and <Name>.SemanticModel under dest; page PNGs go to references_dir."""
    ws, report = parse_powerbi_url(url)
    if max_bytes is None:
        max_bytes = load("thresholds")["max_unpacked_mb"] * 1024 * 1024
    api = _Api(
        token_provider or msal_token_provider(), client or httpx.Client(timeout=60), sleep, max_bytes
    )
    meta = api.request("GET", f"{PBI_API}/groups/{ws}/reports/{report}", PBI_SCOPE).obj()
    dataset = meta.get("datasetId")
    if not isinstance(dataset, str) or not re.fullmatch(_GUID, dataset):
        raise FetchError("Could not find the report's semantic model.")
    name = _safe_name(str(meta.get("name", "Report")))
    report_parts = api.definition_parts(
        f"{FABRIC_API}/workspaces/{ws}/reports/{report}/getDefinition"
    )
    model_parts = api.definition_parts(
        f"{FABRIC_API}/workspaces/{ws}/semanticModels/{dataset}/getDefinition?format=TMDL"
    )
    dest = Path(dest)
    budget = [max_bytes]
    _write_parts(model_parts, dest / f"{name}.SemanticModel", budget)
    _write_parts(report_parts, dest / f"{name}.Report", budget)
    if references_dir is not None:
        try:
            _export_images(api, ws, report, Path(references_dir))
        except FetchError as e:
            if on_warning:
                on_warning(f"page image export skipped: {e}")
    return dest


def execute_dax(
    dataset_id: str,
    query: str,
    token_provider: TokenProvider | None = None,
    client: httpx.Client | None = None,
    max_bytes: int = 50 * 1024 * 1024,
) -> list[dict]:
    api = _Api(
        token_provider or msal_token_provider(),
        client or httpx.Client(timeout=60),
        time.sleep,
        max_bytes,
    )
    body = {"queries": [{"query": query}], "serializerSettings": {"includeNulls": True}}
    r = api.request("POST", f"{PBI_API}/datasets/{dataset_id}/executeQueries", PBI_SCOPE, json=body)
    try:
        rows = r.obj()["results"][0]["tables"][0]["rows"]
    except (KeyError, IndexError, TypeError) as e:
        raise FetchError("Unexpected executeQueries response.") from e
    if not isinstance(rows, list):
        raise FetchError("Unexpected executeQueries response.")
    return rows
