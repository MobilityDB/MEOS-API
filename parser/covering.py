"""Temporal-covering descriptor — the single codegen source of truth for
projecting a MEOS temporal column into the covering columns of
TemporalParquet 2.0.0.

`meta/temporal-covering.json` is the curated, authoritative mapping (RFC
#870 TemporalParquet / #913 Temporal Data Lake): per temporal-type *class*
(spatial → STBOX, number → TBOX, timeOnly → no box) it names the box
converter, the SRID accessor, the covering struct columns with their fields
and MEOS accessors, and the plain columns beside them. A covering gives its
fields once for the whole class, or per type (`byType`) where the types of a
class differ in base type. Folding it into the catalog means every
binding/engine generates the *identical* covering schema, so a temporal
table prunes the same way on every platform (Iceberg manifest pruning +
Parquet row-group min/max) with no spatial-aware engine.

This is curated canonical data, not a heuristic — it is preserved verbatim
and only *derived* lookups are added (a flat `byType` index, where every
covering carries the fields of that type, and the set of referenced C
symbols), so a generator never has to re-derive the mapping. Pure dict →
dict; no header parsing.
"""

import json
from pathlib import Path

# The field names of a GeoParquet bounding box column, in their required order.
BBOX_2D = ("xmin", "ymin", "xmax", "ymax")
BBOX_3D = ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax")


def _resolve(class_name: str, types: list, covering: dict) -> dict:
    """Return, per type of the class, the fields a covering holds."""
    has_fields, has_by_type = "fields" in covering, "byType" in covering
    if has_fields == has_by_type:
        raise ValueError(
            f"temporal-covering: class {class_name!r} covering "
            f"{covering['key']!r} gives neither or both of fields and byType")
    if has_fields:
        return {t: covering["fields"] for t in types}
    by_type = covering["byType"]
    if set(by_type) != set(types):
        raise ValueError(
            f"temporal-covering: class {class_name!r} covering "
            f"{covering['key']!r} gives fields for {sorted(by_type)}, where "
            f"the class holds {sorted(types)}")
    return by_type


def _check_bbox(class_name: str, fields: list) -> None:
    """Reject bbox fields that are not a GeoParquet bounding box column's."""
    names = tuple(f["name"] for f in fields)
    planar = tuple(f["name"] for f in fields if f.get("when") != "hasZ")
    if names not in (BBOX_2D, BBOX_3D) or planar != BBOX_2D:
        raise ValueError(
            f"temporal-covering: class {class_name!r} declares the bbox "
            f"fields {names}, where a GeoParquet bounding box column has "
            f"{BBOX_2D} or {BBOX_3D}, the z fields only for 3D values")
    if any(f["sqlType"] != "double" for f in fields):
        raise ValueError(
            f"temporal-covering: class {class_name!r} declares a bbox "
            f"field that is not double")


def _coverings_by_type(class_name: str, spec: dict) -> dict:
    """Return, per type of the class, its coverings with their fields."""
    keys = [c["key"] for c in spec["coverings"]]
    if len(keys) != len(set(keys)):
        raise ValueError(
            f"temporal-covering: class {class_name!r} declares a covering "
            f"twice ({keys})")
    result = {t: [] for t in spec["types"]}
    for covering in spec["coverings"]:
        fields_by_type = _resolve(class_name, spec["types"], covering)
        for t in spec["types"]:
            fields = fields_by_type[t]
            if covering["key"] == "bbox":
                _check_bbox(class_name, fields)
            result[t].append({
                "key": covering["key"],
                "column": covering["column"],
                "fields": fields,
            })
    return result


def attach_temporal_covering(idl: dict, path: Path) -> dict:
    """Attach ``idl["temporalCovering"]`` from the canonical mapping file."""
    if not Path(path).exists():
        return idl
    data = json.loads(Path(path).read_text())

    classes = data["classes"]

    # Integrity: a temporal type may belong to at most one covering class —
    # two classes claiming the same type would make codegen ambiguous.
    by_type = {}
    for class_name, spec in classes.items():
        coverings = _coverings_by_type(class_name, spec)
        for t in spec["types"]:
            if t in by_type:
                raise ValueError(
                    f"temporal-covering: type {t!r} in two classes "
                    f"({by_type[t]['class']!r} and {class_name!r})")
            by_type[t] = {
                "class": class_name,
                "box": spec.get("box"),
                "srid": spec.get("srid"),
                "coverings": coverings[t],
                "columns": spec.get("columns", []),
            }

    # The complete set of MEOS C symbols this descriptor depends on — the
    # covering parity audit checks every one is actually in the catalog.
    symbols = {data["valueCodec"]["asHexWkb"], data["valueCodec"]["fromHexWkb"]}
    for spec in classes.values():
        if spec.get("box"):
            symbols.add(spec["box"]["from"])
        if spec.get("srid"):
            symbols.add(spec["srid"])
        for col in spec.get("columns", []):
            symbols.add(col["accessor"])
    for entry in by_type.values():
        for covering in entry["coverings"]:
            for field in covering["fields"]:
                symbols.add(field["accessor"])

    idl["temporalCovering"] = {
        "provenance": data["provenance"],
        "version": data["version"],
        "valueCodec": data["valueCodec"],
        "metadataKeys": data["metadataKeys"],
        "classes": classes,
        "deferred": data.get("deferred", {}),
        "notes": data["notes"],
        "byType": by_type,                    # "tgeompoint" -> class + coverings
        "types": sorted(by_type),
        "symbols": sorted(symbols),           # referenced C symbols (audit set)
        "count": len(by_type),
    }
    return idl
