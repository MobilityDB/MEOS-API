"""Unit tests for generator/covering.py.
python3 tests/test_covering_projection.py

Also the CI gate: when the enriched catalog with `temporalCovering` is
present, every covered type projects to a well-formed covering expression
composed against the value.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.covering import attach_temporal_covering
from generator.covering import build_covering_projection

MAP = ROOT / "meta" / "temporal-covering.json"
_CATALOG = ROOT / "output" / "meos-idl.json"


def _projected():
    return build_covering_projection(attach_temporal_covering({}, MAP))


class ProjectionTests(unittest.TestCase):
    def test_spatial_box_composition(self):
        cols = {c["name"]: c
                for c in _projected()["types"]["tgeompoint"]["columns"]}
        # box columns compose accessor(box_from(VALUE))
        self.assertEqual(cols["xmin"]["expr"],
                         "stbox_xmin(tspatial_to_stbox(VALUE))")
        self.assertEqual(cols["xmin"]["sqlType"], "double")
        # srid is read off the value, not the box
        self.assertEqual(cols["srid"]["expr"], "tspatial_srid(VALUE)")
        # zmin is conditional on 3D
        self.assertEqual(cols["zmin"]["when"], "hasZ")

    def test_number_box_composition(self):
        t = _projected()["types"]["tfloat"]
        cols = {c["name"]: c for c in t["columns"]}
        self.assertEqual(t["boxType"], "TBOX")
        self.assertEqual(cols["vmin"]["expr"], "tbox_xmin(tnumber_to_tbox(VALUE))")
        self.assertEqual(cols["tmax"]["expr"], "tbox_tmax(tnumber_to_tbox(VALUE))")

    def test_count_and_codec(self):
        p = _projected()
        self.assertEqual(p["count"], len(p["types"]))
        self.assertEqual(p["valueCodec"]["asHexWkb"], "temporal_as_hexwkb")

    def test_requires_temporal_covering(self):
        with self.assertRaises(ValueError):
            build_covering_projection({"functions": []})


@unittest.skipUnless(_CATALOG.exists(), "run `python run.py` first")
class LiveProjectionGate(unittest.TestCase):
    def test_every_type_projects_wellformed(self):
        cat = attach_temporal_covering(json.loads(_CATALOG.read_text()), MAP)
        p = build_covering_projection(cat)
        self.assertEqual(p["count"], 13)
        # time-only types (tbool/ttext) project to tmin/tmax via the value, no box
        self.assertEqual(p["types"]["tbool"]["boxType"], None)
        self.assertEqual(
            {c["name"] for c in p["types"]["tbool"]["columns"]}, {"tmin", "tmax"})
        for spec in p["types"].values():
            self.assertTrue(spec["columns"])
            for c in spec["columns"]:
                # composed against the value, balanced parentheses
                self.assertIn("(VALUE)", c["expr"])
                self.assertEqual(c["expr"].count("("), c["expr"].count(")"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
