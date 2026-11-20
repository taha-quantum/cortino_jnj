"""PBIR (folder-based report definition) parser."""

import json
from dataclasses import dataclass, field
from pathlib import Path

from cortino.parsers.common import (
    SLICER_OPERATORS,
    field_ref,
    normalize_visual_type,
    slicer_mode,
)
from cortino.spec.models import Encoding, Filter, Page, Position, Unknown, Visual

CARTESIAN = {"bar", "column", "line", "area"}
ROLES = {
    "Category": "category",
    "Series": "series",
    "Values": "values",
    "Size": "size",
    "Tooltips": "tooltip",
}
CARTESIAN_ROLES = {**ROLES, "Category": "x", "Y": "y"}
PAGE_SETTINGS = ("visibility", "type", "pageBinding")


@dataclass
class ReportPart:
    pages: list[Page] = field(default_factory=list)
    unknowns: list[Unknown] = field(default_factory=list)


def _read(path: Path) -> tuple[str, dict]:
    text = path.read_text(encoding="utf-8-sig")
    return text, json.loads(text)


def _filter_config(data: dict, where: str, part: ReportPart) -> None:
    config = data.get("filterConfig")
    if isinstance(config, dict) and config.get("filters"):
        part.unknowns.append(
            Unknown(where=where, what="unparsed filterConfig", raw=json.dumps(config))
        )


def _parse_visual(
    folder: Path, page_id: str, part: ReportPart
) -> tuple[Visual, Filter | None]:
    where = f"visual:{folder.name}"
    text, data = _read(folder / "visual.json")
    original = data.get("visual", {}).get("visualType", "")
    vtype = normalize_visual_type(original)
    pos = data.get("position", {})
    has_pos = all(k in pos for k in ("x", "y", "width", "height"))
    visual = Visual(
        id=folder.name,
        page=page_id,
        type=vtype,
        original_type=original,
        position=Position(x=pos["x"], y=pos["y"], w=pos["width"], h=pos["height"])
        if has_pos
        else None,
    )
    _filter_config(data, where, part)
    if vtype == "unknown":
        part.unknowns.append(
            Unknown(where=where, what=f"custom visual {original}", raw=text)
        )
        return visual, None
    query = data["visual"].get("query", {})
    if query.get("sortDefinition"):
        part.unknowns.append(
            Unknown(
                where=where,
                what="unparsed sortDefinition",
                raw=json.dumps(query["sortDefinition"]),
            )
        )
    roles = CARTESIAN_ROLES if vtype in CARTESIAN else ROLES
    for pbir_role, state in query.get("queryState", {}).items():
        for projection in state.get("projections", []):
            ref = field_ref(projection.get("field", {}))
            if pbir_role not in roles or ref is None:
                part.unknowns.append(
                    Unknown(
                        where=where,
                        what=f"unrecognized role or field {pbir_role}",
                        raw=json.dumps(projection),
                    )
                )
                continue
            visual.encodings.append(Encoding(role=roles[pbir_role], field=ref))
    slicer = None
    if vtype == "slicer" and visual.encodings:
        mode = slicer_mode(data["visual"])
        if mode not in SLICER_OPERATORS:
            part.unknowns.append(Unknown(where=where, what=f"slicer mode {mode} read as an in filter", raw=text))
        slicer = Filter(
            level="page",
            field=visual.encodings[0].field,
            operator=SLICER_OPERATORS.get(mode, "in"),
            values=[],
            is_slicer=True,
        )
    return visual, slicer


def _parse_report_level(definition: Path, part: ReportPart) -> None:
    report_json = definition / "report.json"
    if report_json.exists():
        _filter_config(_read(report_json)[1], "report", part)
    extensions = definition / "reportExtensions.json"
    if extensions.exists():
        part.unknowns.append(
            Unknown(
                where="report",
                what="unparsed reportExtensions",
                raw=extensions.read_text(encoding="utf-8-sig"),
            )
        )
    bookmarks = definition / "bookmarks"
    for path in sorted(bookmarks.glob("*.json")) if bookmarks.is_dir() else []:
        if path.name != "bookmarks.json":
            part.unknowns.append(
                Unknown(
                    where="report",
                    what=f"unparsed bookmark {path.stem}",
                    raw=path.read_text(encoding="utf-8-sig"),
                )
            )


def parse_pbir(report_dir: Path) -> ReportPart:
    part = ReportPart()
    definition = report_dir / "definition"
    _parse_report_level(definition, part)
    pages_dir = definition / "pages"
    folders = (
        sorted(p.name for p in pages_dir.iterdir() if p.is_dir())
        if pages_dir.is_dir()
        else []
    )
    order: list[str] = []
    pages_json = pages_dir / "pages.json"
    if pages_json.exists():
        order = _read(pages_json)[1].get("pageOrder", [])
    elif folders:
        part.unknowns.append(
            Unknown(where="report", what="missing pages.json; page order lost", raw="")
        )
    for missing in (p for p in order if p not in folders):
        part.unknowns.append(
            Unknown(where="report", what=f"pageOrder entry without folder: {missing}")
        )
    for page_id in [p for p in order if p in folders] + [
        p for p in folders if p not in order
    ]:
        where = f"page:{page_id}"
        page_json = pages_dir / page_id / "page.json"
        data: dict = {}
        if page_json.exists():
            data = _read(page_json)[1]
        else:
            part.unknowns.append(Unknown(where=where, what="missing page.json"))
        page = Page(id=page_id, name=data.get("displayName", page_id))
        _filter_config(data, where, part)
        for key in PAGE_SETTINGS:
            if key in data:
                part.unknowns.append(
                    Unknown(
                        where=where,
                        what=f"unparsed page {key}",
                        raw=json.dumps(data[key]),
                    )
                )
        visuals_dir = pages_dir / page_id / "visuals"
        visual_folders = sorted(visuals_dir.iterdir()) if visuals_dir.is_dir() else []
        for folder in (f for f in visual_folders if f.is_dir()):
            if not (folder / "visual.json").exists():
                part.unknowns.append(
                    Unknown(where=f"visual:{folder.name}", what="missing visual.json")
                )
                continue
            visual, slicer = _parse_visual(folder, page_id, part)
            page.visuals.append(visual)
            if slicer:
                page.filters.append(slicer)
        part.pages.append(page)
    return part
