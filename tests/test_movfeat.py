"""Unit tests for generator/movfeat.py.

Runs without libclang or a MEOS runtime: pure dict→dict.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generator.movfeat import build_movfeat_openapi, _OGC_MOVFEAT_ROUTES

TEMP = "const struct Temporal *"


def serialized(name, ctype, decode):
    return {"name": name, "kind": "serialized", "cType": ctype,
            "decode": decode, "encodings": ["mfjson", "text", "wkb"]}


# A synthetic enriched catalog covering all the MEOS functions the MovFeat
# projection references. Functions not listed here remain unreferenced and
# unexposed; the test for the missing-function reporting uses an
# intentionally trimmed catalog.
FULL_CATALOG = {
    "functions": [
        {
            "name": "temporal_as_mfjson", "category": "io",
            "network": {"exposable": True},
            "wire": {
                "params": [serialized("temp", TEMP, "temporal_in")],
                "result": {"kind": "serialized", "cType": TEMP,
                           "encode": "as_mfjson",
                           "encodings": ["mfjson"]},
            },
        },
        {
            "name": "temporal_from_mfjson", "category": "io",
            "network": {"exposable": True},
            "wire": {
                "params": [{"name": "json", "kind": "json", "json": "string"}],
                "result": {"kind": "serialized", "cType": TEMP,
                           "encode": "temporal_out",
                           "encodings": ["mfjson", "wkb"]},
            },
        },
        {
            "name": "tpoint_speed", "category": "tpoint",
            "network": {"exposable": True},
            "wire": {
                "params": [serialized("temp", TEMP, "temporal_in")],
                "result": {"kind": "serialized", "cType": TEMP,
                           "encode": "temporal_out",
                           "encodings": ["mfjson", "wkb"]},
            },
        },
        {
            "name": "tpoint_cumulative_length", "category": "tpoint",
            "network": {"exposable": True},
            "wire": {
                "params": [serialized("temp", TEMP, "temporal_in")],
                "result": {"kind": "serialized", "cType": TEMP,
                           "encode": "temporal_out",
                           "encodings": ["mfjson", "wkb"]},
            },
        },
        {
            "name": "tpoint_azimuth", "category": "tpoint",
            "network": {"exposable": True},
            "wire": {
                "params": [serialized("temp", TEMP, "temporal_in")],
                "result": {"kind": "serialized", "cType": TEMP,
                           "encode": "temporal_out",
                           "encodings": ["mfjson", "wkb"]},
            },
        },
        {
            "name": "temporal_derivative", "category": "tnumber",
            "network": {"exposable": True},
            "wire": {
                "params": [serialized("temp", TEMP, "temporal_in")],
                "result": {"kind": "serialized", "cType": TEMP,
                           "encode": "temporal_out",
                           "encodings": ["mfjson", "wkb"]},
            },
        },
    ],
    "typeEncodings": {
        "Temporal": {
            "encodings": ["mfjson", "text", "wkb"],
            "in": "temporal_in",
            "out": "temporal_out",
        }
    },
    "enums": [],
}


class TestMovFeat(unittest.TestCase):

    def test_paths_under_movfeat_hierarchy(self):
        doc = build_movfeat_openapi(FULL_CATALOG)
        for path in doc["paths"]:
            self.assertTrue(
                path.startswith("/collections"),
                f"non-OGC path emitted: {path!r}",
            )

    def test_movfeat_tag_present(self):
        doc = build_movfeat_openapi(FULL_CATALOG)
        tag_names = {t["name"] for t in doc.get("tags", [])}
        self.assertIn("MovingFeatures", tag_names)

    def test_meos_backed_routes_carry_x_meos_function(self):
        doc = build_movfeat_openapi(FULL_CATALOG)
        for path, ops in doc["paths"].items():
            for method, op in ops.items():
                if op.get("x-meos-function"):
                    # Sanity: should be one of our known MEOS function names.
                    self.assertIn(
                        op["x-meos-function"],
                        {f["name"] for f in FULL_CATALOG["functions"]},
                        f"{method} {path} → x-meos-function "
                        f"{op['x-meos-function']!r} not in catalog",
                    )

    def test_persistence_layer_routes_have_no_x_meos_function(self):
        doc = build_movfeat_openapi(FULL_CATALOG)
        # /collections (no item-id, no MEOS analogue) is persistence-layer.
        self.assertIsNone(
            doc["paths"]["/collections"]["get"].get("x-meos-function"),
        )
        self.assertIsNone(
            doc["paths"]["/collections"]["post"].get("x-meos-function"),
        )

    def test_mfjson_routes_advertise_geo_json_content_type(self):
        doc = build_movfeat_openapi(FULL_CATALOG)
        # getTrajectory uses temporal_as_mfjson (encode == "as_mfjson"),
        # so its 200 response must advertise application/geo+json.
        op = doc["paths"][
            "/collections/{collectionId}/items/{featureId}/tgsequence"
        ]["get"]
        content_types = set(op["responses"]["200"]["content"].keys())
        self.assertIn("application/geo+json", content_types)
        self.assertIn("application/json", content_types)

    def test_meos_default_error_response_referenced_on_every_op(self):
        doc = build_movfeat_openapi(FULL_CATALOG)
        for path, ops in doc["paths"].items():
            for method, op in ops.items():
                self.assertIn(
                    "default", op["responses"],
                    f"{method} {path} missing default error response",
                )
                self.assertEqual(
                    op["responses"]["default"]["$ref"],
                    "#/components/responses/MeosError",
                )

    def test_path_parameter_objects_have_descriptions(self):
        doc = build_movfeat_openapi(FULL_CATALOG)
        op = doc["paths"][
            "/collections/{collectionId}/items/{featureId}"
        ]["get"]
        params = {p["name"]: p for p in op.get("parameters", [])}
        for name in ("collectionId", "featureId"):
            self.assertIn(name, params)
            self.assertTrue(params[name]["description"],
                            f"{name} parameter has empty description")
            self.assertEqual(params[name]["in"], "path")
            self.assertTrue(params[name]["required"])

    def test_route_count_matches_manifest(self):
        doc = build_movfeat_openapi(FULL_CATALOG)
        # Manifest has multiple methods per path; count flattened routes.
        routes = sum(len(ops) for ops in doc["paths"].values())
        self.assertEqual(routes, len(_OGC_MOVFEAT_ROUTES))

    def test_missing_meos_function_is_reported(self):
        # Strip one MEOS function the manifest references.
        catalog = {
            "functions": [
                f for f in FULL_CATALOG["functions"]
                if f["name"] != "tpoint_speed"
            ],
            "typeEncodings": FULL_CATALOG["typeEncodings"],
            "enums": FULL_CATALOG["enums"],
        }
        doc = build_movfeat_openapi(catalog)
        coverage = doc["info"]["x-meos-coverage"]
        self.assertIn("tpoint_speed", coverage["missing_in_catalog"])

    def test_meos_coverage_summary_arithmetic(self):
        doc = build_movfeat_openapi(FULL_CATALOG)
        cov = doc["info"]["x-meos-coverage"]
        # routes == meos_backed + persistence_only by construction
        self.assertEqual(
            cov["routes"], cov["meos_backed"] + cov["persistence_only"],
            f"coverage arithmetic mismatch: {cov}",
        )

    def test_openapi_3_1_top_level_shape(self):
        doc = build_movfeat_openapi(FULL_CATALOG)
        self.assertEqual(doc["openapi"], "3.1.0")
        self.assertIn("info", doc)
        self.assertIn("paths", doc)
        self.assertIn("components", doc)
        self.assertIn("schemas", doc["components"])
        self.assertIn("responses", doc["components"])
        self.assertIn("MeosError", doc["components"]["responses"])

    def test_component_schemas_share_with_generic_openapi(self):
        # Sanity: a referenced opaque type produces a component schema
        # carrying the typeEncodings metadata.  Same helper as the generic
        # OpenAPI generator, so the two projections expose byte-identical
        # schemas for shared types.
        doc = build_movfeat_openapi(FULL_CATALOG)
        schemas = doc["components"]["schemas"]
        if "Temporal" in schemas:
            ts = schemas["Temporal"]
            self.assertEqual(ts["title"], "Temporal")
            self.assertIn("x-meos-encodings", ts)
            self.assertIn("x-meos-in", ts)
            self.assertIn("x-meos-out", ts)


if __name__ == "__main__":
    unittest.main()
