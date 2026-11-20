"""Legacy Power BI report layout (`Report/Layout` in .pbit, `report.json` in old PBIP)."""

import json
from pathlib import Path
from typing import Any

from cortino.parsers.common import (
    field_ref,
    normalize_visual_type,
    read_json,
    slicer_mode,
)
from cortino.parsers.pbir import CARTESIAN, CARTESIAN_ROLES, ROLES, ReportPart
from cortino.spec.models import Encoding, Filter, Page, Position, Unknown, Visual


def _loads(value: Any) -> Any:
    """Layout stores nested JSON as strings (config, filters, query); None if unreadable."""
    if isinstance(value, str):
        return json.loads(value) if value.strip() else None
    return value


def _resolve(node: Any, aliases: dict[str, str]) -> Any:
    """Replace prototypeQuery `Source` aliases with the PBIR-style `Entity` name."""
    if isinstance(node, dict):
        if "Source" in node and set(node) == {"Source"}:
            if node["Source"] not in aliases:
                raise KeyError(
                    node["Source"]
                )  # unresolved alias: caller records an Unknown
            return {"Entity": aliases[node["Source"]]}
        return {k: _resolve(v, aliases) for k, v in node.items()}
    if isinstance(node, list):
        return [_resolve(v, aliases) for v in node]
    return node


def _filters(value: Any, where: str, part: ReportPart) -> None:
    try:
        filters = _loads(value)
    except ValueError:
        filters = value
    if filters:
        part.unknowns.append(
            Unknown(where=where, what="unparsed filters", raw=json.dumps(filters))
        )


def _parse_visual(
    container: dict, index: int, page_id: str, part: ReportPart
) -> tuple[Visual, Filter | None]:
    fallback = f"{page_id}-visual{index}"
    raw_config = container.get("config", "")
    pos = [container.get(k) for k in ("x", "y", "width", "height")]
    position = (
        Position(x=pos[0], y=pos[1], w=pos[2], h=pos[3])
        if all(isinstance(p, (int, float)) for p in pos)
        else None
    )
    try:
        config = _loads(raw_config) or {}
        single = config.get("singleVisual") or {}
        if not isinstance(single, dict):
            raise TypeError("singleVisual is not an object")
    except (ValueError, AttributeError, TypeError):
        part.unknowns.append(
            Unknown(
                where=f"visual:{fallback}",
                what="unreadable visual config",
                raw=str(raw_config),
            )
        )
        visual = Visual(id=fallback, page=page_id, type="unknown", position=position)
        return visual, None
    vid = config.get("name") or fallback
    where = f"visual:{vid}"
    original = single.get("visualType", "")
    vtype = normalize_visual_type(original)
    visual = Visual(
        id=vid, page=page_id, type=vtype, original_type=original, position=position
    )
    _filters(container.get("filters"), where, part)
    if vtype == "unknown":
        part.unknowns.append(
            Unknown(
                where=where,
                what=f"custom visual {original}" if original else "visual without type",
                raw=str(raw_config),
            )
        )
        return visual, None
    query = single.get("prototypeQuery") or {}
    if query.get("OrderBy"):
        part.unknowns.append(
            Unknown(
                where=where,
                what="unparsed sortDefinition",
                raw=json.dumps(query["OrderBy"]),
            )
        )
    aliases = {
        f["Name"]: f["Entity"]
        for f in query.get("From", [])
        if isinstance(f, dict) and "Name" in f and "Entity" in f
    }
    selects = {s.get("Name"): s for s in query.get("Select", []) if isinstance(s, dict)}
    roles = CARTESIAN_ROLES if vtype in CARTESIAN else ROLES
    projections = single.get("projections")
    for role, items in (projections if isinstance(projections, dict) else {}).items():
        for item in items if isinstance(items, list) else []:
            select = (
                selects.get(item.get("queryRef")) if isinstance(item, dict) else None
            )
            try:
                ref = field_ref(_resolve(select, aliases)) if select else None
            except KeyError:
                ref = None
            if role not in roles or ref is None:
                part.unknowns.append(
                    Unknown(
                        where=where,
                        what=f"unrecognized role or field {role}",
                        raw=json.dumps(item),
                    )
                )
                continue
            visual.encodings.append(Encoding(role=roles[role], field=ref))
    slicer = None
    if vtype == "slicer" and visual.encodings:
        mode = slicer_mode(single)
        if mode not in (None, "Basic", "Dropdown"):  # only member slicers are read here
            part.unknowns.append(Unknown(where=where, what=f"slicer mode {mode} read as an in filter",
                                         raw=str(raw_config)))  # fmt: skip
        slicer = Filter(
            level="page",
            field=visual.encodings[0].field,
            operator="in",
            values=[],
            is_slicer=True,
        )
    return visual, slicer


def parse_layout(path: Path) -> ReportPart:
    try:
        return _parse_layout(path)
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        raise ValueError(f"{path.name}: malformed report layout ({e!r})") from e


def _parse_layout(path: Path) -> ReportPart:
    part = ReportPart()
    _, layout = read_json(path)
    _filters(layout.get("filters"), "report", part)
    if layout.get("bookmarks"):
        part.unknowns.append(
            Unknown(
                where="report",
                what="unparsed bookmarks",
                raw=json.dumps(layout["bookmarks"]),
            )
        )
    for index, section in enumerate(layout.get("sections", [])):
        page_id = section.get("name") or f"section{index}"
        where = f"page:{page_id}"
        page = Page(id=page_id, name=section.get("displayName") or page_id)
        _filters(section.get("filters"), where, part)
        built = [
            _parse_visual(c, i, page_id, part)
            for i, c in enumerate(section.get("visualContainers", []))
        ]
        for visual, slicer in sorted(built, key=lambda pair: pair[0].id):
            page.visuals.append(visual)
            if slicer:
                page.filters.append(slicer)
        part.pages.append(page)
    return part
