"""Unit tests for the base-to-collection type-relation registry.

Runs without libclang or pytest:  python3 tests/test_typerelations.py

A hermetic fixture exercises the parse and the inversion; a source check
asserts the canonical numeric mappings against the live MobilityDB tree when it
is available (skipped, never fabricated, when it is not).
"""
import os
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
        saved = os.environ.pop("MDB_SRC_ROOT", None)
        try:
            self.assertNotIn("typeRelations", attach_type_relations({}, None))
            self.assertNotIn("typeRelations", attach_type_relations({}, Path("/no/such/tree")))
        finally:
            if saved is not None:
                os.environ["MDB_SRC_ROOT"] = saved

    def test_mdb_src_root_resolves_when_object_model_root_is_absent(self):
        # The installed-headers build path resolves no meos/src root, but MDB_SRC_ROOT points at the
        # full checkout; the registry must still attach from there.
        with tempfile.TemporaryDirectory() as d:
            catalog = Path(d) / "meos" / "src" / "temporal"
            catalog.mkdir(parents=True)
            (catalog / "meos_catalog.c").write_text(_FIXTURE)
            saved = os.environ.get("MDB_SRC_ROOT")
            os.environ["MDB_SRC_ROOT"] = d
            try:
                by_base = attach_type_relations({}, None)["typeRelations"]["byBase"]
            finally:
                if saved is None:
                    os.environ.pop("MDB_SRC_ROOT", None)
                else:
                    os.environ["MDB_SRC_ROOT"] = saved
        self.assertEqual(by_base["float8"]["spanset"], "floatspanset")


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
