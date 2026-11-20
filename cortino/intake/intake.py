import json
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from cortino.config import load
from cortino.intake.detect import detect
from cortino.intake.fetchers.powerbi_service import FetchError, fetch_powerbi
from cortino.intake.fetchers.tableau_server import fetch_tableau, looks_like_tableau
from cortino.intake.unpack import UnsafeArchive, safe_unzip
from cortino.orchestrator.run_store import RunStore

MARKER = b"<datasources"
CHUNK = 1 << 20

PBIX_MSG = (
    "A .pbix does not contain a readable data model. Please re-save it as .pbit or PBIP, "
    "or give a Power BI service link."
)


@dataclass
class IntakeResult:
    ok: bool
    needs_input: str | None = None


def _has_marker(path: Path) -> bool:
    keep = len(MARKER) - 1
    tail = b""
    with open(path, "rb") as f:
        while chunk := f.read(CHUNK):
            if MARKER in tail + chunk:
                return True
            tail = (tail + chunk)[-keep:]
    return False


def _model_present(source_type: str, files: list[Path]) -> bool:
    if source_type in ("pbip_zip", "pbit"):
        return True
    if source_type in ("twb", "twbx"):
        return any(_has_marker(f) for f in files if f.suffix.lower() == ".twb")
    return False


def run_intake(
    store: RunStore,
    run_id: str,
    upload: Path | str,
    notes: str,
    target_repo: str,
    references: list[Path],
) -> IntakeResult:
    cfg = load("thresholds")
    run_dir = store.path(run_id)
    extracted = run_dir / "extracted"
    ref_dir = run_dir / "input" / "references"
    files: list[Path] = []
    ref_names: list[str] = []
    from_tableau = False
    is_url = isinstance(upload, str) and upload.lower().startswith(("http://", "https://"))
    if is_url and looks_like_tableau(upload):
        # Download the .twbx (+ view images, lineage evidence), then continue as an upload.
        download = run_dir / "download"
        try:
            upload = fetch_tableau(
                upload,
                download,
                os.environ.get("TABLEAU_PAT_NAME", ""),
                os.environ.get("TABLEAU_PAT_SECRET", ""),
                references_dir=ref_dir,
                lineage_path=run_dir / "input" / "tableau_lineage.json",
                max_bytes=cfg["max_upload_mb"] * 1024 * 1024,
                on_warning=lambda m: store.event(run_id, "intake_warning", detail=m),
            )
        except FetchError as e:
            shutil.rmtree(download, ignore_errors=True)
            store.event(run_id, "intake_rejected", reason="fetch_failed", detail=str(e))
            return IntakeResult(False, str(e))
        if ref_dir.is_dir():
            ref_names = [f"input/references/{f.name}" for f in sorted(ref_dir.iterdir())]
        is_url, from_tableau = False, True
    if is_url:
        try:
            fetch_powerbi(
                upload,
                extracted,
                references_dir=ref_dir,
                max_bytes=cfg["max_unpacked_mb"] * 1024 * 1024,
                on_warning=lambda m: store.event(run_id, "intake_warning", detail=m),
            )
        except FetchError as e:
            shutil.rmtree(extracted, ignore_errors=True)
            store.event(run_id, "intake_rejected", reason="fetch_failed", detail=str(e))
            return IntakeResult(False, str(e))
        source_type = "pbip_zip"
        files = [f for f in extracted.rglob("*") if f.is_file()]
        if ref_dir.is_dir():
            ref_names = [f"input/references/{f.name}" for f in sorted(ref_dir.iterdir())]
    else:
        upload = Path(upload)
        max_upload = cfg["max_upload_mb"] * 1024 * 1024
        if upload.stat().st_size > max_upload:
            msg = f"Upload is larger than the {cfg['max_upload_mb']} MB limit; please upload a smaller file."
            store.event(run_id, "intake_rejected", reason="upload_too_large")
            shutil.rmtree(run_dir / "download", ignore_errors=True)
            return IntakeResult(False, msg)

        shutil.copy2(upload, run_dir / "input" / upload.name)
        source_type = detect(upload)
        if source_type in ("pbip_zip", "pbit", "pbix", "twbx"):
            try:
                files = safe_unzip(upload, extracted, cfg["max_unpacked_mb"] * 1024 * 1024)
            except UnsafeArchive as e:
                shutil.rmtree(extracted, ignore_errors=True)
                store.event(
                    run_id, "intake_rejected", reason="unsafe_archive", detail=str(e)
                )
                if from_tableau:
                    return IntakeResult(False, f"The downloaded Tableau workbook is unsafe: {e}")
                raise
        elif source_type == "twb":
            extracted.mkdir(exist_ok=True)
            files = [Path(shutil.copy2(upload, extracted / upload.name))]

    shutil.rmtree(run_dir / "download", ignore_errors=True)
    for ref in references:
        ref_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(ref, ref_dir / Path(ref).name)
        ref_names.append(f"input/references/{Path(ref).name}")

    model = _model_present(source_type, files)
    (run_dir / "intake.json").write_text(
        json.dumps(
            {
                "run_id": run_id,
                "source_type": source_type,
                "files": sorted(f.relative_to(extracted).as_posix() for f in files),
                "reference_images": ref_names,
                "notes": notes,
                "target_repo": target_repo,
                "model_present": model,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    store.set_meta(run_id, source_type=source_type)
    store.event(run_id, "intake_done", source_type=source_type, model_present=model)

    if source_type == "unknown":
        return IntakeResult(
            False,
            "Unrecognised file. Upload a .pbip zip, .pbit, or Tableau .twb/.twbx.",
        )
    if source_type == "pbix":
        return IntakeResult(False, PBIX_MSG)
    if not model:
        return IntakeResult(
            False,
            "No data model found in the upload (no data sources); please provide the full workbook.",
        )
    return IntakeResult(True)
