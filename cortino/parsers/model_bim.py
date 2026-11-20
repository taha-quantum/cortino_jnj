"""JSON (TMSL) semantic model parser: model.bim in PBIP, DataModelSchema in .pbit."""

import json
from pathlib import Path
from typing import Any

from cortino.parsers.common import ModelPart, fill_depends_on, read_json, source_from_m
from cortino.spec.models import Column, Measure, Relationship, RlsRole, Table, Unknown

# Keys that carry no semantics we need; anything else is recorded as an Unknown.
ROOT_KEYS = {
    "name", "compatibilityLevel", "model", "id", "createdTimestamp", "lastUpdate",
    "lastSchemaUpdate", "lastProcessed", "description", "annotations", "culture",
}  # fmt: skip
MODEL_KEYS = {
    "tables", "relationships", "roles", "culture", "defaultPowerBIDataSourceVersion",
    "sourceQueryCulture", "dataAccessOptions", "annotations", "defaultMode", "description",
    "discourageImplicitMeasures", "cultures", "collation", "name",
}  # fmt: skip
TABLE_KEYS = {
    "name", "columns", "measures", "partitions", "annotations", "lineageTag", "dataCategory",
    "isHidden", "description", "extendedProperties", "changedProperties", "isPrivate",
    "showAsVariationsOnly", "excludeFromModelRefresh", "sourceLineageTag", "queryGroup",
    "isDefaultLabel", "isDefaultImage", "systemManaged",
}  # fmt: skip
ROLE_KEYS = {
    "name",
    "modelPermission",
    "tablePermissions",
    "annotations",
    "description",
}
PERMISSION_KEYS = {"name", "filterExpression", "columnPermissions", "annotations"}


def _text(value: Any) -> str:
    """BIM stores expressions as a string or a list of lines."""
    if isinstance(value, list):
        value = "\n".join(str(v) for v in value)
    return str(value or "").strip()


def _unknown(file: str, what: str, raw: Any) -> Unknown:
    text = raw if isinstance(raw, str) else json.dumps(raw)
    return Unknown(where=f"bim:{file}", what=what, raw=text)


def _extras(obj: dict, known: set[str], label: str, file: str, part: ModelPart) -> None:
    for key, value in obj.items():
        if key not in known:
            part.unknowns.append(
                _unknown(file, f"unrecognized {label}{key}", {key: value})
            )


def _parse_table(obj: dict, file: str, part: ModelPart) -> Table:
    name = obj["name"]
    table = Table(name=name)
    _extras(obj, TABLE_KEYS, "", file, part)
    for col in obj.get("columns", []):
        table.columns.append(
            Column(
                name=col["name"],
                data_type=col.get("dataType", ""),
                source_column=col.get("sourceColumn"),
            )
        )
        if col.get("type") == "calculated" or "expression" in col:
            part.unknowns.append(
                _unknown(file, "calculated column", _text(col.get("expression")))
            )
    for measure in obj.get("measures", []):
        part.measures.append(
            Measure(
                name=measure["name"],
                table=name,
                expression=_text(measure.get("expression")),
                language="dax",
            )
        )
    for partition in obj.get("partitions", []):
        source = partition.get("source", {})
        if source.get("type") != "m":
            part.unknowns.append(_unknown(file, "non-M partition", partition))
            continue
        part.sources.append(source_from_m(name, _text(source.get("expression"))))
        table.source_id = name
    return table


def _parse_relationship(
    obj: dict, file: str
) -> tuple[Relationship | None, list[Unknown]]:
    try:
        ends = [obj["fromTable"], obj["fromColumn"], obj["toTable"], obj["toColumn"]]
    except KeyError:
        return None, [_unknown(file, "relationship without usable columns", obj)]
    cards = (obj.get("fromCardinality", "many"), obj.get("toCardinality", "one"))
    if cards == ("one", "many"):
        ends = [ends[2], ends[3], ends[0], ends[1]]
        cards = ("many", "one")
    cardinality = {
        ("many", "one"): "many_to_one",
        ("one", "one"): "one_to_one",
        ("many", "many"): "many_to_many",
    }.get(cards)
    if cardinality is None:
        return None, [_unknown(file, "relationship with unknown cardinality", obj)]
    unknowns = []
    if obj.get("isActive", True) is False:
        unknowns.append(_unknown(file, "inactive relationship", obj))
    both = obj.get("crossFilteringBehavior") == "bothDirections"
    return (
        Relationship(
            from_table=ends[0],
            from_column=ends[1],
            to_table=ends[2],
            to_column=ends[3],
            cardinality=cardinality,
            cross_filter="both" if both else "single",
        ),
        unknowns,
    )


def _parse_role(obj: dict, file: str, part: ModelPart) -> None:
    _extras(obj, ROLE_KEYS, "role ", file, part)
    roles = 0
    for perm in obj.get("tablePermissions", []):
        _extras(perm, PERMISSION_KEYS, "tablePermission ", file, part)
        part.rls_roles.append(
            RlsRole(
                name=obj["name"],
                table=perm["name"],
                filter_expression=_text(perm.get("filterExpression")),
            )
        )
        roles += 1
        if perm.get("columnPermissions"):
            part.unknowns.append(
                _unknown(file, "column-level security", perm["columnPermissions"])
            )
    if not roles:
        part.unknowns.append(
            _unknown(file, f"role {obj['name']} has no tablePermission", obj)
        )


def parse_bim(path: Path) -> ModelPart:
    """Parse a model.bim / DataModelSchema file (UTF-8 or UTF-16, BOM optional)."""
    try:
        return _parse_bim(path)
    except (KeyError, AttributeError, TypeError, ValueError) as e:
        raise ValueError(f"{path.name}: malformed model ({e!r})") from e


def _parse_bim(path: Path) -> ModelPart:
    file = path.name
    _, root = read_json(path)
    part = ModelPart()
    _extras(root, ROOT_KEYS, "", file, part)
    model = root.get("model", {})
    _extras(model, MODEL_KEYS, "", file, part)
    for obj in model.get("tables", []):
        part.tables.append(_parse_table(obj, file, part))
    for obj in model.get("relationships", []):
        rel, unknowns = _parse_relationship(obj, file)
        if rel:
            part.relationships.append(rel)
        part.unknowns.extend(unknowns)
    for obj in model.get("roles", []):
        _parse_role(obj, file, part)
    fill_depends_on(part.measures)
    return part
