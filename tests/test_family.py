"""Regression tests for the ``family`` classification in parser/extractors.py.

Every IDL function/struct/enum carries a ``family`` field derived from the
declaring header: the ``meos/include/<family>/`` subdirectory (or the top-level
``meos_<family>.h`` public header) names the optional type family, and
everything else — the temporal core, the base geo/tpoint types, the shared
top-level headers — is ``CORE``. A binding gates a family in or out purely by
this field, so an edge build can drop unused families (e.g. POINTCLOUD) to
shrink its footprint.

These assert each optional family is populated, that representative symbols land
in the right family, and that CORE stays the base. Plain unittest, no pytest
dependency.

The IDL is generated, not committed; run ``python run.py`` first.
"""
import json
import unittest
from pathlib import Path

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"

OPTIONAL_FAMILIES = {
    "CBUFFER", "NPOINT", "POSE", "RGEO", "H3",
    "QUADBIN", "POINTCLOUD", "JSON", "ARROW", "RASTER",
}


class FamilyClassificationTests(unittest.TestCase):
    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        idl = json.loads(IDL.read_text())
        self.functions = idl["functions"]
        self.by_name = {f["name"]: f for f in self.functions}

    def _family(self, name):
        self.assertIn(name, self.by_name, f"{name} missing from IDL")
        return self.by_name[name]["family"]

    def test_every_record_carries_a_family(self):
        for f in self.functions:
            self.assertIn("family", f, f"{f['name']} has no family")

    def test_representative_symbols_land_in_their_family(self):
        cases = {
            "cbuffer_out": "CBUFFER",
            "npoint_out": "NPOINT",
            "pose_out": "POSE",
            "trgeometry_out": "RGEO",
            "eintersects_trgeometry_geo": "RGEO",
            "h3index_out": "H3",
            "pcpoint_hex_out": "POINTCLOUD",
            "jsonb_in": "JSON",
            "tquadbin_in": "QUADBIN",
        }
        for name, family in cases.items():
            self.assertEqual(self._family(name), family, name)

    def test_core_symbols_are_core(self):
        # temporal core + base geo/tpoint stay CORE (never an optional family).
        for name in ("temporal_eq", "tint_values", "stbox_out", "bigintset_out"):
            self.assertEqual(self._family(name), "CORE", name)

    def test_each_optional_family_is_populated(self):
        # Hard guard: a healthy full-surface IDL populates every optional family;
        # an empty one means the header layout or the classifier regressed.
        present = {f["family"] for f in self.functions}
        for family in OPTIONAL_FAMILIES - {"ARROW", "RASTER"}:
            self.assertIn(family, present, f"{family} unpopulated — classifier regression?")

    def test_families_are_known_labels(self):
        known = OPTIONAL_FAMILIES | {"CORE"}
        for f in self.functions:
            self.assertIn(f["family"], known, f"{f['name']}: unknown family {f['family']}")


if __name__ == "__main__":
    unittest.main()
