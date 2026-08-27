"""Regression tests for the direct one-hop @sqlfn fallback in parser/sqlfn.py.

A surface whose PostgreSQL registration is deferred to a host extension (the
h3index scalar functions defer to h3-pg) has no PG wrapper to carry @sqlfn, so
the MEOS function carries the tag itself. attach_sqlfn_map consults that direct
map ONLY for functions the @csqlfn wrapper chain does not resolve — fill-only,
never an override.

Plain unittest, no pytest dependency; synthetic sources via a temp dir.
"""
import tempfile
import unittest
from pathlib import Path

from parser.sqlfn import _meos_direct_sql, attach_sqlfn_map

MEOS_C = """
/**
 * @ingroup meos_h3_base_inout
 * @brief Parse a string into an H3Index
 * @sqlfn h3index_in()
 */
H3Index
h3index_in(const char *str)
{
}

/**
 * @ingroup meos_h3_base_comp
 * @brief Return true if two h3index values are equal
 * @sqlop @p =
 */
bool
h3index_eq(H3Index a, H3Index b)
{
}

/**
 * @ingroup meos_setspan_inout
 * @brief Return a set from its string representation
 * @sqlfn set_in_direct_should_lose()
 * @csqlfn #Set_in()
 */
Set *
set_in(const char *str)
{
}
"""

MDB_C = """
/**
 * @brief Return a set from its string representation
 * @sqlfn set_in()
 */
Datum
Set_in(PG_FUNCTION_ARGS)
{
}
"""


class DirectSqlfnTests(unittest.TestCase):
    def _trees(self, d):
        meos = Path(d) / "meos"
        mdb = Path(d) / "mdb"
        meos.mkdir()
        mdb.mkdir()
        (meos / "x.c").write_text(MEOS_C)
        (mdb / "y.c").write_text(MDB_C)
        return str(meos), str(mdb)

    def test_multi_op_tag_keeps_the_operator_without_its_comma(self):
        """A block naming several SQL functions lists their operators the same
        comma-separated way `@sqlfn` lists the names, so the operator ends at the
        comma. Reading to the next whitespace published `->,` as the operator."""
        src = """
/**
 * @ingroup meos_json_json
 * @brief Extract a field from a temporal JSONB value
 * @sqlfn tjsonbObjectField(), tjsonbObjectFieldText()
 * @sqlop @p ->, @p ->>
 */
Temporal *
tjsonb_object_field(const Temporal *temp, const text *key)
{
}
"""
        with tempfile.TemporaryDirectory() as d:
            meos = Path(d) / "meos"
            meos.mkdir()
            (meos / "j.c").write_text(src)
            direct = _meos_direct_sql(str(meos))
        self.assertEqual(direct.get("tjsonb_object_field"),
                         ("tjsonbObjectField", "->"))

    def test_direct_map_needs_sqlfn_and_skips_csqlfn_blocks(self):
        with tempfile.TemporaryDirectory() as d:
            meos, _ = self._trees(d)
            direct = _meos_direct_sql(meos)
        # Name-bearing block resolves; @sqlop-only block stays out (same anchor
        # the wrapper-side scan uses); a block with @csqlfn keeps the two-hop chain.
        self.assertEqual(direct.get("h3index_in"), ("h3index_in", None))
        self.assertNotIn("h3index_eq", direct)
        self.assertNotIn("set_in", direct)

    def test_attach_fills_only_unresolved_functions(self):
        idl = {"functions": [{"name": "h3index_in"}, {"name": "set_in"}]}
        with tempfile.TemporaryDirectory() as d:
            meos, mdb = self._trees(d)
            idl, n, _ = attach_sqlfn_map(idl, meos, mdb)
        by = {f["name"]: f for f in idl["functions"]}
        # Deferred surface: filled from the direct tag.
        self.assertEqual(by["h3index_in"]["sqlfn"], "h3index_in")
        self.assertNotIn("mdbC", by["h3index_in"])
        # Wrapper-resolved surface: the two-hop chain wins over the direct tag.
        self.assertEqual(by["set_in"]["sqlfn"], "set_in")
        self.assertEqual(by["set_in"]["mdbC"], "Set_in")
        self.assertEqual(n, 2)


if __name__ == "__main__":
    unittest.main()
