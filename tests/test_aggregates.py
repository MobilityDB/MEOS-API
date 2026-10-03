"""The SQL aggregates, stated with the MEOS function behind each of their roles.

Every `CREATE AGGREGATE` is stated in the catalog's top-level `aggregates` with its
arguments, its result type and, for each role it defines (transition, combine, final,
serialize, deserialize), the SQL function PostgreSQL calls and the public MEOS function
carrying that function's SQL signature. Each role resolves as PostgreSQL resolves it, by
name and the argument types it is called with.

The catalog is synthetic and the SQL sits in a temp dir, as #CompositionTests of
tests/test_compositions.py arranges them. Plain unittest, no pytest dependency.
"""
import tempfile
import unittest
from pathlib import Path

from parser.aggregates import attach_aggregates


def _fn(name, sqlfn, sigs, api="public"):
    return {"name": name, "sqlfn": sqlfn, "api": api,
            "sqlSignatures": [{"sqlName": sqlfn, **s} for s in sigs]}


FUNCTIONS = [
    _fn("temporal_tcount_transfn", "tCountTransition",
        [{"args": ["internal", "tgeompoint"], "ret": "internal"}]),
    _fn("temporal_tcount_combinefn", "tcount_combinefn",
        [{"args": ["internal", "internal"], "ret": "internal"}]),
    _fn("temporal_tagg_finalfn", "tCount",
        [{"args": ["internal"], "ret": "tint", "sqlName": "tint_tagg_finalfn"},
         {"args": ["internal"], "ret": "tfloat", "sqlName": "tfloat_tagg_finalfn"}]),
    _fn("tnumber_wavg_transfn", "wavg_transfn",
        [{"args": ["internal", "tfloat", "interval"], "ret": "internal"}]),
    _fn("tspatial_extent_transfn", "stbox_extent_transfn",
        [{"args": ["stbox", "tgeompoint"], "ret": "stbox"}]),
    _fn("set_union_finalfn", "floatset_union_finalfn",
        [{"args": ["internal"], "ret": "floatset"}]),
    # the transition of an aggregate whose MEOS function is internal
    _fn("temporal_app_tinst_transfn", "appendInstantTransition",
        [{"args": ["tfloat", "tfloat"], "ret": "tfloat"}], api="internal"),
]

DECLARED = """
CREATE FUNCTION tCountTransition(internal, tgeompoint)
  RETURNS internal AS 'MODULE_PATHNAME', 'Temporal_tcount_transfn' LANGUAGE C;
CREATE FUNCTION tcount_combinefn(internal, internal)
  RETURNS internal AS 'MODULE_PATHNAME', 'Temporal_tcount_combinefn' LANGUAGE C;
CREATE FUNCTION tint_tagg_finalfn(internal)
  RETURNS tint AS 'MODULE_PATHNAME', 'Temporal_tagg_finalfn' LANGUAGE C;
CREATE FUNCTION tfloat_tagg_finalfn(internal)
  RETURNS tfloat AS 'MODULE_PATHNAME', 'Temporal_tagg_finalfn' LANGUAGE C;
CREATE FUNCTION taggstate_serialize(internal)
  RETURNS bytea AS 'MODULE_PATHNAME', 'Taggstate_serialize' LANGUAGE C;
CREATE FUNCTION taggstate_deserialize(bytea, internal)
  RETURNS internal AS 'MODULE_PATHNAME', 'Taggstate_deserialize' LANGUAGE C;
CREATE FUNCTION wavg_transfn(internal, tfloat, interval)
  RETURNS internal AS 'MODULE_PATHNAME', 'Tnumber_wavg_transfn' LANGUAGE C;
CREATE FUNCTION stbox_extent_transfn(stbox, tgeompoint)
  RETURNS stbox AS 'MODULE_PATHNAME', 'Tspatial_extent_transfn' LANGUAGE C;
CREATE FUNCTION floatset_union_finalfn(internal)
  RETURNS floatset AS 'MODULE_PATHNAME', 'Set_union_finalfn' LANGUAGE C;
CREATE FUNCTION appendInstantTransition(tfloat, tfloat)
  RETURNS tfloat AS 'MODULE_PATHNAME', 'Temporal_app_tinst_transfn' LANGUAGE C;
CREATE FUNCTION temporal_append_finalfn(tfloat)
  RETURNS tfloat AS 'MODULE_PATHNAME', 'Temporal_append_finalfn' LANGUAGE C;
"""

AGGREGATES = DECLARED + """
CREATE AGGREGATE tCount(tgeompoint) (
  SFUNC = tCountTransition,
  STYPE = internal,
  COMBINEFUNC = tcount_combinefn,
  FINALFUNC = tint_tagg_finalfn,
  SERIALFUNC = taggstate_serialize,
  DESERIALFUNC = taggstate_deserialize,
  PARALLEL = SAFE
);
-- a comment between statements
CREATE AGGREGATE wAvg(tfloat, interval) (
  SFUNC = wavg_transfn,
  STYPE = internal,
  FINALFUNC = tfloat_tagg_finalfn
);
CREATE AGGREGATE extent(tgeompoint) (
  SFUNC = stbox_extent_transfn,
  STYPE = stbox,
  PARALLEL = safe
);
CREATE AGGREGATE setUnion(float8) (
  SFUNC = array_agg_transfn,
  STYPE = internal,
  COMBINEFUNC = array_agg_combine,
  FINALFUNC = floatset_union_finalfn
);
CREATE AGGREGATE appendInstantAgg(tfloat) (
  SFUNC = appendInstantTransition(tfloat, tfloat),
  STYPE = tfloat,
  FINALFUNC = temporal_append_finalfn,
  PARALLEL = safe
);
"""


class AggregateTests(unittest.TestCase):
    """Each aggregate the SQL states, with its roles and result type."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sql = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _attach(self, text, functions=FUNCTIONS):
        (self.sql / "aggregates.in.sql").write_text(text)
        return attach_aggregates({"functions": [dict(f) for f in functions]}, self.sql)

    def _entries(self):
        idl, n = self._attach(AGGREGATES)
        self.assertEqual(n, len(idl["aggregates"]))
        return {(e["sqlName"], tuple(e["args"])): e for e in idl["aggregates"]}

    def test_every_role_names_its_sql_and_meos_function(self):
        e = self._entries()[("tCount", ("tgeompoint",))]
        self.assertEqual(e["ret"], "tint")
        self.assertEqual(e["stype"], "internal")
        self.assertEqual(e["transition"],
                         {"sqlName": "tCountTransition", "meos": "temporal_tcount_transfn"})
        self.assertEqual(e["combine"],
                         {"sqlName": "tcount_combinefn", "meos": "temporal_tcount_combinefn"})
        self.assertEqual(e["final"],
                         {"sqlName": "tint_tagg_finalfn", "meos": "temporal_tagg_finalfn"})

    def test_a_role_over_postgresql_memory_names_no_meos_function(self):
        e = self._entries()[("tCount", ("tgeompoint",))]
        self.assertEqual(e["serialize"], {"sqlName": "taggstate_serialize", "meos": None})
        self.assertEqual(e["deserialize"], {"sqlName": "taggstate_deserialize", "meos": None})

    def test_the_transition_takes_the_state_and_every_argument(self):
        e = self._entries()[("wAvg", ("tfloat", "interval"))]
        self.assertEqual(e["transition"]["meos"], "tnumber_wavg_transfn")
        self.assertEqual(e["ret"], "tfloat")
        self.assertNotIn("combine", e)

    def test_without_a_final_function_the_answer_is_the_state(self):
        e = self._entries()[("extent", ("tgeompoint",))]
        self.assertEqual(e["ret"], "stbox")
        self.assertEqual(e["transition"]["meos"], "tspatial_extent_transfn")
        self.assertNotIn("final", e)

    def test_a_postgresql_function_names_no_meos_function(self):
        # The argument stays as the SQL spells it, as #sql_signature keeps it in every
        # sqlSignatures entry; the role resolves through #_type, as #_Resolver does.
        e = self._entries()[("setUnion", ("float8",))]
        self.assertEqual(e["transition"], {"sqlName": "array_agg_transfn", "meos": None})
        self.assertEqual(e["combine"], {"sqlName": "array_agg_combine", "meos": None})
        self.assertEqual(e["final"]["meos"], "set_union_finalfn")
        self.assertEqual(e["ret"], "floatset")

    def test_a_function_written_with_its_argument_types(self):
        e = self._entries()[("appendInstantAgg", ("tfloat",))]
        self.assertEqual(e["transition"]["sqlName"], "appendInstantTransition")
        self.assertEqual(e["ret"], "tfloat")

    def test_an_internal_meos_function_is_not_named(self):
        e = self._entries()[("appendInstantAgg", ("tfloat",))]
        self.assertIsNone(e["transition"]["meos"])

    def test_a_role_two_public_functions_carry_stops_the_catalog(self):
        twin = _fn("tgeompoint_tcount_transfn", "tCountTransition",
                   [{"args": ["internal", "tgeompoint"], "ret": "internal"}])
        with self.assertRaises(ValueError) as cm:
            self._attach(AGGREGATES, FUNCTIONS + [twin])
        self.assertIn("tCount(tgeompoint)", str(cm.exception))

    def test_an_aggregate_without_a_state_type_stops_the_catalog(self):
        with self.assertRaises(ValueError) as cm:
            self._attach("CREATE AGGREGATE bad(tint) (SFUNC = f);")
        self.assertIn("bad(tint): no STYPE", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
