"""Temporal-covering projection generator.

Projects the ``temporalCovering`` block of the MEOS catalog
(``meos-idl.json``, produced by ``parser/covering.py``) onto the canonical,
language-agnostic covering-column contract of TemporalParquet 2.0.0: per
temporal type, each covering struct column with its name, its fields in
order, and the fully-composed MEOS expression that derives each field from
the value, plus the plain columns beside them.

Every binding generator (PyMEOS, JMEOS, MobilityDuck, MobilitySpark, …)
renders this same contract in its own idiom — a DuckDB struct column, a
Spark UDF projection, a PyMEOS writer — so a temporal table prunes the
same way on every platform (Iceberg manifest + Parquet row-group min/max).
Two placeholders are the binding's to substitute: ``VALUE`` is the temporal
column reference, and ``{col}`` in a covering's column name is the temporal
column's name.

Pure ``dict`` → ``dict``: it reads the catalog only and needs no MEOS runtime.
"""

from __future__ import annotations


def _column_expr(column: dict, box_from: str) -> str:
    """Compose the MEOS expression that derives one field from the temporal
    value (``VALUE``). A ``box`` field is read off the value's box; a
    ``value`` field is read off the value directly."""
    if column["source"] == "value":
        return f"{column['accessor']}(VALUE)"
    return f"{column['accessor']}({box_from}(VALUE))"


def _field(field: dict, box_from: str) -> dict:
    """Project one covering field or plain column."""
    entry = {
        "name": field["name"],
        "sqlType": field["sqlType"],
        "expr": _column_expr(field, box_from),
    }
    if field.get("when"):
        entry["when"] = field["when"]
    return entry


def build_covering_projection(catalog: dict) -> dict:
    """Project ``temporalCovering`` onto the canonical covering-column contract."""
    cov = catalog.get("temporalCovering")
    if not cov:
        raise ValueError("catalog has no `temporalCovering` — run run.py")

    types = {}
    for tname, spec in cov["byType"].items():
        box = spec.get("box")
        box_from = box["from"] if box else None
        coverings = [
            {
                "key": covering["key"],
                "column": covering["column"],
                "fields": [_field(f, box_from) for f in covering["fields"]],
            }
            for covering in spec["coverings"]
        ]
        types[tname] = {
            "class": spec["class"],
            "boxType": box["type"] if box else None,
            "coverings": coverings,
            "columns": [_field(c, box_from) for c in spec.get("columns", [])],
        }

    return {
        "version": cov["version"],
        "valueCodec": cov["valueCodec"],
        "metadataKeys": cov["metadataKeys"],
        "types": types,
        "count": len(types),
    }
