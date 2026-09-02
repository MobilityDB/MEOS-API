"""Unit tests for the temporal-type registry.

Runs without libclang or pytest:  python3 tests/test_temporaltypes.py

A hermetic fixture exercises the parse, including the two shapes a pattern gets
wrong: a brace inside a string literal, and case labels that fall through in a
group. A source check asserts the live MobilityDB tree when it is available
(skipped, never fabricated, when it is not).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.object_model import find_mobilitydb_src
from parser.temporaltypes import (attach_temporal_types, function_body,
                                  mfjson_tokens, strip_comments)
from parser.typerelations import locate_temporal_source

_CATALOG = """
static const char *MEOS_TYPE_NAMES[] =
{
  [T_FLOAT8] = "float8",
  [T_TFLOAT] = "tfloat",
  [T_GEOMETRY] = "geometry",
  [T_TGEOMPOINT] = "tgeompoint",
  [T_TGEOMETRY] = "tgeometry",
  [T_DOUBLE2] = "double2",
  [T_TDOUBLE2] = "tdouble2",
  [T_TBOX] = "tbox",
  [T_STBOX] = "stbox",
  [T_TSTZSPAN] = "tstzspan",
};
static const reltype_catalog_struct MEOS_RELTYPE_CATALOG[] =
{
  [T_TFLOAT] = { .type_bboxtype = T_TBOX, .temptype_basetype = T_FLOAT8 },
  [T_TGEOMPOINT] = { .type_bboxtype = T_STBOX, .temptype_basetype = T_GEOMETRY },
  [T_TGEOMETRY] = { .type_bboxtype = T_STBOX, .temptype_basetype = T_GEOMETRY },
  [T_TDOUBLE2] = { .type_bboxtype = T_TSTZSPAN, .temptype_basetype = T_DOUBLE2 },
};
/* A comment naming T_TQUADBIN, which is not a member of any list below. */
bool
temporal_type(MeosType type)
{
  return (type == T_TFLOAT || type == T_TGEOMPOINT || type == T_TGEOMETRY ||
    type == T_TDOUBLE2);
}
bool
tnumber_type(MeosType type)
{
  return (type == T_TFLOAT);
}
bool
tspatial_type(MeosType type)
{
  return (type == T_TGEOMPOINT
#if POINTCLOUD
    || type == T_TGEOMETRY
#endif
    );
}
bool
temptype_supports_linear(MeosType type)
{
  bool result = (type == T_TFLOAT || type == T_TGEOMPOINT || type == T_TDOUBLE2);
  return result;
}
"""

_TYPE_OUT = r"""
static bool
temptype_as_mfjson_sb(stringbuffer_t *sb, MeosType temptype)
{
  switch (temptype)
  {
    case T_TFLOAT:
      stringbuffer_append_len(sb, "{\"type\":\"MovingFloat\",", 22);
      break;
    case T_TGEOMPOINT:
    case T_TGEOMETRY:
      stringbuffer_append_len(sb, "{\"type\":\"MovingPoint\",", 22);
      break;
    default:
      return false;
  }
  return true;
}
"""


class TemporalTypesParseTest(unittest.TestCase):

    def _attach(self, catalog=_CATALOG, type_out=_TYPE_OUT):
        with tempfile.TemporaryDirectory() as d:
            src = Path(d) / "temporal"
            src.mkdir()
            (src / "meos_catalog.c").write_text(catalog)
            (src / "type_out.c").write_text(type_out)
            return attach_temporal_types({}, Path(d))["temporalTypes"]

    def test_every_temporal_type_carries_its_base_box_and_classes(self):
        types = self._attach()
        self.assertEqual(types["tfloat"], {
            "base": "float8", "bbox": "tbox", "mfjson": "MovingFloat",
            "number": True, "spatial": False, "linear": True})
        self.assertEqual(types["tgeompoint"], {
            "base": "geometry", "bbox": "stbox", "mfjson": "MovingPoint",
            "number": False, "spatial": True, "linear": True})

    def test_a_type_the_mfjson_switch_does_not_name_carries_no_token(self):
        # asMFJSON has no form for it, which is a fact about the type rather than a
        # gap in the parse, so the key is absent rather than empty.
        types = self._attach()
        self.assertNotIn("mfjson", types["tdouble2"])
        self.assertEqual(types["tdouble2"]["base"], "double2")

    def test_case_labels_falling_through_share_one_token(self):
        # A geometry point and a geography point are both a MovingPoint, so the labels
        # accumulate until a token is written and are assigned together.
        types = self._attach()
        self.assertEqual(types["tgeompoint"]["mfjson"], "MovingPoint")
        self.assertEqual(types["tgeometry"]["mfjson"], "MovingPoint")

    def test_a_family_guard_inside_a_predicate_does_not_hide_a_type(self):
        # The type exists when its family is built, so a #if around a member of the list
        # is not a reason to read the type as unclassified.
        self.assertTrue(self._attach()["tgeometry"]["spatial"])

    def test_a_type_named_only_in_a_comment_is_not_a_member(self):
        self.assertNotIn("tquadbin", self._attach())

    def test_a_brace_in_a_string_literal_is_text(self):
        # Every MF-JSON token is written as `{\"type\":\"...\",`, so counting the brace
        # inside it opens a depth that never closes and the body is never found.
        body = function_body(strip_comments(_TYPE_OUT), "temptype_as_mfjson_sb")
        self.assertIn("MovingFloat", body)
        self.assertNotIn("temptype_as_mfjson_sb", body)

    def test_a_renamed_predicate_raises(self):
        # Reading no predicate would emit every type with the class false, which is a
        # registry that lies rather than one that is absent.
        without = _CATALOG.replace("tspatial_type(MeosType type)", "tspatial_kind(MeosType type)")
        with self.assertRaises(ValueError):
            self._attach(catalog=without)

    def test_a_lost_mfjson_switch_raises(self):
        with self.assertRaises(ValueError):
            self._attach(type_out="/* nothing here */\n")

    def test_absent_source_degrades_without_fabricating(self):
        saved = os.environ.pop("MDB_SRC_ROOT", None)
        try:
            self.assertNotIn("temporalTypes", attach_temporal_types({}, None))
            self.assertNotIn("temporalTypes", attach_temporal_types({}, Path("/no/such/tree")))
        finally:
            if saved is not None:
                os.environ["MDB_SRC_ROOT"] = saved


class TemporalTypesSourceTest(unittest.TestCase):

    def test_the_live_registry_names_every_type_and_its_token(self):
        src = find_mobilitydb_src()
        if src is None and locate_temporal_source(None, "meos_catalog.c") is None:
            self.skipTest("MobilityDB source not available")
        types = attach_temporal_types({}, src)["temporalTypes"]
        # The two temporal types over a shared base, which a one-per-base registry loses.
        self.assertEqual(types["tpose"]["base"], "pose")
        self.assertEqual(types["trgeometry"]["base"], "pose")
        # The token differs per type, which is why it cannot be derived from the name.
        self.assertEqual(types["tgeompoint"]["mfjson"], "MovingPoint")
        self.assertEqual(types["tcbuffer"]["mfjson"], "MovingCircularBuffer")
        self.assertEqual(types["trgeometry"]["mfjson"], "MovingRigidGeometry")
        self.assertEqual(types["tbigint"]["mfjson"], "MovingBigInteger")
        # The classes a binding dispatches on.
        self.assertTrue(types["tpose"]["spatial"])
        self.assertFalse(types["tpose"]["number"])
        self.assertTrue(types["tbigint"]["number"])
        self.assertFalse(types["tbigint"]["spatial"])
        self.assertTrue(types["tfloat"]["linear"])
        self.assertFalse(types["tint"]["linear"])


if __name__ == "__main__":
    unittest.main()
