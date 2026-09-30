"""A SQL signature states that it returns a set.

`RETURNS SETOF integer` returns any number of rows of one integer each, and
`RETURNS integer` exactly one, so the row type alone does not state what a
binding registers: Flink and Spark carry a set-returning signature as one
function returning an array and unfold it into rows. The parser keeps
`SETOF` as the signature's `retSet`, PostgreSQL's `proretset`, beside `ret`,
the type of one row. A signature returning one value carries no `retSet`.

Plain unittest, no pytest dependency; synthetic sources via a temp dir.
"""
import tempfile
import unittest
from pathlib import Path

from parser.sqlfn import _create_fn_stmts, _wrapper_sql_sigs, attach_sqlfn_map

MEOS_C = """
/**
 * @ingroup meos_setspan_accessor
 * @brief Return the array of values of an integer set
 * @csqlfn #Set_values(), #Set_unnest()
 */
int *
intset_values(const Set *s)
{
}
"""

MDB_C = """
/**
 * @brief Return the array of values of a set
 * @sqlfn getValues()
 */
Datum
Set_values(PG_FUNCTION_ARGS)
{
}

/**
 * @brief Return the values of a set as rows
 * @sqlfn unnest()
 */
Datum
Set_unnest(PG_FUNCTION_ARGS)
{
}
"""

MDB_SQL = """
CREATE FUNCTION getValues(intset)
  RETURNS integer[]
  AS 'MODULE_PATHNAME', 'Set_values'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION unnest(intset)
  RETURNS SETOF integer
  AS 'MODULE_PATHNAME', 'Set_unnest'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION aDisjointPairs(tgeometry[], tgeometry[], OUT i integer, OUT j integer)
  RETURNS setof record
  AS 'MODULE_PATHNAME', 'Adisjoint_tgeoarr_tgeoarr'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
"""


def _attach(names):
    idl = {"functions": [{"name": n, "api": "public"} for n in names]}
    with tempfile.TemporaryDirectory() as d:
        meos = Path(d) / "meos" / "src"
        mdb = Path(d) / "mdb"
        sql = Path(d) / "sql"
        for p in (meos, mdb, sql):
            p.mkdir(parents=True)
        (meos / "temporal").mkdir()
        (meos / "temporal" / "meos_catalog.c").write_text("")
        (meos / "x.c").write_text(MEOS_C)
        (mdb / "y.c").write_text(MDB_C)
        (sql / "z.sql").write_text(MDB_SQL)
        idl, _, _ = attach_sqlfn_map(idl, str(meos), str(mdb), str(sql))
    return {f["name"]: f for f in idl["functions"]}


class SetofStatementTests(unittest.TestCase):

    def test_setof_is_kept_apart_from_the_row_type(self):
        stmts = {name: (ret, retset)
                 for name, _, ret, _, retset in _create_fn_stmts(MDB_SQL)}
        self.assertEqual(stmts["unnest"], ("integer", True))
        self.assertEqual(stmts["getValues"], ("integer[]", False))

    def test_setof_is_read_in_any_case(self):
        stmts = {name: (ret, retset)
                 for name, _, ret, _, retset in _create_fn_stmts(MDB_SQL)}
        self.assertEqual(stmts["aDisjointPairs"], ("record", True))

    def test_every_wrapper_signature_states_its_retset(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.sql").write_text(MDB_SQL)
            sigs = _wrapper_sql_sigs(d)
        self.assertTrue(sigs["Set_unnest"][0]["retSet"])
        self.assertFalse(sigs["Set_values"][0]["retSet"])


class SetofCatalogTests(unittest.TestCase):

    def test_the_set_returning_signature_carries_retset(self):
        """`intset_values` backs getValues and unnest: only unnest returns rows."""
        f = _attach(["intset_values"])["intset_values"]
        by_name = {s.get("sqlName", f["sqlfn"]): s for s in f["sqlSignatures"]}
        self.assertEqual(by_name["unnest"], {"args": ["intset"], "ret": "integer",
                                             "retSet": True, "sqlName": "unnest"})
        self.assertNotIn("retSet", by_name["getValues"])


if __name__ == "__main__":
    unittest.main()
