"""Unit tests for generator/mcp.py.

Runs without libclang or pytest:  python3 tests/test_mcp.py
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from generator.mcp import build_mcp

TEMP = "const struct Temporal *"


def serialized(name, ctype, decode):
    return {"name": name, "kind": "serialized", "cType": ctype,
            "decode": decode, "encodings": ["mfjson", "text", "wkb"]}


CATALOG = {
    "functions": [
        {
            "name": "temporal_eq", "category": "predicate",
            "network": {"exposable": True},
            "wire": {
                "params": [serialized("temp1", TEMP, "temporal_in"),
                           serialized("temp2", TEMP, "temporal_in")],
                "result": {"kind": "json", "json": "integer"},
            },
        },
        {
            "name": "temporal_set_interp", "category": "transformation",
            "doc": "Set the interpolation of a temporal value.",
            "network": {"exposable": True},
            "wire": {
                "params": [
                    serialized("temp", TEMP, "temporal_in"),
                    {"name": "interp", "kind": "json", "json": "string",
                     "enum": "interpType"},
                ],
                "result": {"kind": "serialized", "cType": "struct Temporal *",
                           "encode": "temporal_out",
                           "encodings": ["text"]},
            },
        },
        {
            "name": "noop_op", "category": "transformation",
            "network": {"exposable": True},
            "wire": {"params": [], "result": {"kind": "void"}},
        },
        {
            "name": "tsequence_make", "category": "constructor",
            "network": {"exposable": False,
                        "reason": "array-or-out-param:instants"},
            "wire": {"params": [], "result": {"kind": "unsupported"}},
        },
        {
            "name": "temporal_merge_array", "category": "transformation",
            "network": {"exposable": True},
            "wire": {
                "params": [{"name": "temparr", "kind": "array",
                            "count_param": "count",
                            "element": {"kind": "serialized",
                                        "cType": "struct Temporal *",
                                        "decode": "temporal_in",
                                        "encodings": ["text"]}}],
                "result": {"kind": "serialized",
                           "cType": "struct Temporal *",
                           "encode": "temporal_out"}},
        },
    ],
    "enums": [{"name": "interpType",
               "values": [{"name": "STEP", "value": 0},
                          {"name": "LINEAR", "value": 1}]}],
    "structs": [],
}


class McpTests(unittest.TestCase):
    def setUp(self):
        self.m = build_mcp(CATALOG)
        self.tools = {t["name"]: t for t in self.m["tools"]}

    def test_envelope_and_exclusion(self):
        self.assertEqual(self.m["x-meos"]["coverage"],
                         {"functions": 5, "exposed": 4})
        self.assertNotIn("tsequence_make", self.tools)
        self.assertEqual(len(self.m["tools"]), 4)

    def test_array_param_inlined(self):
        t = self.tools["temporal_merge_array"]
        a = t["inputSchema"]["properties"]["temparr"]
        self.assertEqual(a["type"], "array")
        self.assertEqual(a["items"]["type"], "string")     # serialized elem
        self.assertIn("MEOS", a["items"]["description"])

    def test_tools_sorted(self):
        names = [t["name"] for t in self.m["tools"]]
        self.assertEqual(names, sorted(names))

    def test_input_schema_and_serialized_param(self):
        t = self.tools["temporal_eq"]
        s = t["inputSchema"]
        self.assertEqual(s["$schema"],
                         "https://json-schema.org/draft/2020-12/schema")
        self.assertEqual(s["type"], "object")
        self.assertEqual(s["required"], ["temp1", "temp2"])
        self.assertFalse(s["additionalProperties"])
        p = s["properties"]["temp1"]
        self.assertEqual(p["type"], "string")
        self.assertIn("MEOS Temporal", p["description"])
        self.assertEqual(t["x-meos"]["decode"],
                         {"temp1": "temporal_in", "temp2": "temporal_in"})
        self.assertEqual(t["x-meos"]["category"], "predicate")
        # scalar result -> wrapped outputSchema
        self.assertEqual(
            t["outputSchema"]["properties"]["result"], {"type": "integer"})

    def test_enum_param_inlined(self):
        t = self.tools["temporal_set_interp"]
        interp = t["inputSchema"]["properties"]["interp"]
        self.assertEqual(interp["type"], "string")
        self.assertEqual(interp["enum"], ["STEP", "LINEAR"])
        self.assertEqual(t["description"],
                         "Set the interpolation of a temporal value. "
                         "Spatiotemporal arguments are passed as serialized "
                         "strings (text/WKT, MF-JSON, or HexWKB).")
        self.assertEqual(t["x-meos"]["encode"], "temporal_out")
        self.assertEqual(
            t["outputSchema"]["properties"]["result"]["type"], "string")

    def test_void_has_no_output_schema(self):
        t = self.tools["noop_op"]
        self.assertNotIn("outputSchema", t)
        self.assertEqual(t["inputSchema"]["properties"], {})
        self.assertEqual(t["inputSchema"]["required"], [])

    def test_annotations(self):
        a = self.tools["temporal_eq"]["annotations"]
        self.assertTrue(a["readOnlyHint"])
        self.assertTrue(a["idempotentHint"])
        self.assertFalse(a["destructiveHint"])
        self.assertFalse(a["openWorldHint"])

    def test_all_tools_well_formed(self):
        for t in self.m["tools"]:
            self.assertTrue(t["name"])
            self.assertTrue(t["description"])
            self.assertEqual(t["inputSchema"]["type"], "object")
            self.assertLessEqual(
                set(t["inputSchema"]["required"]),
                set(t["inputSchema"]["properties"]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
