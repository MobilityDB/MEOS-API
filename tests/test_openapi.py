"""Unit tests for generator/openapi.py.

Runs without libclang or pytest:  python3 tests/test_openapi.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generator.openapi import build_openapi

TEMP = "const struct Temporal *"


def serialized(name, ctype, decode):
    return {"name": name, "kind": "serialized", "cType": ctype,
            "decode": decode, "encodings": ["mfjson", "text", "wkb"]}


CATALOG = {
    "functions": [
        {  # serialized params, scalar result
            "name": "temporal_eq", "category": "predicate",
            "network": {"exposable": True, "method": "POST", "reason": None},
            "wire": {
                "params": [serialized("temp1", TEMP, "temporal_in"),
                           serialized("temp2", TEMP, "temporal_in")],
                "result": {"kind": "json", "json": "integer"},
            },
        },
        {  # enum param, serialized result
            "name": "temporal_set_interp", "category": "transformation",
            "doc": "Set the interpolation of a temporal value.",
            "network": {"exposable": True, "method": "POST", "reason": None},
            "wire": {
                "params": [
                    serialized("temp", TEMP, "temporal_in"),
                    {"name": "interp", "kind": "json", "json": "string",
                     "enum": "interpType"},
                ],
                "result": {"kind": "serialized",
                           "cType": "struct Temporal *",
                           "encode": "temporal_out",
                           "encodings": ["mfjson", "text", "wkb"]},
            },
        },
        {  # no params, void result
            "name": "noop_op", "category": "transformation",
            "network": {"exposable": True, "method": "POST", "reason": None},
            "wire": {"params": [], "result": {"kind": "void"}},
        },
        {  # input-array builder
            "name": "temporal_merge_array", "category": "transformation",
            "network": {"exposable": True, "method": "POST",
                        "reason": None},
            "wire": {
                "params": [{"name": "temparr", "kind": "array",
                            "count_param": "count",
                            "element": {"kind": "serialized",
                                        "cType": "struct Temporal *",
                                        "decode": "temporal_in",
                                        "encodings": ["text"]}}],
                "result": {"kind": "serialized",
                           "cType": "struct Temporal *",
                           "encode": "temporal_out",
                           "encodings": ["text"]},
            },
        },
        {  # not exposable -> excluded
            "name": "tsequence_make", "category": "constructor",
            "network": {"exposable": False, "method": None,
                        "reason": "array-or-out-param:instants"},
            "wire": {"params": [], "result": {"kind": "unsupported"}},
        },
    ],
    "typeEncodings": {
        "Temporal": {"encodings": ["mfjson", "text", "wkb"],
                     "in": "temporal_in", "out": "temporal_out"},
    },
    "enums": [{"name": "interpType",
               "values": [{"name": "STEP", "value": 0},
                          {"name": "LINEAR", "value": 1}]}],
    "structs": [],
}


class OpenApiTests(unittest.TestCase):
    def setUp(self):
        self.spec = build_openapi(CATALOG, version="9.9.9")

    def test_envelope(self):
        self.assertEqual(self.spec["openapi"], "3.1.0")
        self.assertEqual(self.spec["info"]["version"], "9.9.9")
        self.assertEqual(self.spec["info"]["x-meos-coverage"],
                         {"functions": 5, "exposed": 4})

    def test_array_param(self):
        op = self.spec["paths"]["/temporal_merge_array"]["post"]
        sch = op["requestBody"]["content"]["application/json"]["schema"]
        a = sch["properties"]["temparr"]
        self.assertEqual(a["type"], "array")
        self.assertEqual(a["items"],
                         {"$ref": "#/components/schemas/Temporal"})
        self.assertEqual(a["x-meos-decode"], "temporal_in")

    def test_non_exposable_excluded(self):
        self.assertNotIn("/tsequence_make", self.spec["paths"])
        self.assertEqual(len(self.spec["paths"]), 4)

    def test_paths_sorted(self):
        keys = list(self.spec["paths"])
        self.assertEqual(keys, sorted(keys))

    def test_predicate_operation(self):
        op = self.spec["paths"]["/temporal_eq"]["post"]
        self.assertEqual(op["operationId"], "temporal_eq")
        self.assertEqual(op["tags"], ["predicate"])
        self.assertEqual(op["x-meos-category"], "predicate")
        body = op["requestBody"]["content"]["application/json"]["schema"]
        self.assertEqual(body["required"], ["temp1", "temp2"])
        self.assertFalse(body["additionalProperties"])
        temp1 = body["properties"]["temp1"]
        self.assertEqual(temp1["allOf"],
                         [{"$ref": "#/components/schemas/Temporal"}])
        self.assertEqual(temp1["x-meos-decode"], "temporal_in")
        r200 = op["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(r200, {"type": "integer"})
        self.assertEqual(op["responses"]["default"],
                         {"$ref": "#/components/responses/MeosError"})

    def test_enum_param_and_serialized_result(self):
        op = self.spec["paths"]["/temporal_set_interp"]["post"]
        self.assertEqual(op["summary"],
                         "Set the interpolation of a temporal value.")
        props = op["requestBody"]["content"]["application/json"]["schema"][
            "properties"]
        self.assertEqual(props["interp"],
                         {"$ref": "#/components/schemas/interpType"})
        self.assertEqual(op["x-meos-encode"], "temporal_out")
        r = op["responses"]["200"]["content"]["application/json"]["schema"]
        self.assertEqual(r, {"$ref": "#/components/schemas/Temporal"})

    def test_void_operation(self):
        op = self.spec["paths"]["/noop_op"]["post"]
        self.assertNotIn("requestBody", op)
        self.assertIn("204", op["responses"])

    def test_components(self):
        schemas = self.spec["components"]["schemas"]
        self.assertEqual(schemas["Temporal"]["type"], "string")
        self.assertEqual(schemas["Temporal"]["x-meos-in"], "temporal_in")
        self.assertEqual(schemas["Temporal"]["x-meos-encodings"],
                         ["mfjson", "text", "wkb"])
        self.assertEqual(schemas["interpType"]["enum"], ["STEP", "LINEAR"])
        self.assertTrue(schemas["interpType"]["x-meos-c-enum"])
        self.assertIn("MeosError", self.spec["components"]["responses"])

    def test_all_refs_resolve(self):
        import json
        schemas = self.spec["components"]["schemas"]
        responses = self.spec["components"]["responses"]
        for ref in self._refs(self.spec):
            parts = ref.split("/")           # #/components/<kind>/<name>
            kind, name = parts[2], parts[3]
            target = schemas if kind == "schemas" else responses
            self.assertIn(name, target, f"dangling $ref {ref}")

    def _refs(self, node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k == "$ref":
                    yield v
                else:
                    yield from self._refs(v)
        elif isinstance(node, list):
            for v in node:
                yield from self._refs(v)


if __name__ == "__main__":
    unittest.main(verbosity=2)
