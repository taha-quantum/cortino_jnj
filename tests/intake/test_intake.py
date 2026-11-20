import json
import stat
import zipfile
from pathlib import Path

import pytest

from cortino.intake import unpack
from cortino.intake.detect import detect
from cortino.intake.intake import run_intake
from cortino.intake.unpack import UnsafeArchive, safe_unzip
from cortino.orchestrator.run_store import RunStore

TWB = '<?xml version="1.0"?>\n<workbook><datasources><datasource name="a"/></datasources></workbook>'


def make_zip(path: Path, entries: dict[str, str | bytes]) -> Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


@pytest.fixture
def store(tmp_path):
    s = RunStore(tmp_path / "runs")
    yield s
    s.close()


def intake(store, upload, **kw):
    run = store.create(upload.name, {})
    res = run_intake(
        store, run.id, upload, kw.get("notes", ""), "org/repo", kw.get("refs", [])
    )
    return run.id, res


def test_detects_renamed_twb(tmp_path):
    f = tmp_path / "dash.txt"
    f.write_text(TWB)
    assert detect(f) == "twb"


def test_pbip_zip_detected(tmp_path):
    z = make_zip(
        tmp_path / "a.zip",
        {"Sales.SemanticModel/model.bim": "{}", "Sales.Report/r.json": "{}"},
    )
    assert detect(z) == "pbip_zip"


def test_other_types_detected(tmp_path):
    assert detect(make_zip(tmp_path / "a.pbit", {"DataModelSchema": "{}"})) == "pbit"
    assert detect(make_zip(tmp_path / "b.pbix", {"DataModel": "x"})) == "pbix"
    assert detect(make_zip(tmp_path / "c.twbx", {"d.twb": TWB})) == "twbx"
    junk = tmp_path / "j.bin"
    junk.write_bytes(b"\x00\x01nothing")
    assert detect(junk) == "unknown"


def test_zip_slip_rejected(tmp_path):
    z = make_zip(tmp_path / "s.zip", {"../evil.txt": "x"})
    dest = tmp_path / "out" / "extracted"
    with pytest.raises(UnsafeArchive):
        safe_unzip(z, dest, 10**6)
    assert not (tmp_path / "out" / "evil.txt").exists()
    assert not (tmp_path / "evil.txt").exists()


@pytest.mark.parametrize(
    "name", ["/abs.txt", "C:/win.txt", "C:win.txt", r"..\evil.txt", "a/../../e.txt"]
)
def test_absolute_path_entry_rejected(tmp_path, name):
    z = make_zip(tmp_path / "s.zip", {name: "x"})
    with pytest.raises(UnsafeArchive):
        safe_unzip(z, tmp_path / "dest", 10**6)


def test_symlink_entry_rejected(tmp_path):
    z = tmp_path / "l.zip"
    with zipfile.ZipFile(z, "w") as zf:
        info = zipfile.ZipInfo("link")
        info.external_attr = (stat.S_IFLNK | 0o777) << 16
        zf.writestr(info, "/etc/passwd")
    with pytest.raises(UnsafeArchive):
        safe_unzip(z, tmp_path / "dest", 10**6)


def test_zip_bomb_rejected_by_declared_size(tmp_path):
    z = make_zip(tmp_path / "b.zip", {"big.bin": b"0" * 100_000})
    with pytest.raises(UnsafeArchive):
        safe_unzip(z, tmp_path / "dest", 1000)
    assert not (tmp_path / "dest" / "big.bin").exists()


def test_zip_bomb_rejected_while_streaming(tmp_path, monkeypatch):
    z = make_zip(tmp_path / "b.zip", {"big.bin": b"0" * 100_000})
    monkeypatch.setattr(unpack, "_declared_total", lambda infos: 0)  # lying headers
    with pytest.raises(UnsafeArchive):
        safe_unzip(z, tmp_path / "dest", 1000)


def test_safe_unzip_extracts(tmp_path):
    z = make_zip(tmp_path / "a.zip", {"d/x.txt": "hi", "y.txt": "yo"})
    files = safe_unzip(z, tmp_path / "dest", 10**6)
    assert sorted(f.name for f in files) == ["x.txt", "y.txt"]
    assert (tmp_path / "dest" / "d" / "x.txt").read_text() == "hi"


def test_pbix_needs_input(store, tmp_path):
    up = make_zip(tmp_path / "r.pbix", {"DataModel": "x", "Report/Layout": "{}"})
    rid, res = intake(store, up)
    assert not res.ok
    assert ".pbit or PBIP" in res.needs_input
    assert (
        json.loads((store.path(rid) / "intake.json").read_text())["model_present"]
        is False
    )


def test_input_kept_untouched(store, tmp_path):
    up = make_zip(tmp_path / "r.zip", {"S.SemanticModel/model.bim": "{}"})
    rid, res = intake(store, up)
    assert res.ok and res.needs_input is None
    assert (store.path(rid) / "input" / "r.zip").read_bytes() == up.read_bytes()
    assert (store.path(rid) / "extracted" / "S.SemanticModel" / "model.bim").exists()
    data = json.loads((store.path(rid) / "intake.json").read_text())
    assert data["source_type"] == "pbip_zip" and data["model_present"] is True
    assert data["run_id"] == rid and data["target_repo"] == "org/repo"


def test_plain_twb_copied_and_refs(store, tmp_path):
    up = tmp_path / "dash.twb"
    up.write_text(TWB)
    ref = tmp_path / "shot.png"
    ref.write_bytes(b"png")
    rid, res = intake(store, up, notes="n", refs=[ref])
    assert res.ok
    assert (store.path(rid) / "extracted" / "dash.twb").exists()
    assert (
        store.path(rid) / "input" / "references" / "shot.png"
    ).read_bytes() == b"png"
    data = json.loads((store.path(rid) / "intake.json").read_text())
    assert data["source_type"] == "twb" and data["notes"] == "n"
    assert data["reference_images"] == ["input/references/shot.png"]
    assert data["files"] == ["dash.twb"]


def test_twbx_without_datasources_has_no_model(store, tmp_path):
    up = make_zip(tmp_path / "t.twbx", {"d.twb": "<?xml version='1.0'?><workbook/>"})
    rid, res = intake(store, up)
    assert (
        json.loads((store.path(rid) / "intake.json").read_text())["model_present"]
        is False
    )
    assert not res.ok and res.needs_input


def test_oversize_upload_needs_input(store, tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cortino.intake.intake.load",
        lambda n: {"max_upload_mb": 0, "max_unpacked_mb": 1},
    )
    up = tmp_path / "d.twb"
    up.write_text(TWB)
    rid, res = intake(store, up)
    assert not res.ok and "MB limit" in res.needs_input
    assert not (store.path(rid) / "extracted").exists()


def test_unsafe_archive_propagates(store, tmp_path):
    up = make_zip(tmp_path / "e.zip", {"../evil.txt": "x", "S.SemanticModel/m": "{}"})
    run = store.create(up.name, {})
    with pytest.raises(UnsafeArchive):
        run_intake(store, run.id, up, "", "r", [])


def test_relative_store_root(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    s = RunStore(Path("runs"))
    try:
        up = make_zip(tmp_path / "r.zip", {"S.SemanticModel/model.bim": "{}"})
        run = s.create(up.name, {})
        assert run_intake(s, run.id, up, "", "r", []).ok
        assert json.loads((s.path(run.id) / "intake.json").read_text())["files"] == [
            "S.SemanticModel/model.bim"
        ]
    finally:
        s.close()


def test_corrupt_zip_is_unsafe_archive(tmp_path):
    z = make_zip(tmp_path / "c.zip", {"a.txt": b"hello" * 100})
    data = bytearray(z.read_bytes())
    data[40] ^= 0xFF  # corrupt stored payload -> bad CRC
    z.write_bytes(bytes(data))
    with pytest.raises(UnsafeArchive):
        safe_unzip(z, tmp_path / "d", 10**6)
    trunc = tmp_path / "t.zip"
    trunc.write_bytes(z.read_bytes()[:30])
    with pytest.raises(UnsafeArchive):
        safe_unzip(trunc, tmp_path / "d2", 10**6)


def test_encrypted_entry_is_unsafe_archive(tmp_path):
    z = make_zip(tmp_path / "e.zip", {"a.txt": "x"})
    data = bytearray(z.read_bytes())
    for sig, off in ((b"PK\x03\x04", 6), (b"PK\x01\x02", 8)):
        data[data.index(sig) + off] |= 1
    z.write_bytes(bytes(data))
    with pytest.raises(UnsafeArchive):
        safe_unzip(z, tmp_path / "d", 10**6)


def test_entry_resolving_to_dest_rejected(tmp_path):
    z = make_zip(tmp_path / "s.zip", {"./": ""})
    with pytest.raises(UnsafeArchive):
        safe_unzip(z, tmp_path / "d", 10**6)


def test_marker_straddling_chunk_boundary(store, tmp_path, monkeypatch):
    monkeypatch.setattr("cortino.intake.intake.CHUNK", 7)
    up = tmp_path / "d.twb"
    up.write_text(
        '<?xml version="1.0"?><workbook>' + "x" * 5 + "<datasources/></workbook>"
    )
    _, res = intake(store, up)
    assert res.ok


def test_unsafe_archive_cleans_up_and_logs(store, tmp_path):
    up = make_zip(tmp_path / "e.zip", {"S.SemanticModel/m": "{}", "../evil.txt": "x"})
    run = store.create(up.name, {})
    with pytest.raises(UnsafeArchive):
        run_intake(store, run.id, up, "", "r", [])
    assert not (store.path(run.id) / "extracted").exists()
    assert "intake_rejected" in (store.path(run.id) / "events.jsonl").read_text()


@pytest.mark.parametrize(
    ("url", "fetcher"),
    [("https://app.powerbi.com/groups/ws/reports/rep", "fetch_powerbi"),
     ("https://tab.example.com/#/views/Sales/Overview", "fetch_tableau")],
)  # fmt: skip
def test_intake_stage_passes_a_link_as_a_string(tmp_path, monkeypatch, url, fetcher):
    from cortino.intake import intake
    from cortino.orchestrator.runner import RunContext
    from cortino.orchestrator.stages import intake_stage
    from cortino.orchestrator.states import RunState

    seen = []

    def fetch(link, *args, **kwargs):
        seen.append(link)
        raise intake.FetchError("offline")

    monkeypatch.setattr(intake, fetcher, fetch)
    store = RunStore(tmp_path / "runs")
    run = store.create("link", {"upload": url, "name": "Sales", "target_repo": "r"})
    result = intake_stage(RunContext(store, run, store.path(run.id), {}))
    store.close()
    assert seen == [url] and result.next == RunState.NEEDS_INPUT and result.note == "offline"
