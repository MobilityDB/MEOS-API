"""The SQL functions composed over the functions of another type, stated step by step.

A SQL function with no C symbol whose body casts its arguments and calls one other SQL
function is a composition, stated in the catalog's top-level `compositions` with its
operands (each an argument through its casts, or a value), its call and its restore, all
named by MEOS function. Each step resolves through the SQL signatures the catalog
carries, and one that resolves to no public MEOS function stops the catalog.

The catalog is synthetic, as the one #FamilyClassificationTests of tests/test_family.py
reads is generated, and the SQL sits in a temp dir, as #RowSourceTests of
tests/test_sqlfn_rows.py writes its declarations. Plain unittest, no pytest dependency.
"""
import tempfile
import unittest
from pathlib import Path

from parser.compositions import attach_compositions, attach_wrapper_compositions


def _fn(name, sqlfn, params, sigs, api="public", shape=None):
    f = {"name": name, "sqlfn": sqlfn, "api": api,
         "params": [{"name": n, "cType": c} for n, c in params],
         "sqlSignatures": [{"sqlName": sqlfn, **s} for s in sigs]}
    if shape:
        f["shape"] = shape
    return f


T, G, B = "const Temporal *", "const GSERIALIZED *", "const STBox *"

FUNCTIONS = [
    _fn("tpose_to_tpoint", "tgeompoint", [("temp", T)],
        [{"args": ["tpose"], "ret": "tgeompoint"}]),
    _fn("tposechain_to_tpose", "tpose", [("temp", T)],
        [{"args": ["tposechain"], "ret": "tpose"}]),
    _fn("tpcpoint_to_tgeompoint", "tgeompoint", [("temp", T)],
        [{"args": ["tpcpoint"], "ret": "tgeompoint"}]),
    _fn("tpcpatch_to_tgeometry", "tgeometry", [("temp", T)],
        [{"args": ["tpcpatch"], "ret": "tgeometry"}]),
    _fn("tcellindex_cell_to_boundary", "cellToBoundary", [("temp", T)],
        [{"args": ["th3index"], "ret": "tgeography"}]),
    _fn("tgeography_to_tgeometry", "tgeometry", [("temp", T)],
        [{"args": ["tgeography"], "ret": "tgeometry"}]),
    _fn("tspatial_to_stbox", "stbox", [("temp", T)],
        [{"args": ["tgeompoint"], "ret": "stbox"}]),
    _fn("edisjoint_tgeo_geo", "eDisjoint", [("temp", T), ("gs", G)],
        [{"args": ["tgeompoint", "geometry"], "ret": "boolean"}]),
    _fn("econtains_tgeo_geo", "eContains", [("temp", T), ("gs", G)],
        [{"args": ["tgeometry", "geometry"], "ret": "boolean"}]),
    # one function carrying both argument orders, its wrapper swapping them
    _fn("nad_tgeo_geo", "nearestApproachDistance", [("temp", T), ("gs", G)],
        [{"args": ["tgeompoint", "geometry(Point)"], "ret": "float"},
         {"args": ["geometry(Point)", "tgeompoint"], "ret": "float"}]),
    _fn("stbox_expand_space", "expandSpace", [("box", B), ("d", "double")],
        [{"args": ["stbox", "float"], "ret": "stbox"}]),
    _fn("tgeo_space_split", "spaceSplit",
        [("temp", T), ("xsize", "double"), ("ysize", "double"), ("zsize", "double"),
         ("sorigin", G), ("bitmatrix", "bool"), ("border_inc", "bool"),
         ("space_bins", "GSERIALIZED ***"), ("count", "int *")],
        [{"args": ["tgeompoint", "float", "float", "float", "geometry", "boolean",
                   "boolean"], "ret": "point_tgeo", "retSet": True,
          "argDefaults": [None, None, None, None, "'Point(0 0 0)'", "TRUE", "TRUE"],
          "columns": [{"name": "point", "type": "geometry"},
                      {"name": "tpoint", "type": "tgeompoint"}]}],
        shape={"outParams": ["space_bins", "count"]}),
    _fn("temporal_time", "getTime", [("temp", T)],
        [{"args": ["tgeompoint"], "ret": "tstzspanset"}]),
    # the per-subtype twin carries the same signature, and is internal
    _fn("tinstant_time", "getTime", [("inst", T)],
        [{"args": ["tgeompoint"], "ret": "tstzspanset"}], api="internal"),
    _fn("temporal_at_tstzspanset", "atTime", [("temp", T), ("ss", "const SpanSet *")],
        [{"args": ["tpose", "tstzspanset"], "ret": "tpose"}]),
    _fn("temporal_at_timestamptz", "atTime", [("temp", T), ("t", "TimestampTz")],
        [{"args": ["tpcpoint", "timestamptz"], "ret": "tpcpoint"}]),
    _fn("nai_tgeo_geo", "nearestApproachInstant", [("temp", T), ("gs", G)],
        [{"args": ["tgeompoint", "geometry"], "ret": "tgeompoint"}]),
    _fn("temporal_start_timestamptz", "getTimestamp", [("temp", T)],
        [{"args": ["tgeompoint"], "ret": "timestamptz"}]),
    _fn("tgeo_space_time_boxes", "timeBoxes",
        [("temp", T), ("xsize", "double"), ("ysize", "double"), ("zsize", "double"),
         ("duration", "const Interval *"), ("sorigin", G), ("torigin", "TimestampTz"),
         ("bitmatrix", "bool"), ("border_inc", "bool"), ("count", "int *")],
        [{"args": ["tgeompoint", "interval", "timestamptz", "boolean", "boolean"],
          "ret": "stbox[]",
          "boundArgs": {"xsize": "0.0", "ysize": "0.0", "zsize": "0.0",
                        "sorigin": "NULL"}}],
        shape={"outParams": ["count"]}),
    _fn("temporal_stops", "stops", [("temp", T), ("maxdist", "double"),
                                    ("minduration", "const Interval *")],
        [{"args": ["tgeompoint", "float", "interval"], "ret": "tgeompoint"}]),
    _fn("acontains_tgeo_tgeo", "aContains", [("temp1", T), ("temp2", T)],
        [{"args": ["tgeometry", "tgeometry"], "ret": "boolean"}], api="internal"),
]


def _idl():
    return {"functions": [dict(f) for f in FUNCTIONS],
            "objectModel": {"classes": {"Geometry": {"cType": "GSERIALIZED *"},
                                        "STBox": {"cType": "STBox *"},
                                        "TsTzSpanSet": {"cType": "SpanSet *"}}},
            "temporalTypes": {t: {} for t in (
                "tgeompoint", "tgeometry", "tgeography", "tpose", "tposechain",
                "tpcpoint", "tpcpatch", "th3index")}}


CASTS = """
CREATE CAST (tpose AS tgeompoint) WITH FUNCTION tgeompoint(tpose);
CREATE CAST (tposechain AS tpose) WITH FUNCTION tpose(tposechain);
CREATE CAST (tpcpoint AS tgeompoint) WITH FUNCTION tgeompoint(tpcpoint);
CREATE CAST (tpcpatch AS tgeometry) WITH FUNCTION tgeometry(tpcpatch);
CREATE CAST (tgeography AS tgeometry) WITH FUNCTION tgeometry(tgeography);
CREATE CAST (tgeompoint AS stbox) WITH FUNCTION stbox(tgeompoint);
CREATE TYPE point_tpose AS (point geometry, tpose tpose);
"""

COMPOSED = CASTS + """
CREATE FUNCTION eDisjoint(tpose, geometry)
  RETURNS boolean
  AS 'SELECT @extschema@.eDisjoint($1::@extschema@.tgeompoint, $2)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION eDisjoint(tposechain, geometry)
  RETURNS boolean
  AS 'SELECT @extschema@.eDisjoint($1::@extschema@.tpose, $2)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION eContains(th3index, geometry)
  RETURNS boolean
  AS 'SELECT @extschema@.eContains(@extschema@.cellToBoundary($1)::@extschema@.tgeometry, $2)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION nearestApproachDistance(geometry, tpcpoint)
  RETURNS float
  AS $$ SELECT @extschema@.nearestApproachDistance($1, $2::@extschema@.tgeompoint) $$
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION expandSpace(tgeompoint, float)
  RETURNS stbox
  AS 'SELECT @extschema@.expandSpace($1::@extschema@.stbox, $2)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION centroid(tpcpoint)
  RETURNS tgeompoint
  AS 'SELECT $1::@extschema@.tgeompoint'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION spaceSplit(tpose, xsize float, ysize float, zsize float,
    sorigin geometry DEFAULT 'Point(0 0 0)', bitmatrix boolean DEFAULT TRUE,
    borderInc boolean DEFAULT TRUE)
  RETURNS SETOF point_tpose
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT r.point, @extschema@.atTime($1, @extschema@.getTime(r.tpoint))
    FROM @extschema@.spaceSplit(
      $1::@extschema@.tgeompoint, $2, $3, $4, $5, $6, $7) AS r
  $$;
CREATE FUNCTION spaceSplit(tpose, xsize float, sorigin geometry DEFAULT 'Point(0 0 0)',
    bitmatrix boolean DEFAULT TRUE, borderInc boolean DEFAULT TRUE)
  RETURNS SETOF point_tpose
  AS 'SELECT @extschema@.spaceSplit($1, $2, 0, 0, $3, $4, $5)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION nearestApproachInstant(tpcpoint, geometry)
  RETURNS tpcpoint
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE AS $$
    SELECT @extschema@.atTime($1,
      @extschema@.getTimestamp(
        @extschema@.nearestApproachInstant($1::@extschema@.tgeompoint, $2)))
  $$;
CREATE FUNCTION timeBoxes(tpose, duration interval,
    torigin timestamptz DEFAULT '2000-01-03', bitmatrix boolean DEFAULT TRUE,
    borderInc boolean DEFAULT TRUE)
  RETURNS stbox[]
  AS 'SELECT @extschema@.timeBoxes($1::@extschema@.tgeompoint, $2, $3, $4, $5)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION stops(tgeompoint, interval)
  RETURNS tgeompoint
  AS 'SELECT @extschema@.stops($1, 0.0, $2)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION eDisjoint(geometry, tgeompoint)
  RETURNS boolean
  AS 'SELECT @extschema@.eDisjoint($2, $1)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION asEWKT(geometry, integer)
  RETURNS text
  AS 'SELECT @extschema@.ST_AsEWKT($1, $2)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION douglasPeuckerSimplify(tpose, float)
  RETURNS tpose
  AS 'SELECT COALESCE(@extschema@.deleteTime($1, @extschema@.set(@extschema@.timestamps($1))
    - @extschema@.set(@extschema@.timestamps(
      @extschema@.douglasPeuckerSimplify($1::@extschema@.tgeompoint, $2)))), $1)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
"""


class CompositionTests(unittest.TestCase):
    """Each composition the SQL bodies state, and the bodies that are none."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.sql = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _attach(self, text):
        (self.sql / "composed.in.sql").write_text(text)
        return attach_compositions(_idl(), self.sql)

    def _entries(self):
        idl, n = self._attach(COMPOSED)
        self.assertEqual(n, len(idl["compositions"]))
        return {(e["sqlName"], tuple(e["args"])): e for e in idl["compositions"]}

    def test_a_cast_then_a_call(self):
        e = self._entries()[("eDisjoint", ("tpose", "geometry"))]
        self.assertEqual(e["operands"], [
            {"arg": 0, "casts": ["tpose_to_tpoint"], "param": "temp"},
            {"arg": 1, "param": "gs"}])
        self.assertEqual(e["call"], "edisjoint_tgeo_geo")
        self.assertNotIn("restore", e)

    def test_a_cast_to_a_composed_type_takes_its_casts(self):
        e = self._entries()[("eDisjoint", ("tposechain", "geometry"))]
        self.assertEqual(e["operands"][0]["casts"],
                         ["tposechain_to_tpose", "tpose_to_tpoint"])
        self.assertEqual(e["call"], "edisjoint_tgeo_geo")

    def test_a_cell_reaches_the_call_through_its_boundary(self):
        e = self._entries()[("eContains", ("th3index", "geometry"))]
        self.assertEqual(e["operands"][0]["casts"],
                         ["tcellindex_cell_to_boundary", "tgeography_to_tgeometry"])

    def test_operands_follow_the_parameters_of_the_c_function(self):
        # the SQL call passes the geometry first; nad_tgeo_geo takes it second
        e = self._entries()[("nearestApproachDistance", ("geometry", "tpcpoint"))]
        self.assertEqual(e["operands"], [
            {"arg": 1, "casts": ["tpcpoint_to_tgeompoint"], "param": "temp"},
            {"arg": 0, "param": "gs"}])

    def test_a_cast_to_the_box(self):
        e = self._entries()[("expandSpace", ("tgeompoint", "float"))]
        self.assertEqual(e["operands"][0]["casts"], ["tspatial_to_stbox"])
        self.assertEqual(e["call"], "stbox_expand_space")

    def test_a_cast_alone(self):
        e = self._entries()[("centroid", ("tpcpoint",))]
        self.assertEqual(e["operands"], [{"arg": 0, "casts": ["tpcpoint_to_tgeompoint"]}])
        self.assertNotIn("call", e)

    def test_a_split_restores_each_fragment_to_the_input_type(self):
        e = self._entries()[("spaceSplit", ("tpose", "float", "float", "float",
                                            "geometry", "boolean", "boolean"))]
        self.assertEqual(e["call"], "tgeo_space_split")
        self.assertEqual(e["restore"], {"column": "tpose", "from": "tpoint", "of": 0,
                                        "time": "temporal_time",
                                        "at": "temporal_at_tstzspanset"})
        self.assertEqual([c["name"] for c in e["columns"]], ["point", "tpose"])
        self.assertTrue(e["retSet"])

    def test_a_forward_to_a_composition_takes_its_steps(self):
        e = self._entries()[("spaceSplit", ("tpose", "float", "geometry", "boolean",
                                            "boolean"))]
        self.assertEqual(e["operands"], [
            {"arg": 0, "casts": ["tpose_to_tpoint"], "param": "temp"},
            {"arg": 1, "param": "xsize"},
            {"value": "0", "param": "ysize"}, {"value": "0", "param": "zsize"},
            {"arg": 2, "param": "sorigin"}, {"arg": 3, "param": "bitmatrix"},
            {"arg": 4, "param": "border_inc"}])
        self.assertEqual(e["restore"]["of"], 0)

    def test_an_instant_restored_to_the_input_type(self):
        e = self._entries()[("nearestApproachInstant", ("tpcpoint", "geometry"))]
        self.assertEqual(e["call"], "nai_tgeo_geo")
        self.assertEqual(e["restore"], {"of": 0, "time": "temporal_start_timestamptz",
                                        "at": "temporal_at_timestamptz"})

    def test_the_literals_the_callee_binds_are_value_operands(self):
        e = self._entries()[("timeBoxes", ("tpose", "interval", "timestamptz",
                                           "boolean", "boolean"))]
        self.assertEqual([(o.get("arg"), o.get("value"), o["param"])
                          for o in e["operands"]], [
            (0, None, "temp"), (None, "0.0", "xsize"), (None, "0.0", "ysize"),
            (None, "0.0", "zsize"), (1, None, "duration"), (None, "NULL", "sorigin"),
            (2, None, "torigin"), (3, None, "bitmatrix"), (4, None, "border_inc")])

    def test_the_public_function_is_the_step(self):
        # getTime(tgeompoint) is carried by temporal_time and its internal twin
        e = self._entries()[("spaceSplit", ("tpose", "float", "float", "float",
                                            "geometry", "boolean", "boolean"))]
        self.assertEqual(e["restore"]["time"], "temporal_time")

    def test_bodies_that_convert_nothing_are_no_composition(self):
        entries = self._entries()
        for key in (("stops", ("tgeompoint", "interval")),        # a literal forward
                    ("eDisjoint", ("geometry", "tgeompoint")),    # a swap
                    ("asEWKT", ("geometry", "integer")),          # a PostGIS forward
                    ("douglasPeuckerSimplify", ("tpose", "float"))):  # a program
            self.assertNotIn(key, entries)
        self.assertEqual(len(entries), 10)

    def test_a_step_no_public_function_takes_stops_the_catalog(self):
        with self.assertRaises(ValueError) as cm:
            self._attach(CASTS + """
CREATE FUNCTION aContains(tpcpatch, tpcpatch)
  RETURNS boolean
  AS 'SELECT @extschema@.aContains($1::@extschema@.tgeometry, $2::@extschema@.tgeometry)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
""")
        self.assertIn("aContains(tpcpatch, tpcpatch)", str(cm.exception))
        self.assertIn("0 public MEOS functions", str(cm.exception))

    def test_a_cast_no_create_cast_declares_stops_the_catalog(self):
        with self.assertRaises(ValueError) as cm:
            self._attach(CASTS + """
CREATE FUNCTION eDisjoint(tnpoint, geometry)
  RETURNS boolean
  AS 'SELECT @extschema@.eDisjoint($1::@extschema@.tgeompoint, $2)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
""")
        self.assertIn("no CREATE CAST from tnpoint to tgeompoint", str(cm.exception))


CAST_WRAPPERS = '''
Datum
Eintersects_tpose_geo(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  GSERIALIZED *gs = PG_GETARG_GSERIALIZED_P(1);
  Temporal *tpoint = tpose_to_tpoint(temp);
  int result = eintersects_tgeo_geo(tpoint, gs);
  pfree(tpoint);
  PG_RETURN_BOOL(result ? true : false);
}

Datum
Eintersects_tgeo_geo(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  GSERIALIZED *gs = PG_GETARG_GSERIALIZED_P(1);
  int result = eintersects_tgeo_geo(temp, gs);
  PG_RETURN_BOOL(result ? true : false);
}
'''

CAST_SQL = '''
CREATE FUNCTION eIntersects(tpose, geometry)
  RETURNS boolean
  AS 'MODULE_PATHNAME', 'Eintersects_tpose_geo'
  SUPPORT tspatial_supportfn
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION eIntersects(tgeompoint, geometry)
  RETURNS boolean
  AS 'MODULE_PATHNAME', 'Eintersects_tgeo_geo'
  SUPPORT tspatial_supportfn
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
'''

CAST_MEOS = '''
/**
 * @brief Return 1 if a temporal geo ever intersects a geometry
 * @csqlfn #Eintersects_tgeo_geo(), #Eintersects_tpose_geo()
 */
int
eintersects_tgeo_geo(const Temporal *temp, const GSERIALIZED *gs)
{
  return 0;
}
'''


class WrapperCompositionTests(unittest.TestCase):
    """#attach_wrapper_compositions: a C wrapper casting an argument through a public cast
    states a composition, and its sibling passing the argument as it is keeps its
    signature."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for sub, name, text in (("src", "rels.c", CAST_WRAPPERS), ("sql", "rels.in.sql", CAST_SQL),
                                ("meos", "rels_meos.c", CAST_MEOS)):
            (root / sub).mkdir()
            (root / sub / name).write_text(text)

    def tearDown(self):
        self.tmp.cleanup()

    def _attach(self):
        root = Path(self.tmp.name)
        idl = {"functions": [
            {"name": "eintersects_tgeo_geo", "api": "public", "mdbC": "Eintersects_tgeo_geo",
             "sqlfn": "eIntersects", "params": [{"name": "temp"}, {"name": "gs"}],
             "sqlSignatures": [{"args": ["tgeompoint", "geometry"], "ret": "boolean"},
                               {"args": ["tpose", "geometry"], "ret": "boolean"}]},
            {"name": "tpose_to_tpoint", "api": "public", "params": [{"name": "temp"}],
             "sqlSignatures": []}],
            "compositions": []}
        return attach_wrapper_compositions(idl, root / "src", root / "sql", root / "meos")

    def test_the_cast_wrapper_states_a_composition(self):
        idl, n = self._attach()
        self.assertEqual(n, 1)
        self.assertEqual(idl["compositions"], [{
            "sqlName": "eIntersects", "args": ["tpose", "geometry"], "required": 2,
            "argDefaults": [None, None], "ret": "boolean",
            "operands": [{"arg": 0, "casts": ["tpose_to_tpoint"], "param": "temp"},
                         {"arg": 1, "param": "gs"}],
            "call": "eintersects_tgeo_geo"}])

    def test_the_wrapper_casting_nothing_keeps_its_signature(self):
        idl, _ = self._attach()
        self.assertEqual([s["args"] for s in idl["functions"][0]["sqlSignatures"]],
                         [["tgeompoint", "geometry"]])


if __name__ == "__main__":
    unittest.main()
