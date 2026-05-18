"""OpenAPI 3.1 generator.

Projects the *enriched* MEOS catalog (``meos-idl.json`` with ``category`` /
``network`` / ``wire`` / ``typeEncodings``, produced by ``parser/enrich.py``)
onto an OpenAPI 3.1 service contract.

The projection is deliberately RPC-style — MEOS is a value algebra, not a
REST resource model, so each *stateless-exposable* function becomes one
``POST /{function}`` operation (≈ an OGC API – Processes "process"). Opaque
values cross the wire as strings carried in their `typeEncodings` (text /
MF-JSON / WKB); each opaque type and referenced enum becomes a reusable
component schema. ``x-meos-*`` extensions carry the decode/encode function
names and category so a downstream server or MCP generator can consume this
same document.

Pure ``dict`` → ``dict``; no libclang and no MEOS runtime. Only functions
with ``network.exposable == true`` are emitted; the rest are reported by
``build_openapi``'s return-value count via the caller.
"""

import re

_QUAL_RE = re.compile(r"\b(const|volatile|struct|union|enum)\b")

_PRIMITIVE = {
    "integer": {"type": "integer"},
    "number": {"type": "number"},
    "boolean": {"type": "boolean"},
    "string": {"type": "string"},
}


def _clean_type(c_type: str) -> str:
    """``const struct Temporal *`` -> ``Temporal`` (matches typeEncodings keys)."""
    return " ".join(_QUAL_RE.sub(" ", c_type).replace("*", " ").split())


def _scalar_schema(wire: dict, used_enums: set) -> dict:
    if wire.get("enum"):
        used_enums.add(wire["enum"])
        return {"$ref": f"#/components/schemas/{wire['enum']}"}
    return dict(_PRIMITIVE.get(wire.get("json", "string"), {"type": "string"}))


def _value_schema(wire: dict, used_types: set, used_enums: set) -> dict:
    """Schema for one parameter or the result."""
    kind = wire["kind"]
    if kind == "json":
        return _scalar_schema(wire, used_enums)
    if kind == "serialized":
        t = _clean_type(wire["cType"])
        used_types.add(t)
        return {"$ref": f"#/components/schemas/{t}"}
    if kind == "array":
        return {"type": "array",
                "items": _value_schema(wire["element"], used_types,
                                       used_enums)}
    # Should not happen for an exposable function.
    return {"type": "string"}


def _operation(fn: dict, used_types: set, used_enums: set) -> dict:
    wire = fn["wire"]
    op = {
        "operationId": fn["name"],
        "summary": fn.get("doc") or fn["name"],
        "tags": [fn["category"]],
        "x-meos-category": fn["category"],
    }

    params = wire.get("params", [])
    if params:
        props, required = {}, []
        for p in params:
            props[p["name"]] = _value_schema(p, used_types, used_enums)
            required.append(p["name"])
            if p["kind"] == "serialized":
                props[p["name"]] = {
                    "allOf": [props[p["name"]]],
                    "x-meos-decode": p["decode"],
                }
            elif p["kind"] == "array":
                props[p["name"]] = {
                    **props[p["name"]],
                    "x-meos-decode": p["element"]["decode"],
                }
        op["requestBody"] = {
            "required": True,
            "content": {"application/json": {"schema": {
                "type": "object",
                "required": required,
                "additionalProperties": False,
                "properties": props,
            }}},
        }

    result = wire["result"]
    if result["kind"] == "void":
        op["responses"] = {"204": {"description": "No content"}}
    else:
        schema = _value_schema(result, used_types, used_enums)
        content_schema = schema
        if result["kind"] == "serialized":
            op["x-meos-encode"] = result["encode"]
        op["responses"] = {"200": {
            "description": "Result",
            "content": {"application/json": {"schema": content_schema}},
        }}
    op["responses"]["default"] = {
        "$ref": "#/components/responses/MeosError"
    }
    return op


def _type_schema(name: str, type_encodings: dict) -> dict:
    te = type_encodings.get(name)
    if not te:
        return {"type": "string", "title": name}
    encs = te.get("encodings", [])
    return {
        "type": "string",
        "title": name,
        "description": (
            f"Serialized MEOS {name}. Wire encodings: {', '.join(encs)} "
            f"(e.g. WKT / MF-JSON / HexWKB)."
        ),
        "x-meos-encodings": encs,
        "x-meos-in": te.get("in"),
        "x-meos-out": te.get("out"),
    }


def _enum_schema(name: str, enums: list) -> dict:
    for e in enums:
        if e["name"] == name:
            return {
                "type": "string",
                "title": name,
                "enum": [v["name"] for v in e.get("values", [])],
                "x-meos-c-enum": True,
            }
    return {"type": "string", "title": name}


def build_openapi(catalog: dict, *, title: str = "MEOS API",
                  version: str = "0.1.0") -> dict:
    """Build an OpenAPI 3.1 document from an enriched catalog."""
    functions = sorted(
        (f for f in catalog.get("functions", [])
         if f.get("network", {}).get("exposable")),
        key=lambda f: f["name"],
    )
    type_encodings = catalog.get("typeEncodings", {})
    enums = catalog.get("enums", [])

    used_types: set = set()
    used_enums: set = set()
    paths: dict = {}
    tags_seen: set = set()

    for fn in functions:
        paths[f"/{fn['name']}"] = {
            "post": _operation(fn, used_types, used_enums)
        }
        tags_seen.add(fn["category"])

    schemas = {}
    for t in sorted(used_types):
        schemas[t] = _type_schema(t, type_encodings)
    for e in sorted(used_enums):
        schemas[e] = _enum_schema(e, enums)

    total = len(catalog.get("functions", []))
    return {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": version,
            "description": (
                "Auto-generated from the MEOS-API catalog. Each operation is "
                "a stateless-exposable MEOS function projected RPC-style as "
                "`POST /{function}`; opaque values cross the wire as strings "
                "in the encodings listed on their component schema. "
                "Generated, do not edit by hand."
            ),
            "x-meos-coverage": {
                "functions": total,
                "exposed": len(functions),
            },
        },
        "tags": [{"name": t} for t in sorted(tags_seen)],
        "paths": dict(sorted(paths.items())),
        "components": {
            "schemas": schemas,
            "responses": {
                "MeosError": {
                    "description": "MEOS error",
                    "content": {"application/json": {"schema": {
                        "type": "object",
                        "properties": {
                            "error": {"type": "string"},
                            "code": {"type": "integer"},
                        },
                        "required": ["error"],
                    }}},
                }
            },
        },
    }
