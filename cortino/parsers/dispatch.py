from pathlib import Path

from cortino.parsers.common import ModelPart
from cortino.parsers.model_bim import parse_bim
from cortino.parsers.pbi_legacy_layout import parse_layout
from cortino.parsers.pbir import ReportPart, parse_pbir
from cortino.parsers.tableau_twb import parse_twb
from cortino.parsers.tmdl import parse_tmdl
from cortino.spec.models import DashboardSpec, Unknown


def _find(intake_dir: Path, pattern: str) -> Path | None:
    """The PBIP project sits at the root of `extracted/` or one folder below it."""
    for glob in (pattern, f"*/{pattern}"):
        for path in sorted(intake_dir.glob(glob)):
            if path.is_dir():
                return path
    return None


def _find_file(intake_dir: Path, *relative: str) -> Path | None:
    for base in (intake_dir, *sorted(p for p in intake_dir.iterdir() if p.is_dir())):
        for rel in relative:
            if (base / rel).is_file():
                return base / rel
    return None


def _no_report(detail: str) -> Unknown:
    return Unknown(where="report", what="no readable report layout", raw=detail)


def _assemble(
    name: str, source_format: str, model: ModelPart, report: ReportPart
) -> DashboardSpec:
    return DashboardSpec(
        spec_version="0.1",
        name=name,
        source_format=source_format,
        sources=model.sources,
        tables=model.tables,
        relationships=model.relationships,
        measures=model.measures,
        pages=report.pages,
        rls_roles=model.rls_roles,
        unknowns=[*model.unknowns, *report.unknowns],
    )


def _parse_pbip(intake_dir: Path, name: str) -> DashboardSpec:
    model_dir = _find(intake_dir, "*.SemanticModel")
    if model_dir is None:
        raise ValueError(f"no *.SemanticModel folder found in {intake_dir}")
    definition = model_dir / "definition"
    if definition.is_dir() and any(definition.rglob("*.tmdl")):
        model, source_format = parse_tmdl(definition), "pbip_tmdl"
    elif (model_dir / "model.bim").is_file():
        model, source_format = parse_bim(model_dir / "model.bim"), "pbip_bim"
    else:
        raise ValueError(f"no definition/*.tmdl or model.bim in {model_dir}")
    report_dir = _find(intake_dir, "*.Report")
    report = ReportPart()
    if report_dir and (report_dir / "definition").is_dir():
        report = parse_pbir(report_dir)
    elif report_dir and (report_dir / "report.json").is_file():
        report = parse_layout(report_dir / "report.json")
    elif report_dir:
        report.unknowns.append(
            _no_report(f"{report_dir.name} has no definition/ or report.json")
        )
    return _assemble(name, source_format, model, report)


def _parse_pbit(intake_dir: Path, name: str) -> DashboardSpec:
    schema = _find_file(intake_dir, "DataModelSchema")
    if schema is None:
        raise ValueError(f"no DataModelSchema found in {intake_dir}")
    layout = _find_file(intake_dir, "Report/Layout")
    report = ReportPart()
    if layout:
        report = parse_layout(layout)
    else:
        report.unknowns.append(_no_report("no Report/Layout in the .pbit"))
    return _assemble(name, "pbit", parse_bim(schema), report)


def parse(intake_dir: Path, source_type: str, name: str) -> DashboardSpec:
    if source_type == "pbip_zip":
        return _parse_pbip(Path(intake_dir), name)
    if source_type == "pbit":
        return _parse_pbit(Path(intake_dir), name)
    if source_type == "pbix":
        raise ValueError(
            "a .pbix is report-only (no readable data model); re-save it as .pbit or PBIP"
        )
    if source_type in ("twb", "twbx"):
        return parse_twb(Path(intake_dir), source_type, name)
    raise NotImplementedError(f"no parser for source type {source_type!r}")
