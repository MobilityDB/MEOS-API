"""OGC API – Moving Features (OGC 22-003r3) generator.

Projects the *enriched* MEOS catalog (``meos-idl.json`` with ``category`` /
``network`` / ``wire`` / ``typeEncodings``, produced by ``parser/enrich.py``)
onto an **OGC API – Moving Features**-shaped OpenAPI 3.1 service contract.

The OGC projection is a complementary view to the generic ``generate_openapi.py``:

- Generic projection (``generator/openapi.py``): every stateless-exposable
  MEOS function becomes one RPC-style ``POST /{function}`` operation. Faithful
  to MEOS's value-algebra shape but not aligned with OGC standards.
- **MovFeat projection (this module)**: only the MEOS functions that have an
  OGC API – Moving Features analogue are exposed, and they are placed under
  the OGC-defined REST resource hierarchy (``/collections/{cid}/items/{fid}/...``).
  Each route carries an ``x-meos-{decode,encode,function}`` extension so a
  downstream OGC server (MobilityAPI, in this ecosystem) can dispatch each
  call to the right MEOS function without re-deriving the mapping.

The output is a *subset* of MEOS. Functions without an OGC analogue stay in
the generic OpenAPI; they are not duplicated here. MobilityAPI's runtime
serves both projections side by side: OGC-shaped paths for adopter clients
that follow the OGC API – Moving Features standard, generic RPC paths for
clients that want the raw MEOS surface.

Pure ``dict`` → ``dict``; no libclang and no MEOS runtime. Catalog enrichment
is a prerequisite (the ``network`` / ``wire`` / ``typeEncodings`` fields are
authored by ``parser/enrich.py``).
"""

from __future__ import annotations

from typing import Iterable

# Reuse the schema-building primitives from the generic OpenAPI generator so
# component schemas and the MeosError response are byte-identical across the
# two projections.
from generator.openapi import (
    _value_schema, _type_schema, _enum_schema,
)


# OGC API – Moving Features path map.
#
# Each entry maps an OGC-defined path under
# ``/collections/{collectionId}/items/{featureId}`` to the MEOS function the
# OGC operation dispatches to. The MEOS function name MUST match an entry in
# the enriched catalog with ``network.exposable == true``; entries whose
# MEOS function is absent from the catalog are skipped (with a warning on
# stderr).
#
# Method-and-shape rules:
# - GET on a deterministic accessor (no body): the MEOS function takes a
#   single ``Temporal *`` opaque + zero or more query parameters; result
#   is encoded per its ``wire.result.encode``.
# - POST on a constructor / aggregator: the MEOS function takes a request
#   body whose shape matches the function's ``wire.params``.
# - DELETE / PUT: persistence-layer concerns owned by MobilityAPI, not by
#   MEOS — the path is exposed but ``x-meos-function`` is null and the
#   request body / response schemas are OGC-defined rather than MEOS-derived.
_OGC_MOVFEAT_ROUTES: list[dict] = [
    # --- Collection-level (not MEOS-backed; persistence-layer in MobilityAPI) ---
    {"path": "/collections", "method": "get",
     "operationId": "listCollections", "meos": None,
     "summary": "List feature collections."},
    {"path": "/collections", "method": "post",
     "operationId": "createCollection", "meos": None,
     "summary": "Create a new feature collection."},
    {"path": "/collections/{collectionId}", "method": "get",
     "operationId": "getCollection", "meos": None,
     "summary": "Retrieve a collection's metadata."},
    {"path": "/collections/{collectionId}", "method": "put",
     "operationId": "replaceCollection", "meos": None,
     "summary": "Replace a collection's metadata."},
    {"path": "/collections/{collectionId}", "method": "delete",
     "operationId": "deleteCollection", "meos": None,
     "summary": "Delete a collection."},
    # --- Item-level (MEOS-backed for the I/O paths) ---
    {"path": "/collections/{collectionId}/items", "method": "get",
     "operationId": "listItems", "meos": None,
     "summary": "List moving-feature items in a collection."},
    {"path": "/collections/{collectionId}/items", "method": "post",
     "operationId": "createItem", "meos": "temporal_from_mfjson",
     "summary": "Create a moving-feature item from an MF-JSON payload."},
    {"path": "/collections/{collectionId}/items/{featureId}", "method": "get",
     "operationId": "getItem", "meos": "temporal_as_mfjson",
     "summary": "Retrieve a moving-feature item as MF-JSON."},
    {"path": "/collections/{collectionId}/items/{featureId}", "method": "delete",
     "operationId": "deleteItem", "meos": None,
     "summary": "Delete a moving-feature item."},
    # --- Trajectory-derived (the MEOS-rich part of the MovFeat surface) ---
    {"path": "/collections/{collectionId}/items/{featureId}/tgsequence",
     "method": "get",
     "operationId": "getTrajectory", "meos": "temporal_as_mfjson",
     "summary": "Retrieve the full trajectory geometry as MF-JSON."},
    {"path": "/collections/{collectionId}/items/{featureId}/tgsequence/velocity",
     "method": "get",
     "operationId": "getVelocity", "meos": "tpoint_speed",
     "summary": "Retrieve the temporal velocity profile."},
    {"path": "/collections/{collectionId}/items/{featureId}/tgsequence/acceleration",
     "method": "get",
     "operationId": "getAcceleration", "meos": "temporal_derivative",
     "summary": "Retrieve the temporal acceleration profile."},
    {"path": "/collections/{collectionId}/items/{featureId}/tgsequence/distance",
     "method": "get",
     "operationId": "getDistance", "meos": "tpoint_cumulative_length",
     "summary": "Retrieve the cumulative-length distance profile."},
    {"path": "/collections/{collectionId}/items/{featureId}/tgsequence/azimuth",
     "method": "get",
     "operationId": "getAzimuth", "meos": "tpoint_azimuth",
     "summary": "Retrieve the temporal azimuth profile."},
    # --- Property-level (per-name temporal property access) ---
    {"path": "/collections/{collectionId}/items/{featureId}/tproperty/{propertyName}",
     "method": "get",
     "operationId": "getProperty", "meos": "temporal_as_mfjson",
     "summary": "Retrieve a temporal property's MF-JSON encoding."},
    {"path": "/collections/{collectionId}/items/{featureId}/tproperty/{propertyName}",
     "method": "delete",
     "operationId": "deleteProperty", "meos": None,
     "summary": "Delete a temporal property."},
]


_PATH_PARAM_DESCRIPTIONS = {
    "collectionId": "Identifier of the feature collection.",
    "featureId":    "Identifier of the moving-feature item within the collection.",
    "propertyName": "Name of the temporal property.",
}


def _path_parameters(path: str) -> list[dict]:
    """Extract `{name}` placeholders from `path` and return OpenAPI parameter objects."""
    import re
    return [
        {
            "name": name,
            "in":   "path",
            "required": True,
            "schema": {"type": "string"},
            "description": _PATH_PARAM_DESCRIPTIONS.get(
                name, f"Path parameter `{name}`."
            ),
        }
        for name in re.findall(r"\{(\w+)\}", path)
    ]


def _meos_request_body(meos_fn: dict, used_types: set, used_enums: set) -> dict | None:
    """Build a JSON request body schema from a MEOS function's wire params."""
    wire = meos_fn["wire"]
    params = wire.get("params", [])
    if not params:
        return None
    props: dict = {}
    required: list = []
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
    return {
        "required": True,
        "content": {"application/json": {"schema": {
            "type": "object",
            "required": required,
            "additionalProperties": False,
            "properties": props,
        }}},
    }


def _meos_response_schema(meos_fn: dict, used_types: set,
                          used_enums: set) -> tuple[dict | None, str | None]:
    """Build a response schema + encode-name from a MEOS function's wire result."""
    wire = meos_fn["wire"]
    result = wire["result"]
    if result["kind"] == "void":
        return None, None
    schema = _value_schema(result, used_types, used_enums)
    encode = result.get("encode") if result["kind"] == "serialized" else None
    return schema, encode


def _build_operation(route: dict, fns_by_name: dict,
                     used_types: set, used_enums: set) -> dict:
    op: dict = {
        "operationId": route["operationId"],
        "summary":     route["summary"],
        "tags":        ["MovingFeatures"],
    }
    parameters = _path_parameters(route["path"])
    if parameters:
        op["parameters"] = parameters

    meos_name = route["meos"]
    if meos_name and meos_name in fns_by_name:
        meos_fn = fns_by_name[meos_name]
        op["x-meos-function"] = meos_name
        op["x-meos-category"] = meos_fn.get("category", "ogc")

        if route["method"] == "post":
            body = _meos_request_body(meos_fn, used_types, used_enums)
            if body is not None:
                op["requestBody"] = body

        if route["method"] in ("get", "post"):
            schema, encode = _meos_response_schema(
                meos_fn, used_types, used_enums
            )
            if schema is not None:
                if encode:
                    op["x-meos-encode"] = encode
                # MF-JSON is the OGC-standard MoveFeat encoding; advertise it
                # as the primary content type when the MEOS encode is mfjson.
                if encode == "as_mfjson":
                    op["responses"] = {"200": {
                        "description": "Result (MF-JSON)",
                        "content": {
                            "application/geo+json": {"schema": schema},
                            "application/json":     {"schema": schema},
                        },
                    }}
                else:
                    op["responses"] = {"200": {
                        "description": "Result",
                        "content": {"application/json": {"schema": schema}},
                    }}
            else:
                op["responses"] = {"204": {"description": "No content"}}
    else:
        # Persistence-layer route — MobilityAPI owns the body / response shape;
        # MEOS doesn't get called for this operation.
        op["x-meos-function"] = None
        op["responses"] = _persistence_layer_responses(route)

    op.setdefault("responses", {"204": {"description": "No content"}})
    op["responses"]["default"] = {
        "$ref": "#/components/responses/MeosError"
    }
    return op


def _persistence_layer_responses(route: dict) -> dict:
    """Conservative default responses for OGC routes with no MEOS analogue."""
    method = route["method"]
    if method == "get":
        return {"200": {
            "description": "Resource",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }}
    if method == "post":
        return {"201": {
            "description": "Created",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }}
    if method == "put":
        return {"200": {
            "description": "Replaced",
            "content": {"application/json": {"schema": {"type": "object"}}},
        }}
    if method == "delete":
        return {"204": {"description": "Deleted"}}
    return {"200": {"description": "OK"}}


def build_movfeat_openapi(catalog: dict, *,
                          title: str = "MEOS API – OGC Moving Features",
                          version: str = "0.1.0-draft") -> dict:
    """Build an OGC API – Moving Features OpenAPI 3.1 document.

    Routes whose ``meos`` field references a function not present in the
    enriched catalog (or not exposable) are skipped. The total number of
    routes and the number that have a MEOS backing are reported on
    ``info.x-meos-coverage``.
    """
    functions = {
        f["name"]: f
        for f in catalog.get("functions", [])
        if f.get("network", {}).get("exposable")
    }
    type_encodings = catalog.get("typeEncodings", {})
    enums = catalog.get("enums", [])

    used_types: set = set()
    used_enums: set = set()
    paths: dict = {}
    meos_backed = 0
    persistence_only = 0
    missing: list = []

    # Group route entries by path so multiple methods on the same path collapse.
    for route in _OGC_MOVFEAT_ROUTES:
        if route["meos"] and route["meos"] not in functions:
            missing.append(route["meos"])
            persistence_only += 1
        elif route["meos"]:
            meos_backed += 1
        else:
            persistence_only += 1
        op = _build_operation(route, functions, used_types, used_enums)
        paths.setdefault(route["path"], {})[route["method"]] = op

    schemas: dict = {}
    for t in sorted(used_types):
        schemas[t] = _type_schema(t, type_encodings)
    for e in sorted(used_enums):
        schemas[e] = _enum_schema(e, enums)

    doc = {
        "openapi": "3.1.0",
        "info": {
            "title": title,
            "version": version,
            "description": (
                "OGC API – Moving Features (OGC 22-003r3) projection of the "
                "MEOS catalog. Routes follow the OGC-defined REST resource "
                "hierarchy under `/collections/{collectionId}/items/{featureId}/…`; "
                "each route that has a MEOS analogue carries an "
                "`x-meos-function` / `x-meos-decode` / `x-meos-encode` extension "
                "so a downstream OGC server can dispatch to MEOS without "
                "re-deriving the mapping. Routes without a MEOS analogue are "
                "persistence-layer concerns owned by the consuming server. "
                "Generated, do not edit by hand."
            ),
            "x-meos-coverage": {
                "routes":           len(_OGC_MOVFEAT_ROUTES),
                "meos_backed":      meos_backed,
                "persistence_only": persistence_only,
                "missing_in_catalog": sorted(set(missing)),
            },
        },
        "tags": [{"name": "MovingFeatures",
                  "description": (
                      "OGC API – Moving Features operations. Trajectory-derived "
                      "paths dispatch to MEOS via the `x-meos-function` "
                      "extension; collection-level and item-level persistence "
                      "paths are owned by the consuming server."
                  )}],
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
                            "code":  {"type": "integer"},
                        },
                        "required": ["error"],
                    }}},
                }
            },
        },
    }
    return doc


def _missing_summary(missing: Iterable[str]) -> str:
    """Human-readable summary for stderr."""
    missing = sorted(set(missing))
    if not missing:
        return ""
    return (
        f"warning: {len(missing)} OGC route(s) reference MEOS functions absent "
        f"from the catalog: {', '.join(missing)}"
    )
