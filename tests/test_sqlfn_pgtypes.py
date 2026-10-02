"""The tags of a public MEOS function are read wherever the function is defined.

The PostgreSQL base types MEOS exposes are defined in the pgtypes library beside meos/src at the
repository root (jsonb_to_text in pgtypes/utils/jsonb.c), declared in the public headers, and
tagged on their definitions like every MEOS function. The readers of parser/sqlfn.py walk both
trees, so jsonb_to_text reaches its wrapper Jsonb_as_text and the SQL name asText it deploys.

Plain unittest, no pytest dependency; synthetic sources in a temp dir, as #DirectSqlfnTests of
tests/test_sqlfn_direct.py builds its trees.
"""
import tempfile
import unittest
from pathlib import Path

from parser.sqlfn import _meos_definition_files, _meos_to_mdb, attach_sqlfn_map

MEOS_C = """
/**
 * @ingroup meos_setspan_inout
 * @brief Return a set from its string representation
 * @csqlfn #Set_in()
 */
Set *
set_in(const char *str)
{
}
"""

PGTYPES_C = """
/**
 * @ingroup meos_json_base_conversion
 * @brief Return the text representation of a JSONB value
 * @csqlfn #Jsonb_as_text()
 */
text *
jsonb_to_text(const Jsonb *jb)
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

/**
 * @brief Return the text representation of a JSONB value
 * @sqlfn asText()
 */
Datum
Jsonb_as_text(PG_FUNCTION_ARGS)
{
}
"""


class PgtypesDefinitionTests(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for sub, name, text in (("meos/src", "set.c", MEOS_C),
                                ("pgtypes/utils", "jsonb.c", PGTYPES_C),
                                ("mobilitydb/src", "wrappers.c", MDB_C)):
            (root / sub).mkdir(parents=True)
            (root / sub / name).write_text(text)
        self.meos_src = root / "meos" / "src"
        self.mdb_src = root / "mobilitydb" / "src"

    def tearDown(self):
        self.tmp.cleanup()

    def test_the_definition_files_include_the_pgtypes_library(self):
        names = sorted(p.name for p in _meos_definition_files(self.meos_src))
        self.assertEqual(names, ["jsonb.c", "set.c"])

    def test_a_pgtypes_definition_reaches_its_wrapper(self):
        self.assertEqual(_meos_to_mdb(self.meos_src),
                         {"set_in": ["Set_in"], "jsonb_to_text": ["Jsonb_as_text"]})

    def test_a_pgtypes_function_takes_the_sql_name_its_wrapper_deploys(self):
        idl = {"functions": [{"name": "jsonb_to_text", "api": "public"},
                             {"name": "set_in", "api": "public"}]}
        idl, _, _ = attach_sqlfn_map(idl, self.meos_src, self.mdb_src)
        sqlfn = {f["name"]: f.get("sqlfn") for f in idl["functions"]}
        self.assertEqual(sqlfn, {"jsonb_to_text": "asText", "set_in": "set_in"})

    def test_a_tree_without_pgtypes_reads_meos_src_alone(self):
        (Path(self.tmp.name) / "pgtypes" / "utils" / "jsonb.c").unlink()
        (Path(self.tmp.name) / "pgtypes" / "utils").rmdir()
        (Path(self.tmp.name) / "pgtypes").rmdir()
        self.assertEqual(_meos_to_mdb(self.meos_src), {"set_in": ["Set_in"]})


if __name__ == "__main__":
    unittest.main()
