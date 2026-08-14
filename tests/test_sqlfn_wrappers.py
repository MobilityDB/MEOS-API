"""A MEOS function's SQL surface is the union over every wrapper it claims.

One MEOS function commonly backs a SET of PostgreSQL wrappers, each registering
its own CREATE FUNCTION overloads: the ever/always pair over one `ea_*` kernel,
the shift/scale/shiftScale trio over one `*_shift_scale`, send + asBinary over
one `*_as_wkb`, and the argument-COMMUTED form of an asymmetric operation
(`NAD_stbox_tgeo` beside `NAD_tgeo_stbox`, both over `nad_tgeo_stbox`). Reading
only the first wrapper dropped every sibling's overloads, so half of each
ever/always pair — and any commuted overload — was invisible to bindings.

Plain unittest, no pytest dependency; synthetic sources via a temp dir.
"""
import tempfile
import unittest
from pathlib import Path

from parser.sqlfn import _create_fn_stmts, attach_sqlfn_map

MEOS_C = """
/**
 * @ingroup meos_geo_distance
 * @brief Return the nearest approach distance between a temporal geo and a box
 * @csqlfn #NAD_tgeo_stbox() #NAD_stbox_tgeo()
 */
double
nad_tgeo_stbox(const Temporal *temp, const STBox *box)
{
}

/**
 * @ingroup meos_geo_rel
 * @brief Return true if a temporal geo and a geo are ever or always within a
 * distance of each other
 * @csqlfn #Edwithin_tgeo_geo() #Adwithin_tgeo_geo()
 */
int
ea_dwithin_tgeo_geo(const Temporal *temp, const GSERIALIZED *gs, double dist,
  bool ever)
{
}
"""

MDB_C = """
/**
 * @brief Return the nearest approach distance between a temporal geo and a box
 * @sqlfn nearestApproachDistance()
 * @sqlop @p |=|
 */
Datum
NAD_tgeo_stbox(PG_FUNCTION_ARGS)
{
}

/**
 * @brief Return the nearest approach distance between a box and a temporal geo
 * @sqlfn nearestApproachDistance()
 * @sqlop @p |=|
 */
Datum
NAD_stbox_tgeo(PG_FUNCTION_ARGS)
{
}

/**
 * @brief Return true if a temporal geo and a geo are ever within a distance
 * @sqlfn eDwithin()
 */
Datum
Edwithin_tgeo_geo(PG_FUNCTION_ARGS)
{
}

/**
 * @brief Return true if a temporal geo and a geo are always within a distance
 * @sqlfn aDwithin()
 */
Datum
Adwithin_tgeo_geo(PG_FUNCTION_ARGS)
{
}
"""

MDB_SQL = """
CREATE FUNCTION nearestApproachDistance(tgeompoint, stbox)
  RETURNS float
  AS 'MODULE_PATHNAME', 'NAD_tgeo_stbox'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION nearestApproachDistance(stbox, tgeompoint)
  RETURNS float
  AS 'MODULE_PATHNAME', 'NAD_stbox_tgeo'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION eDwithin(tgeompoint, geometry, float)
  RETURNS boolean
  AS 'MODULE_PATHNAME', 'Edwithin_tgeo_geo'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION aDwithin(tgeompoint, geometry, float)
  RETURNS boolean
  SUPPORT tspatial_supportfn
  AS 'MODULE_PATHNAME', 'Adwithin_tgeo_geo'
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
        # The type-scope deriver reads MEOS's own type catalog; nothing in this
        # fixture shares a wrapper, so an empty one states every fact needed.
        (meos / "temporal").mkdir()
        (meos / "temporal" / "meos_catalog.c").write_text("")
        (meos / "x.c").write_text(MEOS_C)
        (mdb / "y.c").write_text(MDB_C)
        (sql / "z.sql").write_text(MDB_SQL)
        idl, _, _ = attach_sqlfn_map(idl, str(meos), str(mdb), str(sql))
    return {f["name"]: f for f in idl["functions"]}


class EveryClaimedWrapperTests(unittest.TestCase):

    def test_a_commuted_wrapper_contributes_its_overload(self):
        """The argument order a commuted wrapper registers is part of the surface."""
        f = _attach(["nad_tgeo_stbox"])["nad_tgeo_stbox"]
        self.assertEqual([s["args"] for s in f["sqlSignatures"]],
                         [["tgeompoint", "stbox"], ["stbox", "tgeompoint"]])

    def test_the_primary_wrapper_still_names_the_function(self):
        """The union widens the signatures; it does not move `sqlfn` or `mdbC`."""
        f = _attach(["nad_tgeo_stbox"])["nad_tgeo_stbox"]
        self.assertEqual(f["mdbC"], "NAD_tgeo_stbox")
        self.assertEqual(f["sqlfn"], "nearestApproachDistance")
        self.assertEqual(f["sqlop"], "|=|")

    def test_both_halves_of_an_ever_always_pair_are_kept(self):
        """`ea_*` backs two SQL names, and each is a registration a binding emits."""
        f = _attach(["ea_dwithin_tgeo_geo"])["ea_dwithin_tgeo_geo"]
        self.assertEqual([s["sqlName"] for s in f["sqlSignatures"]],
                         ["eDwithin", "aDwithin"])

    def test_a_signature_from_a_second_wrapper_widens_the_arity(self):
        f = _attach(["ea_dwithin_tgeo_geo"])["ea_dwithin_tgeo_geo"]
        self.assertEqual((f["sqlArity"], f["sqlArityMax"]), (3, 3))


class ReturnTypeTests(unittest.TestCase):

    def test_an_attribute_between_returns_and_as_is_not_part_of_the_type(self):
        """PostgreSQL accepts the attributes in any order, so SUPPORT may precede
        the body — `aTouches(tcbuffer, cbuffer)` is the one place MobilityDB
        writes it that way."""
        rets = {name: ret for name, _, ret, _ in _create_fn_stmts(MDB_SQL)}
        self.assertEqual(rets["aDwithin"], "boolean")
        self.assertEqual(rets["eDwithin"], "boolean")

    def test_the_union_reports_one_return_type_when_the_wrappers_agree(self):
        f = _attach(["ea_dwithin_tgeo_geo"])["ea_dwithin_tgeo_geo"]
        self.assertEqual(f["sqlReturnType"], "boolean")
        self.assertNotIn("sqlReturnTypeAll", f)


if __name__ == "__main__":
    unittest.main()
