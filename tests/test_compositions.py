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

    def test_a_call_of_a_body_that_is_no_composition_is_none(self):
        # #test_bodies_that_convert_nothing_are_no_composition, one call away: stable-1.3's
        # rotate is a program over cos and -sin, so rotateZ calling it composes nothing,
        # as a call of a PostGIS forward composes nothing.
        idl, _ = self._attach(CASTS + """
CREATE FUNCTION rotate(tgeompoint, float)
  RETURNS tgeompoint
  AS 'SELECT @extschema@.affine($1, cos($2), -sin($2))'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION rotateZ(tpose, float)
  RETURNS tgeompoint
  AS 'SELECT @extschema@.rotate($1::@extschema@.tgeompoint, $2)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION asText(tpose, integer)
  RETURNS text
  AS 'SELECT @extschema@.asEWKT($1::@extschema@.tgeompoint, $2)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION asEWKT(tgeompoint, integer)
  RETURNS text
  AS 'SELECT @extschema@.ST_AsEWKT($1, $2)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
""")
        self.assertEqual(idl["compositions"], [])

    def test_a_call_of_a_plain_forward_composes_through_it(self):
        # #test_a_forward_to_a_composition_takes_its_steps, for a forward to a C-backed
        # signature: stops(tgeompoint, interval) binds maxdist, and a body calling it
        # reaches temporal_stops with that literal.
        idl, _ = self._attach(COMPOSED + """
CREATE FUNCTION stops(tpose, interval)
  RETURNS tgeompoint
  AS 'SELECT @extschema@.stops($1::@extschema@.tgeompoint, $2)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
""")
        e = {(e["sqlName"], tuple(e["args"])): e
             for e in idl["compositions"]}[("stops", ("tpose", "interval"))]
        self.assertEqual(e["call"], "temporal_stops")
        self.assertEqual(e["operands"], [
            {"arg": 0, "casts": ["tpose_to_tpoint"], "param": "temp"},
            {"value": "0.0", "param": "maxdist"}, {"arg": 1, "param": "minduration"}])

    def test_a_call_of_a_forward_stating_no_c_call_is_none(self):
        # stable-1.3's translate(t, x, y, z) forwards to affine, whose wrapper packs
        # twelve floats into the one AFFINE argument tgeo_affine takes: no call of the C
        # function, so translate(t, x, y) calling it composes nothing either.
        idl = _idl()
        idl["functions"].append(_fn("tgeo_affine", "affine", [("temp", T), ("a", "const AFFINE *")],
                                    [{"args": ["tgeompoint", "float", "float", "float"],
                                      "ret": "tgeompoint"}]))
        (self.sql / "composed.in.sql").write_text(CASTS + """
CREATE FUNCTION translate(tgeompoint, float, float, float)
  RETURNS tgeompoint
  AS 'SELECT @extschema@.affine($1, $2, $3, $4)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
CREATE FUNCTION translate(tpose, float, float)
  RETURNS tgeompoint
  AS 'SELECT @extschema@.translate($1::@extschema@.tgeompoint, $2, $3, 0)'
  LANGUAGE SQL IMMUTABLE STRICT PARALLEL SAFE;
""")
        idl, _ = attach_compositions(idl, self.sql)
        self.assertEqual(idl["compositions"], [])

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

Datum
Edwithin_tpose_geo(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  GSERIALIZED *gs = PG_GETARG_GSERIALIZED_P(1);
  double dist = PG_GETARG_FLOAT8(2);
  Temporal *tpoint = tpose_to_tpoint(temp);
  int result = edwithin_tgeo_geo(tpoint, gs, dist, true);
  pfree(tpoint);
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
CREATE FUNCTION eDwithin(tpose, geometry, float)
  RETURNS boolean
  AS 'MODULE_PATHNAME', 'Edwithin_tpose_geo'
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

/**
 * @brief Return 1 if a temporal geo is ever within a distance of a geometry
 * @csqlfn #Edwithin_tpose_geo()
 */
int
edwithin_tgeo_geo(const Temporal *temp, const GSERIALIZED *gs, double dist,
  bool spheroid)
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
             "sqlSignatures": []},
            {"name": "edwithin_tgeo_geo", "api": "public", "sqlfn": "eDwithin",
             "params": [{"name": "temp"}, {"name": "gs"}, {"name": "dist"},
                        {"name": "spheroid"}],
             "sqlSignatures": [{"args": ["tpose", "geometry", "float"], "ret": "boolean"}]}],
            "compositions": []}
        return attach_wrapper_compositions(idl, root / "src", root / "sql", root / "meos")

    def test_the_cast_wrapper_states_a_composition(self):
        idl, n = self._attach()
        self.assertEqual(n, 2)
        self.assertEqual(idl["compositions"][:1], [{
            "sqlName": "eIntersects", "args": ["tpose", "geometry"], "required": 2,
            "argDefaults": [None, None], "ret": "boolean",
            "operands": [{"arg": 0, "casts": ["tpose_to_tpoint"], "param": "temp"},
                         {"arg": 1, "param": "gs"}],
            "call": "eintersects_tgeo_geo"}])

    def test_a_literal_argument_is_a_value_operand(self):
        idl, _ = self._attach()
        self.assertEqual(idl["compositions"][1]["operands"], [
            {"arg": 0, "casts": ["tpose_to_tpoint"], "param": "temp"},
            {"arg": 1, "param": "gs"}, {"arg": 2, "param": "dist"},
            {"value": "true", "param": "spheroid"}])
        self.assertEqual(idl["functions"][2]["sqlSignatures"], [])

    def test_the_wrapper_casting_nothing_keeps_its_signature(self):
        idl, _ = self._attach()
        self.assertEqual([s["args"] for s in idl["functions"][0]["sqlSignatures"]],
                         [["tgeompoint", "geometry"]])


HELPER_WRAPPERS = '''
static Datum
Stbox_constructor(FunctionCallInfo fcinfo, bool hasx, bool hasz,
  bool hast, bool geodetic)
{
  double xmin = 0, xmax = 0, ymin = 0, ymax = 0, zmin = 0, zmax = 0;
  int32_t srid = 0;
  Span *period = NULL;

  int i = 0;
  if (hasx)
  {
    xmin = PG_GETARG_FLOAT8(i++);
    ymin = PG_GETARG_FLOAT8(i++);
    xmax = PG_GETARG_FLOAT8(i++);
    ymax = PG_GETARG_FLOAT8(i++);
  }
  if (hast)
  {
    MeosType basetype = oid_meostype(get_fn_expr_argtype(fcinfo->flinfo, i));
    assert(basetype == T_TSTZSPAN || basetype == T_TIMESTAMPTZ);
    if (basetype == T_TSTZSPAN)
      period = PG_GETARG_SPAN_P(i++);
    else /* basetype == T_TIMESTAMPTZ */
    {
      TimestampTz t = PG_GETARG_TIMESTAMPTZ(i++);
      period = timestamptz_to_span(t);
    }
  }
  if (hasx)
    srid = PG_GETARG_INT32(i++);
  PG_RETURN_STBOX_P(stbox_make(hasx, hasz, geodetic, srid, xmin, xmax,
    ymin, ymax, zmin, zmax, period));
}

PGDLLEXPORT Datum Stbox_constructor_t(PG_FUNCTION_ARGS);
PG_FUNCTION_INFO_V1(Stbox_constructor_t);
Datum
Stbox_constructor_t(PG_FUNCTION_ARGS)
{
  return Stbox_constructor(fcinfo, false, false, true, false);
}

PGDLLEXPORT Datum Stbox_constructor_xt(PG_FUNCTION_ARGS);
PG_FUNCTION_INFO_V1(Stbox_constructor_xt);
Datum
Stbox_constructor_xt(PG_FUNCTION_ARGS)
{
  return Stbox_constructor(fcinfo, true, false, true, false);
}
'''

HELPER_SQL = '''
CREATE FUNCTION stboxT(timestamptz)
  RETURNS stbox
  AS 'MODULE_PATHNAME', 'Stbox_constructor_t'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
CREATE FUNCTION stboxT(tstzspan)
  RETURNS stbox
  AS 'MODULE_PATHNAME', 'Stbox_constructor_t'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
CREATE FUNCTION stboxXT(float, float, float, float, timestamptz, integer DEFAULT 0)
  RETURNS stbox
  AS 'MODULE_PATHNAME', 'Stbox_constructor_xt'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
CREATE FUNCTION stboxXT(float, float, float, float, tstzspan, integer DEFAULT 0)
  RETURNS stbox
  AS 'MODULE_PATHNAME', 'Stbox_constructor_xt'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
'''

HELPER_MEOS = '''
/**
 * @brief Return a spatiotemporal box from its arguments
 * @csqlfn #Stbox_constructor_t(), #Stbox_constructor_xt()
 */
STBox *
stbox_make(bool hasx, bool hasz, bool geodetic, int32_t srid, double xmin,
  double xmax, double ymin, double ymax, double zmin, double zmax, const Span *p)
{
  return NULL;
}
'''


class HelperCompositionTests(unittest.TestCase):
    """#attach_wrapper_compositions over a wrapper delegating to a shared helper: the helper
    runs as the wrapper's literals and each signature's argument types make it, so the
    signature taking a timestamptz builds its period through the public cast
    ``timestamptz_to_span`` and states a composition, while the one taking the span passes
    it as it is and keeps its signature."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for sub, name, text in (("src", "stbox.c", HELPER_WRAPPERS),
                                ("sql", "stbox.in.sql", HELPER_SQL),
                                ("meos", "stbox_meos.c", HELPER_MEOS)):
            (root / sub).mkdir()
            (root / sub / name).write_text(text)

    def tearDown(self):
        self.tmp.cleanup()

    def _attach(self):
        root = Path(self.tmp.name)
        flags_t = {"hasx": "false", "hasz": "false", "geodetic": "false", "srid": "0",
                   "xmin": "0", "xmax": "0", "ymin": "0", "ymax": "0", "zmin": "0",
                   "zmax": "0"}
        flags_xt = {"hasx": "true", "hasz": "false", "geodetic": "false", "zmin": "0",
                    "zmax": "0"}
        idl = {"functions": [
            {"name": "stbox_make", "api": "public", "sqlfn": "stboxT",
             "params": [{"name": n} for n in ("hasx", "hasz", "geodetic", "srid", "xmin",
                                              "xmax", "ymin", "ymax", "zmin", "zmax", "p")],
             "sqlSignatures": [
                 {"sqlName": "stboxT", "args": ["timestamptz"], "ret": "stbox",
                  "boundArgs": flags_t},
                 {"sqlName": "stboxT", "args": ["tstzspan"], "ret": "stbox",
                  "boundArgs": flags_t},
                 {"sqlName": "stboxXT",
                  "args": ["float", "float", "float", "float", "timestamptz", "integer"],
                  "argDefaults": [None, None, None, None, None, "0"], "ret": "stbox",
                  "boundArgs": flags_xt},
                 {"sqlName": "stboxXT",
                  "args": ["float", "float", "float", "float", "tstzspan", "integer"],
                  "argDefaults": [None, None, None, None, None, "0"], "ret": "stbox",
                  "boundArgs": flags_xt}]},
            {"name": "timestamptz_to_span", "api": "public", "params": [{"name": "t"}],
             "sqlSignatures": []}],
            "enums": [{"name": "MeosType", "values": [
                {"name": "T_TIMESTAMPTZ"}, {"name": "T_TSTZSPAN"}]}],
            "compositions": []}
        return attach_wrapper_compositions(idl, root / "src", root / "sql", root / "meos")

    def test_a_timestamptz_is_cast_to_the_period(self):
        idl, n = self._attach()
        self.assertEqual(n, 2)
        self.assertEqual(idl["compositions"][0], {
            "sqlName": "stboxT", "args": ["timestamptz"], "required": 1,
            "argDefaults": [None], "ret": "stbox",
            "operands": [{"value": "false", "param": "hasx"},
                         {"value": "false", "param": "hasz"},
                         {"value": "false", "param": "geodetic"},
                         {"value": "0", "param": "srid"},
                         {"value": "0", "param": "xmin"}, {"value": "0", "param": "xmax"},
                         {"value": "0", "param": "ymin"}, {"value": "0", "param": "ymax"},
                         {"value": "0", "param": "zmin"}, {"value": "0", "param": "zmax"},
                         {"arg": 0, "casts": ["timestamptz_to_span"], "param": "p"}],
            "call": "stbox_make"})

    def test_the_arguments_read_before_the_period_number_it(self):
        idl, _ = self._attach()
        self.assertEqual(idl["compositions"][1]["operands"], [
            {"value": "true", "param": "hasx"}, {"value": "false", "param": "hasz"},
            {"value": "false", "param": "geodetic"}, {"arg": 5, "param": "srid"},
            {"arg": 0, "param": "xmin"}, {"arg": 2, "param": "xmax"},
            {"arg": 1, "param": "ymin"}, {"arg": 3, "param": "ymax"},
            {"value": "0", "param": "zmin"}, {"value": "0", "param": "zmax"},
            {"arg": 4, "casts": ["timestamptz_to_span"], "param": "p"}])

    def test_the_span_signatures_keep_their_place(self):
        idl, _ = self._attach()
        self.assertEqual([s["args"][-1] if s["sqlName"] == "stboxT" else s["args"][4]
                          for s in idl["functions"][0]["sqlSignatures"]],
                         ["tstzspan", "tstzspan"])


if __name__ == "__main__":
    unittest.main()
