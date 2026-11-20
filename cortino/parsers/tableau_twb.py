"""Tableau workbook (TWB, or the TWB inside an unpacked TWBX) -> DashboardSpec.

Anything that is not mapped goes to `unknowns`; nothing is dropped silently.
"""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from xml.parsers import expat

from cortino.spec.models import (
    Column,
    DashboardSpec,
    Encoding,
    FieldRef,
    Filter,
    Measure,
    Page,
    Position,
    Relationship,
    Source,
    Table,
    Unknown,
    Visual,
)

_REF = re.compile(
    r"^\[[^\]]*\]\.\[(?:(?P<derivation>[A-Za-z]+):)?(?P<field>.+?):(?P<type>[a-z]{2})\]$"
)
_SHELF_REF = re.compile(r"\[[^\]]+\]\.\[[^\]]+\]")
_USER_FUNCTION = re.compile(
    r"\b(USERNAME|USERDOMAIN|FULLNAME|ISMEMBEROF|USER)\s*\(", re.IGNORECASE
)
# Tableau column-instance derivation tokens that are real aggregations.
_AGGREGATIONS = {
    "sum": "sum", "avg": "avg", "cnt": "count", "ctd": "distinctcount", "min": "min", "max": "max",
}  # fmt: skip
# Derivations that leave the field as it is. Anything else (date parts, attr, med, ...) changes
# its meaning, so it is reported rather than dropped.
_PLAIN_DERIVATIONS = {None, "none", "usr"}
_DATA_TYPES = {
    "integer": "int64", "real": "decimal", "date": "dateTime", "datetime": "dateTime",
    "string": "string", "boolean": "boolean",
}  # fmt: skip
_SOURCE_KINDS = {
    "sqlserver": "sqlserver", "snowflake": "snowflake", "postgres": "postgres",
    "textscan": "csv", "excel-direct": "excel",
}  # fmt: skip
_ENCODING_ROLES = {
    "text": "values",
    "color": "series",
    "size": "size",
    "tooltip": "tooltip",
}
# Dashboard zones that are layout or decoration, not data.
_PLAIN_ZONES = {
    "layout-basic",
    "layout-flow",
    "text",
    "title",
    "empty",
    "color",
    "size",
}
# Datasource children handled elsewhere or without data meaning.
_BENIGN_DS_CHILDREN = {
    "connection", "column", "layout", "semantic-values", "object-graph",
    "repository-location", "date-options",
}  # fmt: skip


@dataclass
class SplitRef:
    derivation: str | None
    field: str
    type: str

    @property
    def aggregation(self) -> str | None:
        return _AGGREGATIONS.get(self.derivation or "")

    @property
    def supported(self) -> bool:
        return self.derivation in _PLAIN_DERIVATIONS or self.derivation in _AGGREGATIONS


def parse_field_ref(ref: str) -> SplitRef | None:
    """`[federated.x].[sum:amount:qk]` -> derivation `sum`, field `amount`, type `qk`."""
    match = _REF.match(ref)
    if not match:
        return None
    derivation = match["derivation"]
    return SplitRef(
        derivation.lower() if derivation else None, match["field"], match["type"]
    )


def _strip(name: str | None) -> str:
    return (name or "").strip().strip("[]")


def _raw(element: ET.Element) -> str:
    return ET.tostring(element, encoding="unicode")


def _unknown(where: str, what: str, element: ET.Element | None = None) -> Unknown:
    return Unknown(
        where=where, what=what, raw=_raw(element) if element is not None else ""
    )


def _find_twb(intake_dir: Path) -> Path:
    for pattern in ("*.twb", "*/*.twb"):
        for path in sorted(intake_dir.glob(pattern)):
            return path
    raise ValueError(f"no .twb file found in {intake_dir}")


def _load(path: Path) -> ET.Element:
    """Parse with expat handlers that refuse DOCTYPE and entity declarations, whatever the encoding."""

    def refuse(*_args):
        raise ValueError(
            f"{path.name}: refusing a workbook with a DOCTYPE or ENTITY declaration"
        )

    data = path.read_bytes()
    scanner = expat.ParserCreate()
    scanner.StartDoctypeDeclHandler = refuse
    scanner.EntityDeclHandler = refuse
    try:
        scanner.Parse(data, True)
        return ET.fromstring(data)
    except (expat.ExpatError, ET.ParseError) as exc:
        raise ValueError(f"{path.name}: not valid XML ({exc})") from exc


def _operand(op: str | None) -> tuple[str, str] | None:
    parts = [_strip(p) for p in (op or "").split("].[")]
    return (parts[0], parts[1]) if len(parts) == 2 and all(parts) else None


def _relations(element: ET.Element) -> list[ET.Element]:
    """`<relation>` elements, including Tableau 2020.2+ wrappers like `_.fcp...false...relation`."""
    return [
        e
        for e in element.iter()
        if e.tag == "relation" or e.tag.endswith("...relation")
    ]


class _Model:
    """What one datasource defines: tables, columns, calculated fields."""

    def __init__(self, ds: ET.Element, unknowns: list[Unknown]):
        self.sources: list[Source] = []
        self.tables: list[Table] = []
        self.relationships: list[Relationship] = []
        self.measures: list[Measure] = []
        self.local: dict[str, tuple[str, str]] = {}  # local name -> (table, column)
        self.dtypes: dict[tuple[str, str], str] = {}
        self.roles: dict[str, str] = {}
        self.calcs: dict[str, str] = {}  # internal name -> caption
        self.calc_roles: dict[str, str] = {}
        self._read_tables(ds, unknowns)
        if self.tables:
            self._read_columns(ds, unknowns)
        self._read_children(ds, unknowns)

    def _add_relationship(self, ft: str, fc: str, tt: str, tc: str) -> None:
        rel = Relationship(
            from_table=ft, from_column=fc, to_table=tt, to_column=tc,
            cardinality="many_to_one", cross_filter="single",
        )  # fmt: skip
        if rel not in self.relationships:
            self.relationships.append(rel)

    def _read_tables(self, ds: ET.Element, unknowns: list[Unknown]) -> None:
        where = "twb:datasource"
        connection = ds.find("connection")
        named = {
            nc.get("name"): nc.find("connection")
            for nc in connection.findall("named-connections/named-connection")
        }
        relations = _relations(connection)
        by_name: dict[str, Table] = {}
        for rel in relations:
            kind, name = rel.get("type"), rel.get("name")
            if kind in ("join", "collection"):
                continue
            if kind not in ("table", "text") or not name:
                unknowns.append(_unknown(where, f"relation type {kind}", rel))
                continue
            if name in by_name:  # the 2020.2+ object model repeats every table
                continue
            conn = named.get(rel.get("connection"))
            conn = conn if conn is not None else connection
            common = {
                "id": name,
                "kind": _SOURCE_KINDS.get(conn.get("class"), "other"),
                "server": conn.get("server"),
                "database": conn.get("dbname"),
            }
            if kind == "text":
                source = Source(**common, query=(rel.text or "").strip())
            else:
                parts = [_strip(p) for p in rel.get("table", "").split("].[")]
                source = Source(
                    **common,
                    schema=parts[0] if len(parts) > 1 else None,
                    table=parts[-1],
                )
            self.sources.append(source)
            by_name[name] = Table(name=name, source_id=name)
            self.tables.append(by_name[name])

        reported: set[str] = set()
        for rec in connection.findall(
            "metadata-records/metadata-record[@class='column']"
        ):
            table, remote = (
                _strip(rec.findtext("parent-name")),
                rec.findtext("remote-name"),
            )
            if table not in by_name or not remote:
                if table not in reported:
                    reported.add(table)
                    unknowns.append(
                        _unknown(where, f"columns of unmodelled relation {table}", rec)
                    )
                continue
            local_type = rec.findtext("local-type")
            data_type = _DATA_TYPES.get(local_type, local_type or "unknown")
            by_name[table].columns.append(
                Column(name=remote, data_type=data_type, source_column=remote)
            )
            self.dtypes[(table, remote)] = data_type
            self.local[_strip(rec.findtext("local-name") or remote)] = (table, remote)

        for join in relations:
            if join.get("type") != "join":
                continue
            if join.get("join", "inner") != "inner":
                unknowns.append(_unknown(where, f"join type {join.get('join')}", join))
            for clause in join.findall("clause"):
                expr = clause.find("expression")
                ends = (
                    [e.get("op") for e in expr.findall("expression")]
                    if expr is not None
                    else []
                )
                pair = [_operand(e) for e in ends]
                if (
                    expr is None
                    or expr.get("op") != "="
                    or len(pair) != 2
                    or None in pair
                ):
                    unknowns.append(_unknown(where, "unsupported join clause", clause))
                else:
                    self._add_relationship(*pair[0], *pair[1])
        self._read_object_graph(ds, unknowns)

    def _read_object_graph(self, ds: ET.Element, unknowns: list[Unknown]) -> None:
        graph = ds.find("object-graph")
        if graph is None:
            return
        names = {}
        for obj in graph.findall("objects/object"):
            rel = obj.find(".//relation")
            names[obj.get("id")] = (
                rel.get("name") if rel is not None else obj.get("caption")
            )
        for rel in graph.findall("relationships/relationship"):
            expr = rel.find("expression")
            ops = (
                [e.get("op") for e in expr.findall("expression")]
                if expr is not None
                else []
            )
            first, second = rel.find("first-end-point"), rel.find("second-end-point")
            cols = [self.local.get(_strip(op)) for op in ops]
            if (
                expr is None or expr.get("op") != "=" or len(cols) != 2 or None in cols
                or first is None or second is None
                or first.get("object-id") not in names or second.get("object-id") not in names
            ):  # fmt: skip
                unknowns.append(
                    _unknown("twb:datasource", "unsupported relationship", rel)
                )
                continue
            self._add_relationship(
                names[first.get("object-id")],
                cols[0][1],
                names[second.get("object-id")],
                cols[1][1],
            )

    def _read_columns(self, ds: ET.Element, unknowns: list[Unknown]) -> None:
        where = "twb:datasource"
        table = self.tables[0].name
        calcs = []
        for col in ds.findall("column"):
            name = _strip(col.get("name"))
            self.roles[name] = col.get("role", "dimension")
            calc = col.find("calculation")
            if calc is None:
                continue
            if calc.get("class") != "tableau":
                unknowns.append(
                    _unknown(where, f"calculation class {calc.get('class')}", col)
                )
                continue
            caption = col.get("caption") or name
            self.calcs[name] = caption
            self.calc_roles[name] = col.get("role", "measure")
            calcs.append((name, caption, calc.get("formula", ""), col))
        for name, caption, formula, col in calcs:
            if _USER_FUNCTION.search(formula):
                unknowns.append(
                    _unknown(
                        where,
                        f"calculation {caption} uses a user function (row-level security?)",
                        col,
                    )
                )
            if self.calc_roles[name] != "measure":
                unknowns.append(_unknown(where, f"calculated dimension {caption}", col))
                continue
            refs = dict.fromkeys(
                self.calcs[r] for r in re.findall(r"\[([^\]]+)\]", formula)
                if r in self.calcs and r != name
            )  # fmt: skip
            self.measures.append(
                Measure(name=caption, table=table, expression=formula, language="tableau", depends_on=list(refs))
            )  # fmt: skip

    def _read_children(self, ds: ET.Element, unknowns: list[Unknown]) -> None:
        for child in ds:
            if child.tag in _BENIGN_DS_CHILDREN or (
                child.tag == "aliases" and len(child) == 0
            ):
                continue
            if child.tag == "filter":
                what = "datasource filter"
            elif child.tag == "group":
                what = f"group {child.get('name')}"
            else:
                what = f"datasource element {child.tag}"
            unknowns.append(_unknown("twb:datasource", what, child))

    def resolve(self, ref: str) -> tuple[FieldRef, bool] | None:
        """FieldRef and whether it is a measure; None when the reference is not understood."""
        split = parse_field_ref(ref)
        if split is None or not split.supported:
            return None
        if split.field in self.calcs:
            is_measure = self.calc_roles[split.field] == "measure"
            return FieldRef(
                table=self.tables[0].name,
                name=self.calcs[split.field],
                kind="measure" if is_measure else "column",
                aggregation=split.aggregation,
            ), is_measure or split.aggregation is not None
        found = self.local.get(split.field)
        if found is None:
            return None
        table, column = found
        is_measure = (
            split.aggregation is not None or self.roles.get(split.field) == "measure"
        )
        return FieldRef(
            table=table, name=column, kind="column", aggregation=split.aggregation
        ), is_measure

    def is_date(self, field: FieldRef) -> bool:
        return self.dtypes.get((field.table, field.name)) == "dateTime"


def _filter_values(flt: ET.Element) -> list[str] | None:
    """Members kept by a categorical filter; [] means no restriction, None means not mapped."""
    root = flt.find("groupfilter")
    if root is None:
        return None
    function = root.get("function")
    if function == "level-members":
        return []
    members = [root] if function == "member" else []
    if function == "union":
        members = root.findall("groupfilter")
        if not members or any(m.get("function") != "member" for m in members):
            return None
    return [m.get("member", "").strip('"') for m in members] if members else None


def _worksheet_visual(
    ws: ET.Element,
    vid: str,
    page: str,
    position: Position | None,
    model: _Model,
    unknowns: list[Unknown],
) -> Visual:
    where = f"twb:worksheet:{ws.get('name')}"
    table = ws.find("table")
    if table is None:
        unknowns.append(_unknown(where, "worksheet without a table", ws))
        return Visual(id=vid, page=page, type="unknown", position=position)
    for instance in table.findall("view/datasource-dependencies/column-instance"):
        calc = instance.find("table-calc")
        # The spec keeps only a calc's formula, read with Table (across) addressing: say so for any other.
        if calc is not None and (dict(calc.attrib) != {"ordering-type": "Columns"} or len(calc)):
            unknowns.append(_unknown(
                where, f"table-calc addressing {calc.get('ordering-type')} on {instance.get('name')}", instance,
            ))  # fmt: skip
    panes = table.findall("panes/pane")
    if len(panes) > 1:
        unknowns.append(_unknown(where, "multiple panes (dual axis)", table))
    mark = panes[0].find("mark") if panes else None
    mark_class = mark.get("class") if mark is not None else "Automatic"
    by_shelf: dict[str, list[tuple[FieldRef, bool]]] = {"rows": [], "cols": []}
    for tag, found in by_shelf.items():
        for ref in _SHELF_REF.findall(table.findtext(tag) or ""):
            resolved = model.resolve(ref)
            if resolved is None:
                unknowns.append(
                    Unknown(
                        where=where,
                        what=f"unresolved or unsupported field {ref}",
                        raw=ref,
                    )
                )
            else:
                found.append(resolved)
    fields = by_shelf["rows"] + by_shelf["cols"]
    dims = [f for f, is_measure in fields if not is_measure]
    measures = [f for f, is_measure in fields if is_measure]
    dim_on_rows = any(not is_measure for _, is_measure in by_shelf["rows"])

    if (
        mark_class == "Automatic"
    ):  # Tableau picks the mark from the shelves; mirror it, but flag it
        if not fields:
            vtype = "card"
        elif any(model.is_date(f) for f in dims):
            vtype = "line"
        else:
            vtype = "bar" if dim_on_rows else "column"
        unknowns.append(
            _unknown(where, f"mark class Automatic (inferred {vtype})", table)
        )
    elif mark_class == "Bar":
        vtype = "bar" if dim_on_rows else "column"
    elif mark_class in ("Line", "Area"):
        vtype = mark_class.lower()
    elif mark_class == "Text":
        vtype = "table" if dims else "card"
    else:
        vtype = "unknown"
        unknowns.append(_unknown(where, f"mark class {mark_class}", table))

    encodings: list[Encoding] = []
    if vtype != "unknown":
        if vtype in ("table", "card"):
            encodings = [Encoding(role="values", field=f) for f, _ in fields]
        else:
            encodings = [Encoding(role="x", field=f) for f in dims] + [
                Encoding(role="y", field=f) for f in measures
            ]
        for enc in panes[0].findall("encodings/*") if panes else []:
            role = _ENCODING_ROLES.get(enc.tag)
            resolved = model.resolve(enc.get("column", ""))
            if role is None or resolved is None:
                unknowns.append(_unknown(where, f"encoding {enc.tag}", enc))
            else:
                encodings.append(Encoding(role=role, field=resolved[0]))

    filters = []
    for flt in table.findall("view/filter"):
        resolved = model.resolve(flt.get("column", ""))
        values = _filter_values(flt) if flt.get("class") == "categorical" else None
        if resolved is None or values is None:
            unknowns.append(
                _unknown(where, f"filter class {flt.get('class')} not mapped", flt)
            )
        elif values:  # an empty list is "all members": no restriction
            filters.append(
                Filter(level="visual", field=resolved[0], operator="in", values=values)
            )

    return Visual(
        id=vid, page=page, type=vtype, original_type=mark_class, position=position,
        encodings=encodings, filters=filters,
    )  # fmt: skip


def _zone_position(
    zone: ET.Element, width: float | None, height: float | None
) -> Position | None:
    if width is None or height is None:
        return None
    try:
        x, y, w, h = (int(zone.get(a)) * size / 100000 for a, size in
                      (("x", width), ("y", height), ("w", width), ("h", height)))  # fmt: skip
    except (TypeError, ValueError):
        return None
    return Position(x=round(x, 2), y=round(y, 2), w=round(w, 2), h=round(h, 2))


def _dashboard_size(size: ET.Element | None) -> tuple[float, float] | None:
    try:
        return float(size.get("maxwidth")), float(size.get("maxheight"))
    except (AttributeError, TypeError, ValueError):
        return None


def _page(
    dash: ET.Element,
    worksheets: dict[str, ET.Element],
    model: _Model,
    unknowns: list[Unknown],
    used: set[str],
) -> Page:
    name = dash.get("name") or "dashboard"
    where = f"twb:dashboard:{name}"
    size = _dashboard_size(dash.find("size"))
    width, height = size if size else (None, None)
    if size is None:
        unknowns.append(
            _unknown(
                where,
                "dashboard size is not fixed; positions unknown",
                dash.find("size"),
            )
        )
    page = Page(id=name, name=name)
    ids: set[str] = set()

    def unique(vid: str) -> str:
        while vid in ids:
            vid += "_"
        ids.add(vid)
        return vid

    def walk(zone: ET.Element) -> None:
        kind = zone.get("type-v2")
        zname = zone.get("name")
        if kind in _PLAIN_ZONES or (kind is None and zname is None):
            for child in zone.findall("zone"):
                walk(child)
        elif kind == "filter":
            resolved = model.resolve(zone.get("param", ""))
            if resolved is None:
                unknowns.append(_unknown(where, "filter zone", zone))
                return
            field = resolved[0]
            split = parse_field_ref(zone.get("param", ""))
            # A continuous (quantitative) field's filter is a range; a discrete one picks members.
            operator = "between" if split and split.type.startswith("q") else "in"
            page.filters.append(
                Filter(
                    level="page", field=field, operator=operator, values=[], is_slicer=True
                )
            )
            page.visuals.append(
                Visual(
                    id=unique(f"slicer_{field.name}"), page=name, type="slicer", original_type="filter",
                    position=_zone_position(zone, width, height),
                    encodings=[Encoding(role="values", field=field)],
                )
            )  # fmt: skip
        elif kind is None and zname in worksheets:
            used.add(zname)
            page.visuals.append(
                _worksheet_visual(
                    worksheets[zname], unique(zname), name,
                    _zone_position(zone, width, height), model, unknowns,
                )
            )  # fmt: skip
        else:
            unknowns.append(_unknown(where, f"zone {kind or zname}", zone))

    for top in dash.findall("zones/zone"):
        walk(top)
    return page


def parse_twb(intake_dir: Path, source_format: str, name: str) -> DashboardSpec:
    path = _find_twb(Path(intake_dir))
    root = _load(path)
    unknowns: list[Unknown] = []
    datasources = root.findall("datasources/datasource")
    data = [d for d in datasources if d.get("name") != "Parameters"]
    if not data:
        raise ValueError(f"{path.name}: no data source found")
    if data[0].find("connection") is None:
        raise ValueError(
            f"{path.name}: data source {data[0].get('name')} has no connection"
        )
    for ds in datasources:
        if ds is not data[0]:
            unknowns.append(
                _unknown(
                    "twb:datasource", f"datasource {ds.get('name')} not modelled", ds
                )
            )
    model = _Model(data[0], unknowns)
    if not model.tables:
        raise ValueError(
            f"{path.name}: data source {data[0].get('name')} has no tables"
        )
    worksheets = {w.get("name"): w for w in root.findall("worksheets/worksheet")}

    pages, used = [], set()
    for dash in root.findall("dashboards/dashboard"):
        if dash.get("type") == "storyboard":
            unknowns.append(
                _unknown(f"twb:dashboard:{dash.get('name')}", "story", dash)
            )
        else:
            pages.append(_page(dash, worksheets, model, unknowns, used))
    for wname, ws in worksheets.items():
        if wname not in used:
            unknowns.append(
                _unknown(f"twb:worksheet:{wname}", "worksheet not on a dashboard", ws)
            )
    for action in root.findall("actions/action"):
        source = action.find("source")
        dash = source.get("dashboard") if source is not None else None
        unknowns.append(
            _unknown(
                f"twb:dashboard:{dash}" if dash else "twb:dashboard",
                f"action {action.get('caption') or action.get('name')}",
                action,
            )
        )

    return DashboardSpec(
        spec_version="0.1",
        name=name,
        source_format=source_format,
        sources=model.sources,
        tables=model.tables,
        relationships=model.relationships,
        measures=model.measures,
        pages=pages,
        unknowns=unknowns,
    )
