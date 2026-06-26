"""Temporal-covering projection generator.

Projects the ``temporalCovering`` block of the MEOS catalog
(``meos-idl.json``, produced by ``parser/covering.py``) onto the canonical,
language-agnostic covering-column contract: per temporal type, the ordered
covering columns with the fully-composed MEOS expression that derives each
from the value.

Every binding generator (PyMEOS, JMEOS, MobilityDuck, MobilitySpark, …)
renders this same contract in its own idiom — a DuckDB ``GENERATED`` column,
a Spark UDF projection, a PyMEOS writer — so a temporal table prunes the
same way on every platform (Iceberg manifest + Parquet row-group min/max).
The ``VALUE`` placeholder is the temporal column reference the binding
substitutes.

Pure ``dict`` → ``dict``; no libclang and no MEOS runtime.
"""

from __future__ import annotations


def _column_expr(column: dict, box_from: str) -> str:
    """Compose the MEOS expression that derives one covering column from the
    temporal value (``VALUE``). A ``box`` column is read off the value's box;
    a ``value`` column is read off the value directly."""
    if column["source"] == "value":
        return f"{column['accessor']}(VALUE)"
    return f"{column['accessor']}({box_from}(VALUE))"


def build_covering_projection(catalog: dict) -> dict:
    """Project ``temporalCovering`` onto the canonical covering-column contract."""
    cov = catalog.get("temporalCovering")
    if not cov:
        raise ValueError("catalog has no `temporalCovering` — run run.py")

    types = {}
    for tname, spec in cov["byType"].items():
        box = spec.get("box")
        box_from = box["from"] if box else None
        columns = []
        for col in spec["columns"]:
            entry = {
                "name": col["name"],
                "sqlType": col["sqlType"],
                "expr": _column_expr(col, box_from),
            }
            if col.get("when"):
                entry["when"] = col["when"]
            columns.append(entry)
        types[tname] = {
            "class": spec["class"],
            "boxType": box["type"] if box else None,
            "columns": columns,
        }

    return {
        "version": cov["version"],
        "valueCodec": cov["valueCodec"],
        "metadataKeys": cov["metadataKeys"],
        "types": types,
        "count": len(types),
    }
