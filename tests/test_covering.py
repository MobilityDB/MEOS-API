"""Unit tests for parser/covering.py and the descriptor shape.
python3 tests/test_covering.py
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.covering import attach_temporal_covering

MAP = ROOT / "meta" / "temporal-covering.json"
SCHEMA = ROOT / "meta" / "temporal-covering.schema.json"


class AttachTests(unittest.TestCase):
    def test_attaches_and_indexes(self):
        idl = attach_temporal_covering({"functions": []}, MAP)
        cov = idl["temporalCovering"]
        # tgeompoint resolves to the spatial class with an STBOX box
        self.assertEqual(cov["byType"]["tgeompoint"]["class"], "spatial")
        self.assertEqual(cov["byType"]["tgeompoint"]["box"]["type"], "STBOX")
        # tfloat resolves to the number class with a TBOX box
        self.assertEqual(cov["byType"]["tfloat"]["class"], "number")
        self.assertEqual(cov["byType"]["tfloat"]["box"]["type"], "TBOX")
        # tbool resolves to the time-only class with no box
        self.assertEqual(cov["byType"]["tbool"]["class"], "timeOnly")
        self.assertIsNone(cov["byType"]["tbool"]["box"])
        # count == number of covered types; types sorted
        self.assertEqual(cov["count"], len(cov["byType"]))
        self.assertEqual(cov["types"], sorted(cov["byType"]))

    def test_symbols_collected(self):
        cov = attach_temporal_covering({}, MAP)["temporalCovering"]
        # the value codec and both box converters are in the audit set
        for sym in ("temporal_as_hexwkb", "temporal_from_hexwkb",
                    "tspatial_to_stbox", "tnumber_to_tbox", "stbox_xmin",
                    "tbox_xmin", "tspatial_srid"):
            self.assertIn(sym, cov["symbols"])

    def test_missing_file_is_noop(self):
        idl = attach_temporal_covering({"x": 1}, ROOT / "nope.json")
        self.assertEqual(idl, {"x": 1})

    def test_duplicate_type_rejected(self):
        bad = json.loads(MAP.read_text())
        # claim tfloat in a second class too -> ambiguous codegen
        bad["classes"]["spatial"]["types"].append("tfloat")
        p = ROOT / "output" / "_dup_covering.json"
        p.parent.mkdir(exist_ok=True)
        p.write_text(json.dumps(bad))
        try:
            with self.assertRaises(ValueError):
                attach_temporal_covering({}, p)
        finally:
            p.unlink()


class SchemaTests(unittest.TestCase):
    def test_descriptor_validates(self):
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed")
        jsonschema.validate(
            json.loads(MAP.read_text()), json.loads(SCHEMA.read_text()))


if __name__ == "__main__":
    unittest.main(verbosity=2)
