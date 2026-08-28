"""Regression tests for @sqlaggfn — the SQL AGGREGATE a PG wrapper serves.

An aggregate's transition/combine/final wrapper backs a CREATE FUNCTION nobody
calls (`tcount_transfn`) while implementing a CREATE AGGREGATE everybody does
(`tCount`). @sqlfn states the first and @sqlaggfn the second, so neither
displaces the other and a consumer tells an aggregate from a function without
reading the C symbol's suffix.

Plain unittest, no pytest dependency; synthetic sources via a temp dir.
"""
import tempfile
import unittest
from pathlib import Path

from parser.sqlfn import _mdb_to_agg, attach_sqlaggfn_map, attach_sqlfn_map

MEOS_C = """
/**
 * @ingroup meos_temporal_agg
 * @brief Transition function for temporal count aggregation
 * @csqlfn #Temporal_tcount_transfn()
 */
SkipList *
temporal_tcount_transfn(SkipList *state, const Temporal *temp)
{
}

/**
 * @ingroup meos_temporal_accessor
 * @brief Return the number of instants of a temporal value
 * @csqlfn #Temporal_num_instants()
 */
int
temporal_num_instants(const Temporal *temp)
{
}
"""

MDB_C = """
/**
 * @ingroup mobilitydb_temporal_agg
 * @brief Transition function for temporal count aggregation
 * @sqlfn tcount_transfn()
 * @sqlaggfn tCount()
 */
Datum
Temporal_tcount_transfn(PG_FUNCTION_ARGS)
{
}

/**
 * @ingroup mobilitydb_temporal_accessor
 * @brief Return the number of instants of a temporal value
 * @sqlfn numInstants()
 */
Datum
Temporal_num_instants(PG_FUNCTION_ARGS)
{
}
"""


class SqlAggfnTests(unittest.TestCase):
    def _trees(self, d):
        meos = Path(d) / "meos"
        mdb = Path(d) / "mdb"
        meos.mkdir()
        mdb.mkdir()
        (meos / "x.c").write_text(MEOS_C)
        (mdb / "y.c").write_text(MDB_C)
        return str(meos), str(mdb)

    def test_wrapper_map_reads_only_tagged_wrappers(self):
        with tempfile.TemporaryDirectory() as d:
            _, mdb = self._trees(d)
            d2a = _mdb_to_agg(mdb)
        self.assertEqual(d2a.get("Temporal_tcount_transfn"), ["tCount"])
        # A wrapper carrying only @sqlfn names no aggregate and stays out.
        self.assertNotIn("Temporal_num_instants", d2a)

    def test_aggregate_name_rides_the_csqlfn_chain(self):
        idl = {"functions": [{"name": "temporal_tcount_transfn"},
                             {"name": "temporal_num_instants"}]}
        with tempfile.TemporaryDirectory() as d:
            meos, mdb = self._trees(d)
            idl, n = attach_sqlaggfn_map(idl, meos, mdb)
        by = {f["name"]: f for f in idl["functions"]}
        self.assertEqual(n, 1)
        self.assertEqual(by["temporal_tcount_transfn"]["sqlAggregate"], ["tCount"])
        self.assertNotIn("sqlAggregate", by["temporal_num_instants"])

    def test_sqlfn_states_the_function_the_wrapper_backs(self):
        """The two tags coexist on one block: the aggregate name does not take
        the `sqlfn` slot, which keeps naming the CREATE FUNCTION."""
        idl = {"functions": [{"name": "temporal_tcount_transfn"}]}
        with tempfile.TemporaryDirectory() as d:
            meos, mdb = self._trees(d)
            idl, _, _ = attach_sqlfn_map(idl, meos, mdb)
            idl, _ = attach_sqlaggfn_map(idl, meos, mdb)
        f = idl["functions"][0]
        self.assertEqual(f["sqlfn"], "tcount_transfn")
        self.assertEqual(f["sqlAggregate"], ["tCount"])


if __name__ == "__main__":
    unittest.main()
