"""Unit tests for generator/covering.py.
python3 tests/test_covering_projection.py

Also the CI gate: when the enriched catalog with `temporalCovering` is
present, every covered type projects to well-formed covering columns
composed against the value.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.covering import attach_temporal_covering, BBOX_2D, BBOX_3D
from generator.covering import build_covering_projection

MAP = ROOT / "meta" / "temporal-covering.json"
_CATALOG = ROOT / "output" / "meos-idl.json"


def _projected():
    return build_covering_projection(attach_temporal_covering({}, MAP))


def _covering(projection, tname, key):
    return next(c for c in projection["types"][tname]["coverings"]
                if c["key"] == key)


class ProjectionTests(unittest.TestCase):
    def test_spatial_box_composition(self):
        p = _projected()
        bbox = _covering(p, "tgeompoint", "bbox")
        self.assertEqual(bbox["column"], "{col}_bbox")
        # the fields are in GeoParquet bounding box order
        self.assertEqual(tuple(f["name"] for f in bbox["fields"]), BBOX_3D)
        fields = {f["name"]: f for f in bbox["fields"]}
        # box fields compose accessor(box_from(VALUE))
        self.assertEqual(fields["xmin"]["expr"],
                         "stbox_xmin(tspatial_to_stbox(VALUE))")
        self.assertEqual(fields["xmin"]["sqlType"], "double")
        # zmin is conditional on 3D
        self.assertEqual(fields["zmin"]["when"], "hasZ")
        tspan = _covering(p, "tgeompoint", "tspan")
        self.assertEqual(tspan["column"], "{col}_tspan")
        self.assertEqual([f["expr"] for f in tspan["fields"]],
                         ["stbox_tmin(tspatial_to_stbox(VALUE))",
                          "stbox_tmax(tspatial_to_stbox(VALUE))"])
        # srid is a plain column read off the value, not the box
        cols = {c["name"]: c for c in p["types"]["tgeompoint"]["columns"]}
        self.assertEqual(cols["srid"]["expr"], "tspatial_srid(VALUE)")

    def test_number_box_composition(self):
        p = _projected()
        t = p["types"]["tfloat"]
        self.assertEqual(t["boxType"], "TBOX")
        self.assertEqual([c["key"] for c in t["coverings"]], ["vspan", "tspan"])
        tspan = {f["name"]: f for f in _covering(p, "tfloat", "tspan")["fields"]}
        self.assertEqual(tspan["tmax"]["expr"], "tbox_tmax(tnumber_to_tbox(VALUE))")
        self.assertEqual(t["columns"], [])

    def test_number_value_bounds_per_base_type(self):
        # each numeric type reads its value bounds off the value, typed as
        # its base type
        p = _projected()
        for tname, sql_type in (("tint", "int"), ("tbigint", "bigint"),
                                ("tfloat", "double")):
            vspan = _covering(p, tname, "vspan")
            self.assertEqual(
                [(f["name"], f["sqlType"], f["expr"]) for f in vspan["fields"]],
                [("vmin", sql_type, f"{tname}_min_value(VALUE)"),
                 ("vmax", sql_type, f"{tname}_max_value(VALUE)")])

    def test_time_only_composition(self):
        p = _projected()
        t = p["types"]["tbool"]
        self.assertIsNone(t["boxType"])
        self.assertEqual([c["key"] for c in t["coverings"]], ["tspan"])
        tspan = {f["name"]: f for f in _covering(p, "tbool", "tspan")["fields"]}
        self.assertEqual(tspan["tmin"]["expr"], "temporal_start_timestamptz(VALUE)")

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
        # time-only types (tbool/ttext) project to a tspan read off the value
        self.assertEqual(p["types"]["tbool"]["boxType"], None)
        self.assertEqual(
            [f["name"] for f in _covering(p, "tbool", "tspan")["fields"]],
            ["tmin", "tmax"])
        for spec in p["types"].values():
            self.assertTrue(spec["coverings"])
            for covering in spec["coverings"]:
                self.assertTrue(covering["column"].startswith("{col}_"))
                if covering["key"] == "bbox":
                    names = tuple(f["name"] for f in covering["fields"])
                    self.assertIn(names, (BBOX_2D, BBOX_3D))
                for f in covering["fields"] + spec["columns"]:
                    # composed against the value, balanced parentheses
                    self.assertIn("(VALUE)", f["expr"])
                    self.assertEqual(f["expr"].count("("), f["expr"].count(")"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
