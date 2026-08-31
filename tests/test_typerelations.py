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

from parser.typerelations import attach_type_relations, locate_catalog
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
  [T_GEOMETRY] = "geometry",
  [T_TGEOMPOINT] = "tgeompoint",
  [T_TGEOMETRY] = "tgeometry",
};
static const reltype_catalog_struct MEOS_RELTYPE_CATALOG[] =
{
  [T_FLOAT8] = { .basetype_settype = T_FLOATSET,
    .basetype_spantype = T_FLOATSPAN },
  [T_FLOATSET] = { .type_bboxtype = T_FLOATSPAN, .settype_basetype = T_FLOAT8 },
  [T_FLOATSPAN] = { .spantype_basetype = T_FLOAT8,
    .spantype_spansettype = T_FLOATSPANSET },
  [T_FLOATSPANSET] = { .spansettype_spantype = T_FLOATSPAN },
  [T_TFLOAT] = { .type_bboxtype = T_TBOX, .temptype_basetype = T_FLOAT8 },
  [T_TEXT] = { .basetype_settype = T_TEXTSET },
  [T_TEXTSET] = { .settype_basetype = T_TEXT },
  [T_TTEXT] = { .type_bboxtype = T_TSTZSPAN, .temptype_basetype = T_TEXT },
  [T_TGEOMPOINT] = { .type_bboxtype = T_STBOX, .temptype_basetype = T_GEOMETRY },
  [T_TGEOMETRY] = { .type_bboxtype = T_STBOX, .temptype_basetype = T_GEOMETRY },
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

    def test_base_shared_by_several_temporal_types_resolves_in_meostype_order(self):
        # A geometry is the base of both tgeompoint and tgeometry; the catalog names no
        # temporal type at the base's own entry, so the role is the inverse relation
        # resolved in MeosType order — the last entry, as the arrays of pairs resolved it.
        by_base = self._attach(_FIXTURE)
        self.assertEqual(by_base["geometry"]["temporal"], "tgeometry")

    def test_catalog_without_the_relation_array_raises(self):
        # A located catalog the parse reads no relation out of is a lost array, not a
        # catalog without types: it must raise rather than attach an empty registry that
        # silently degrades every consumer resolving a concrete collection type.
        names_only = _FIXTURE[:_FIXTURE.index("static const reltype_catalog_struct")]
        with self.assertRaises(ValueError):
            self._attach(names_only)

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
        # Resolve the tree the way the extractor does, so the live assertion runs wherever the
        # extractor runs: find_mobilitydb_src reads $MOBILITYDB_SRC, while the provisioning that
        # derives the catalog checks the repository out under $MDB_SRC_ROOT, which locate_catalog
        # consults. Resolving through only the first skipped this check on the build path that
        # produces the catalog, which is the path whose drift it exists to catch.
        src = find_mobilitydb_src()
        if src is None and locate_catalog(None) is None:
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
