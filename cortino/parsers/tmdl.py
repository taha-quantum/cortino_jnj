"""TMDL (indentation-based) semantic model parser."""

import re
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

from cortino.parsers.common import ModelPart, fill_depends_on, source_from_m
from cortino.spec.models import (
    Column,
    Measure,
    Relationship,
    RlsRole,
    Table,
    Unknown,
)

# Keywords that carry no semantics we need; anything else is recorded as an Unknown.
TOP_LEVEL = {
    "table",
    "relationship",
    "role",
    "model",
    "database",
    "ref",
    "annotation",
    "culture",
}
TABLE_CHILDREN = {
    "column", "measure", "partition", "annotation", "lineageTag", "dataCategory", "isHidden",
    "description", "extendedProperty", "changedProperty", "isPrivate",
    "showAsVariationsOnly", "excludeFromModelRefresh", "sourceLineageTag",
    "queryGroup", "isDefaultLabel", "isDefaultImage", "systemManaged",
}  # fmt: skip
ROLE_CHILDREN = {"tablePermission", "modelPermission", "annotation", "description"}


@dataclass
class Node:
    """A header line plus the deeper (or blank) lines that follow it."""

    raw: str
    indent: int
    body: list[str] = field(default_factory=list)

    @property
    def header(self) -> str:
        return self.raw.strip()

    @property
    def keyword(self) -> str:
        match = re.match(r"[\w.]+", self.header)
        return match.group(0) if match else ""

    @property
    def rest(self) -> str:
        return self.header[len(self.keyword) :]

    @property
    def text(self) -> str:
        return "\n".join([self.raw, *self.body]).rstrip()


def _indent(line: str) -> int:
    expanded = line.expandtabs(4)
    return len(expanded) - len(expanded.lstrip())


def _split(lines: list[str]) -> list[Node]:
    lines = [line for line in lines if not line.strip().startswith("///")]
    nonblank = [line for line in lines if line.strip()]
    if not nonblank:
        return []
    base = min(_indent(line) for line in nonblank)
    nodes: list[Node] = []
    for line in lines:
        if line.strip() and _indent(line) == base:
            nodes.append(Node(line, base))
        elif nodes:
            nodes[-1].body.append(line)
    return nodes


def _name_and_expr(rest: str) -> tuple[str, str | None]:
    """Split `'Quoted ''name''' = expr` / `name = expr` into (name, expr or None)."""
    rest = rest.strip()
    if rest.startswith("'"):
        i = 1
        while i < len(rest):
            if rest[i] == "'":
                if rest[i + 1 : i + 2] == "'":
                    i += 2
                    continue
                break
            i += 1
        name, tail = rest[1:i].replace("''", "'"), rest[i + 1 :].strip()
    else:
        match = re.match(r"[^\s=]+", rest)
        name = match.group(0) if match else ""
        tail = rest[len(name) :].strip()
    return name, (tail[1:].strip() if tail.startswith("=") else None)


def _dedent(lines: list[str]) -> str:
    return textwrap.dedent("\n".join(lines)).strip()


def _expression(
    node: Node, inline: str | None, unit: int, whole_body: bool = False
) -> str:
    """Inline expression plus continuation lines (deeper than the node's own properties)."""
    limit = -1 if whole_body else node.indent + unit
    cont = _dedent(
        [line for line in node.body if not line.strip() or _indent(line) > limit]
    )
    text = "\n".join(part for part in (inline, cont) if part).strip()
    if text.startswith("```") and text.endswith("```"):
        text = textwrap.dedent(text[3:-3].strip("\n")).strip()
    return text


def _properties(node: Node) -> dict[str, str]:
    props = {}
    for child in _split(node.body):
        match = re.match(r"(\w+)\s*:\s*(.*)$", child.header)
        if match:
            props[match.group(1)] = match.group(2).strip()
    return props


def _column_ref(text: str) -> tuple[str, str]:
    parts = re.findall(r"'((?:[^']|'')*)'|([^.']+)", text.strip())
    names = [quoted.replace("''", "'") if quoted else bare for quoted, bare in parts]
    return names[0], names[1]


def _unknown(file: str, what: str, raw: str) -> Unknown:
    return Unknown(where=f"tmdl:{file}", what=what, raw=raw)


def _parse_table(node: Node, unit: int, file: str, part: ModelPart) -> Table:
    name, _ = _name_and_expr(node.rest)
    table = Table(name=name)
    for child in _split(node.body):
        keyword = child.keyword
        if keyword == "column":
            col_name, inline = _name_and_expr(child.rest)
            props = _properties(child)
            table.columns.append(
                Column(
                    name=col_name,
                    data_type=props.get("dataType", ""),
                    source_column=props.get("sourceColumn"),
                )
            )
            if inline is not None:
                expr = _expression(child, inline, unit)
                part.unknowns.append(_unknown(file, "calculated column", expr))
        elif keyword == "measure":
            m_name, inline = _name_and_expr(child.rest)
            part.measures.append(
                Measure(
                    name=m_name,
                    table=name,
                    expression=_expression(child, inline, unit),
                    language="dax",
                )
            )
        elif keyword == "partition":
            kind = re.search(r"=\s*(\w+)\s*$", child.header)
            if not kind or kind.group(1) != "m":
                part.unknowns.append(_unknown(file, "non-M partition", child.text))
                continue
            for sub in _split(child.body):
                if sub.keyword == "source":
                    inline = (
                        sub.header.split("=", 1)[1].strip()
                        if "=" in sub.header
                        else None
                    )
                    part.sources.append(
                        source_from_m(
                            name, _expression(sub, inline, unit, whole_body=True)
                        )
                    )
                    table.source_id = name
        elif keyword not in TABLE_CHILDREN:
            part.unknowns.append(_unknown(file, f"unrecognized {keyword}", child.text))
    return table


def _parse_relationship(
    node: Node, file: str
) -> tuple[Relationship | None, list[Unknown]]:
    props = _properties(node)
    try:
        from_table, from_column = _column_ref(props["fromColumn"])
        to_table, to_column = _column_ref(props["toColumn"])
    except (KeyError, IndexError):
        return None, [_unknown(file, "relationship without usable columns", node.text)]
    ends = (props.get("fromCardinality", "many"), props.get("toCardinality", "one"))
    if ends == ("one", "many"):
        from_table, from_column, to_table, to_column = (
            to_table,
            to_column,
            from_table,
            from_column,
        )
        ends = ("many", "one")
    cardinality = {
        ("many", "one"): "many_to_one",
        ("one", "one"): "one_to_one",
        ("many", "many"): "many_to_many",
    }.get(ends)
    if cardinality is None:
        return None, [
            _unknown(file, "relationship with unknown cardinality", node.text)
        ]
    unknowns = []
    if props.get("isActive", "true").lower() == "false":
        unknowns.append(_unknown(file, "inactive relationship", node.text))
    both = props.get("crossFilteringBehavior") == "bothDirections"
    return (
        Relationship(
            from_table=from_table,
            from_column=from_column,
            to_table=to_table,
            to_column=to_column,
            cardinality=cardinality,
            cross_filter="both" if both else "single",
        ),
        unknowns,
    )


def _parse_role(
    node: Node, unit: int, file: str
) -> tuple[list[RlsRole], list[Unknown]]:
    role, _ = _name_and_expr(node.rest)
    roles, unknowns = [], []
    for child in _split(node.body):
        if child.keyword == "tablePermission":
            table, inline = _name_and_expr(child.rest)
            roles.append(
                RlsRole(
                    name=role,
                    table=table,
                    filter_expression=_expression(child, inline, unit),
                )
            )
            for line in child.body:
                if _indent(line) == child.indent + unit and line.strip().startswith(
                    "columnPermission"
                ):
                    unknowns.append(
                        _unknown(file, "column-level security", line.strip())
                    )
        elif child.keyword not in ROLE_CHILDREN:
            unknowns.append(_unknown(file, f"unrecognized {child.keyword}", child.text))
    if not roles:
        unknowns.append(
            _unknown(file, f"role {role} has no tablePermission", node.text)
        )
    return roles, unknowns


def parse_tmdl(definition_dir: Path) -> ModelPart:
    part = ModelPart()
    tables: dict[str, Table] = {}
    order: list[str] = []
    for path in sorted(definition_dir.rglob("*.tmdl")):
        if "cultures" in path.relative_to(definition_dir).parts:
            continue
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        unit = min(
            (_indent(ln) for ln in lines if ln.strip() and _indent(ln) > 0), default=4
        )
        for node in _split(lines):
            keyword = node.keyword
            if keyword == "table":
                table = _parse_table(node, unit, path.name, part)
                tables[table.name] = table
            elif keyword == "relationship":
                rel, unknowns = _parse_relationship(node, path.name)
                if rel:
                    part.relationships.append(rel)
                part.unknowns.extend(unknowns)
            elif keyword == "role":
                roles, unknowns = _parse_role(node, unit, path.name)
                part.rls_roles.extend(roles)
                part.unknowns.extend(unknowns)
            elif node.header.startswith("ref table"):
                order.append(_name_and_expr(node.header[len("ref table") :])[0])
            elif keyword not in TOP_LEVEL:
                part.unknowns.append(
                    _unknown(path.name, f"unrecognized {keyword}", node.text)
                )
    ordered = [n for n in order if n in tables] + [n for n in tables if n not in order]
    part.tables = [tables[n] for n in ordered]
    part.sources.sort(key=lambda s: ordered.index(s.id))
    fill_depends_on(part.measures)
    return part
