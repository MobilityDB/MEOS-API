"""Unit tests for the base-to-collection type-relation registry.

Runs without libclang or pytest:  python3 tests/test_typerelations.py

A hermetic fixture exercises the parse and the inversion; a source check
asserts the canonical numeric mappings against the live MobilityDB tree when it
is available (skipped, never fabricated, when it is not).
"""
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.typerelations import attach_type_relations
from parser.object_model import find_mobilitydb_src

_FIXTURE = """
static const char *MEOS_TYPE_NAMES[] =
{
  [T_FLOAT8] = "float8",
  [T_FLOATSET] = "floatset",
  [T_FLOATSPAN] = "floatspan",
  [T_FLOATSPANSET] = "floatspanset",
  [T_TFLOAT] = "tfloat",
  [T_TEXT] = "text",
  [T_TEXTSET] = "textset",
  [T_TTEXT] = "ttext",
};
static const settype_catalog_struct MEOS_SETTYPE_CATALOG[] =
{
  {T_FLOATSET,  T_FLOAT8},
  {T_TEXTSET,   T_TEXT},
};
static const spantype_catalog_struct MEOS_SPANTYPE_CATALOG[] =
{
  {T_FLOATSPAN, T_FLOAT8},
};
static const spansettype_catalog_struct MEOS_SPANSETTYPE_CATALOG[] =
{
  {T_FLOATSPANSET, T_FLOATSPAN},
};
static const temptype_catalog_struct MEOS_TEMPTYPE_CATALOG[] =
{
  {T_TFLOAT, T_FLOAT8},
  {T_TTEXT,  T_TEXT},
};
"""


class TypeRelationsParseTest(unittest.TestCase):

    def _attach(self, text):
        with tempfile.TemporaryDirectory() as d:
            catalog = Path(d) / "temporal"
            catalog.mkdir()
            (catalog / "meos_catalog.c").write_text(text)
            return attach_type_relations({}, Path(d))["typeRelations"]["byBase"]

    def test_full_numeric_base_resolves_all_four_templates(self):
        by_base = self._attach(_FIXTURE)
        self.assertEqual(by_base["float8"], {
            "temporal": "tfloat", "set": "floatset",
            "span": "floatspan", "spanset": "floatspanset"})

    def test_non_orderable_base_has_set_but_no_span(self):
        # text has a set and a temporal type but no span/span set.
        by_base = self._attach(_FIXTURE)
        self.assertEqual(by_base["text"], {"temporal": "ttext", "set": "textset"})

    def test_absent_source_degrades_without_fabricating(self):
        self.assertNotIn("typeRelations", attach_type_relations({}, None))
        self.assertNotIn("typeRelations", attach_type_relations({}, Path("/no/such/tree")))


class TypeRelationsSourceTest(unittest.TestCase):

    def test_canonical_numeric_mappings(self):
        src = find_mobilitydb_src()
        if src is None:
            self.skipTest("MobilityDB source not available")
        by_base = attach_type_relations({}, src)["typeRelations"]["byBase"]
        self.assertEqual(by_base["float8"]["spanset"], "floatspanset")
        self.assertEqual(by_base["int4"], {
            "temporal": "tint", "set": "intset",
            "span": "intspan", "spanset": "intspanset"})
        self.assertEqual(by_base["int8"]["span"], "bigintspan")
        self.assertNotIn("span", by_base["text"])


if __name__ == "__main__":
    unittest.main()
