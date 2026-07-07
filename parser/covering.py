"""Temporal-covering descriptor — the single codegen source of truth for
projecting a MEOS temporal column into Parquet/Iceberg covering columns.

`meta/temporal-covering.json` is the curated, authoritative mapping (RFC
#870 TemporalParquet / #913 Temporal Data Lake): per temporal-type *class*
(spatial → STBOX, number → TBOX) it names the box converter, the SRID
accessor, and the covering columns with their MEOS bbox accessors. Folding
it into the catalog means every binding/engine generates the *identical*
covering schema, so a temporal table prunes the same way on every platform
(Iceberg manifest pruning + Parquet row-group min/max) with no spatial-aware
engine.

This is curated canonical data, not a heuristic — it is preserved verbatim
and only *derived* lookups are added (a flat `byType` index and the set of
referenced C symbols), so a generator never has to re-derive the mapping.
Pure dict → dict; no libclang.
"""

import json
from pathlib import Path


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
        for t in spec["types"]:
            if t in by_type:
                raise ValueError(
                    f"temporal-covering: type {t!r} in two classes "
                    f"({by_type[t]['class']!r} and {class_name!r})")
            by_type[t] = {
                "class": class_name,
                "box": spec.get("box"),
                "srid": spec.get("srid"),
                "columns": spec["columns"],
            }

    # The complete set of MEOS C symbols this descriptor depends on — the
    # covering parity audit checks every one is actually in the catalog.
    symbols = {data["valueCodec"]["asHexWkb"], data["valueCodec"]["fromHexWkb"]}
    for spec in classes.values():
        if spec.get("box"):
            symbols.add(spec["box"]["from"])
        if spec.get("srid"):
            symbols.add(spec["srid"])
        for col in spec["columns"]:
            symbols.add(col["accessor"])

    idl["temporalCovering"] = {
        "provenance": data["provenance"],
        "version": data["version"],
        "valueCodec": data["valueCodec"],
        "metadataKeys": data["metadataKeys"],
        "classes": classes,
        "deferred": data.get("deferred", {}),
        "notes": data["notes"],
        "byType": by_type,                    # "tgeompoint" -> class + columns
        "types": sorted(by_type),
        "symbols": sorted(symbols),           # referenced C symbols (audit set)
        "count": len(by_type),
    }
    return idl
