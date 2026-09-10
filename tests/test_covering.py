"""Unit tests for parser/covering.py and the descriptor shape.
python3 tests/test_covering.py
"""

import copy
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.covering import attach_temporal_covering, BBOX_3D

MAP = ROOT / "meta" / "temporal-covering.json"
SCHEMA = ROOT / "meta" / "temporal-covering.schema.json"


def _attach_variant(testcase, mutate):
    """Attach a mutated copy of the descriptor written to a scratch file."""
    bad = json.loads(MAP.read_text())
    mutate(bad)
    p = ROOT / "output" / "_variant_covering.json"
    p.parent.mkdir(exist_ok=True)
    p.write_text(json.dumps(bad))
    try:
        return attach_temporal_covering({}, p)
    finally:
        p.unlink()


def _covering(cov, tname, key):
    return next(c for c in cov["byType"][tname]["coverings"] if c["key"] == key)


class AttachTests(unittest.TestCase):
    def test_attaches_and_indexes(self):
        idl = attach_temporal_covering({"functions": []}, MAP)
        cov = idl["temporalCovering"]
        # tgeompoint resolves to the spatial class with an STBOX box
        self.assertEqual(cov["byType"]["tgeompoint"]["class"], "spatial")
        self.assertEqual(cov["byType"]["tgeompoint"]["box"]["type"], "STBOX")
        self.assertEqual(
            [c["key"] for c in cov["byType"]["tgeompoint"]["coverings"]],
            ["bbox", "tspan"])
        # tfloat resolves to the number class with a TBOX box
        self.assertEqual(cov["byType"]["tfloat"]["class"], "number")
        self.assertEqual(cov["byType"]["tfloat"]["box"]["type"], "TBOX")
        self.assertEqual(
            [c["key"] for c in cov["byType"]["tfloat"]["coverings"]],
            ["vspan", "tspan"])
        # tbool resolves to the time-only class with no box
        self.assertEqual(cov["byType"]["tbool"]["class"], "timeOnly")
        self.assertIsNone(cov["byType"]["tbool"]["box"])
        self.assertEqual(
            [c["key"] for c in cov["byType"]["tbool"]["coverings"]], ["tspan"])
        # count == number of covered types; types sorted
        self.assertEqual(cov["count"], len(cov["byType"]))
        self.assertEqual(cov["types"], sorted(cov["byType"]))

    def test_bbox_is_a_geoparquet_bounding_box_column(self):
        cov = attach_temporal_covering({}, MAP)["temporalCovering"]
        bbox = _covering(cov, "tgeompoint", "bbox")
        self.assertEqual(bbox["column"], "{col}_bbox")
        self.assertEqual(tuple(f["name"] for f in bbox["fields"]), BBOX_3D)
        self.assertEqual({f["sqlType"] for f in bbox["fields"]}, {"double"})
        self.assertEqual(
            [f["name"] for f in bbox["fields"] if f.get("when") == "hasZ"],
            ["zmin", "zmax"])

    def test_vspan_bounds_are_of_the_base_type(self):
        # the value bounds are read off the value in its base type, so a
        # bigint bound stays exact and no bound is rounded inward
        cov = attach_temporal_covering({}, MAP)["temporalCovering"]
        expected = {
            "tint":    ("int",    "tint_min_value",    "tint_max_value"),
            "tbigint": ("bigint", "tbigint_min_value", "tbigint_max_value"),
            "tfloat":  ("double", "tfloat_min_value",  "tfloat_max_value"),
        }
        for tname, (sql_type, vmin, vmax) in expected.items():
            vspan = _covering(cov, tname, "vspan")
            self.assertEqual(vspan["column"], "{col}_vspan")
            self.assertEqual(
                [(f["name"], f["sqlType"], f["accessor"], f["source"])
                 for f in vspan["fields"]],
                [("vmin", sql_type, vmin, "value"),
                 ("vmax", sql_type, vmax, "value")])

    def test_symbols_collected(self):
        cov = attach_temporal_covering({}, MAP)["temporalCovering"]
        # the value codec, both box converters, and the field accessors are
        # in the audit set
        for sym in ("temporal_as_hexwkb", "temporal_from_hexwkb",
                    "tspatial_to_stbox", "tnumber_to_tbox", "stbox_xmin",
                    "stbox_tmin", "tbox_tmin", "tspatial_srid",
                    "tint_min_value", "tbigint_max_value", "tfloat_min_value",
                    "temporal_start_timestamptz"):
            self.assertIn(sym, cov["symbols"])

    def test_missing_file_is_noop(self):
        idl = attach_temporal_covering({"x": 1}, ROOT / "nope.json")
        self.assertEqual(idl, {"x": 1})

    def test_duplicate_type_rejected(self):
        # claim tfloat in a second class too -> ambiguous codegen
        with self.assertRaises(ValueError):
            _attach_variant(self, lambda d: d["classes"]["spatial"]["types"]
                            .append("tfloat"))

    def test_misordered_bbox_rejected(self):
        # xmax before ymin is not the GeoParquet field order
        def swap(d):
            fields = d["classes"]["spatial"]["coverings"][0]["fields"]
            fields[1], fields[3] = fields[3], fields[1]
        with self.assertRaises(ValueError):
            _attach_variant(self, swap)

    def test_time_bounds_in_bbox_rejected(self):
        # a bbox carrying tmin is not a GeoParquet bounding box column
        def add_time(d):
            spatial = d["classes"]["spatial"]
            spatial["coverings"][0]["fields"].append(
                copy.deepcopy(spatial["coverings"][1]["fields"][0]))
        with self.assertRaises(ValueError):
            _attach_variant(self, add_time)

    def test_duplicate_covering_rejected(self):
        def dup(d):
            number = d["classes"]["number"]
            number["coverings"].append(copy.deepcopy(number["coverings"][1]))
        with self.assertRaises(ValueError):
            _attach_variant(self, dup)

    def test_by_type_missing_a_type_rejected(self):
        # a per-type covering must give fields for every type of its class
        def drop(d):
            del d["classes"]["number"]["coverings"][0]["byType"]["tbigint"]
        with self.assertRaises(ValueError):
            _attach_variant(self, drop)


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
