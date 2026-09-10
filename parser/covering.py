"""Temporal-covering descriptor — the single codegen source of truth for
projecting a MEOS temporal column into the covering columns of
TemporalParquet 2.0.0.

`meta/temporal-covering.json` is the curated, authoritative mapping (RFC
#870 TemporalParquet / #913 Temporal Data Lake): per temporal-type *class*
(spatial → STBOX, number → TBOX, timeOnly → no box) it names the box
converter, the SRID accessor, the covering struct columns with their fields
and MEOS accessors, and the plain columns beside them. Folding it into the
catalog means every binding/engine generates the *identical* covering
schema, so a temporal table prunes the same way on every platform (Iceberg
manifest pruning + Parquet row-group min/max) with no spatial-aware engine.

This is curated canonical data, not a heuristic — it is preserved verbatim
and only *derived* lookups are added (a flat `byType` index and the set of
referenced C symbols), so a generator never has to re-derive the mapping.
Pure dict → dict; no libclang.
"""

import json
from pathlib import Path

# The field names of a GeoParquet bounding box column, in their required order.
BBOX_2D = ("xmin", "ymin", "xmax", "ymax")
BBOX_3D = ("xmin", "ymin", "zmin", "xmax", "ymax", "zmax")


def _check_coverings(class_name: str, coverings: list) -> None:
    """Reject a class whose coverings a generator could not render as the
    TemporalParquet 2.0.0 covering columns."""
    keys = [c["key"] for c in coverings]
    if len(keys) != len(set(keys)):
        raise ValueError(
            f"temporal-covering: class {class_name!r} declares a covering "
            f"twice ({keys})")
    for covering in coverings:
        if covering["key"] != "bbox":
            continue
        fields = covering["fields"]
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
        _check_coverings(class_name, spec["coverings"])
        for t in spec["types"]:
            if t in by_type:
                raise ValueError(
                    f"temporal-covering: type {t!r} in two classes "
                    f"({by_type[t]['class']!r} and {class_name!r})")
            by_type[t] = {
                "class": class_name,
                "box": spec.get("box"),
                "srid": spec.get("srid"),
                "coverings": spec["coverings"],
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
        for covering in spec["coverings"]:
            for field in covering["fields"]:
                symbols.add(field["accessor"])
        for col in spec.get("columns", []):
            symbols.add(col["accessor"])

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
