"""Regression tests for parser/boundargs.py.

A MobilityDB PG wrapper can BIND a MEOS input to a fixed literal instead of
exposing it as a SQL argument.  ``merge_boundargs`` reads the wrapper body (the
source of truth) and folds those literals into ``shape.boundArgs``, the
input-side sibling of ``shape.outParams``.

Plain unittest, no pytest dependency; writes a tiny synthetic wrapper tree.
"""
import os
import tempfile
import unittest
from pathlib import Path

from parser.boundargs import (_notnull_default, _sql_arg_params, attach_call_literals,
                              attach_null_default_binds,
                              attach_type_derived_args,
                              extract_call_literals,
                              extract_wrappers, merge_boundargs, merge_sql_arg_params,
                              resolve_bound_names, strip_call_literals)

# A synthetic MobilityDB wrapper source (mobilitydb/src/**/*.c shape).
SAMPLE = '''
/**
 * @sqlfn valueAtTimestamp()
 */
Datum
Temporal_value_at_timestamptz(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  TimestampTz t = PG_GETARG_TIMESTAMPTZ(1);
  Datum result;
  bool found = temporal_value_at_timestamptz(temp, t, true, &result);
  if (! found)
    PG_RETURN_NULL();
  PG_RETURN_DATUM(result);
}

/**
 * @sqlfn beforeTimestamp()
 */
Datum
Temporal_before_timestamptz(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  TimestampTz t = PG_GETARG_TIMESTAMPTZ(1);
  bool strict = PG_GETARG_BOOL(2);
  Temporal *result = temporal_before_timestamptz(temp, t, strict);
  PG_RETURN_TEMPORAL_P(result);
}

/**
 * @sqlfn spanFromHexWKB()
 */
Datum
Span_from_hexwkb(PG_FUNCTION_ARGS)
{
  text *hexwkb_txt = PG_GETARG_TEXT_P(0);
  char *hexwkb = text2cstring(hexwkb_txt);
  Span *result = span_from_hexwkb(hexwkb);
  PG_RETURN_SPAN_P(result);
}

/**
 * @sqlfn appendInstant()
 */
Datum
Temporal_append_tinstant(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  TInstant *inst = PG_GETARG_TINSTANT_P(1);
  interpType interp = MEOS_FLAGS_GET_INTERP(temp->flags);
  Temporal *result = temporal_append_tinstant(temp, inst, interp, 0.0, NULL, false);
  PG_RETURN_TEMPORAL_P(result);
}

/**
 * @sqlfn fooFromArray()
 */
Datum
Foo_from_array(PG_FUNCTION_ARGS)
{
  ArrayType *array = PG_GETARG_ARRAYTYPE_P(0);
  int count;
  Temporal **arr = temparr_extract(array, &count);
  Temporal *result = foo_from_array(arr, count);
  PG_RETURN_TEMPORAL_P(result);
}
'''


def _idl():
    return {"functions": [
        {"name": "temporal_value_at_timestamptz", "mdbC": "Temporal_value_at_timestamptz",
         "params": [{"name": "temp"}, {"name": "t"}, {"name": "strict"}, {"name": "result"}]},
        {"name": "temporal_before_timestamptz", "mdbC": "Temporal_before_timestamptz",
         "params": [{"name": "temp"}, {"name": "t"}, {"name": "strict"}]},
        {"name": "span_from_hexwkb", "mdbC": "Span_from_hexwkb",
         "params": [{"name": "hexwkb"}]},
        {"name": "temporal_append_tinstant", "mdbC": "Temporal_append_tinstant",
         "params": [{"name": "temp"}, {"name": "inst"}, {"name": "interp"},
                    {"name": "maxdist"}, {"name": "maxt"}, {"name": "expand"}]},
        # a function with no wrapper mapping -> untouched
        {"name": "orphan_fn", "params": [{"name": "x"}]},
        # a per-base-type collapse sibling: SHARES the wrapper (mdbC) with the generic
        # but the wrapper never calls it by name -> inherits {strict:true} by param name
        {"name": "tbool_value_at_timestamptz", "mdbC": "Temporal_value_at_timestamptz",
         "params": [{"name": "temp"}, {"name": "t"}, {"name": "strict"}, {"name": "value"}]},
        # `count` is a declared local filled by reference (`temparr_extract(array, &count)`),
        # so the `=`-assignment heuristic does not see it; it is classified by its
        # @param documentation instead of drifting.
        {"name": "foo_from_array", "mdbC": "Foo_from_array",
         "params": [{"name": "arr"}, {"name": "count"}]},
    ]}


DELEGATING = """
Datum
Jsonb_field_common(FunctionCallInfo fcinfo, bool astext)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  text *key = PG_GETARG_TEXT_P(1);
  Temporal *result = jsonb_field(temp, key, astext);
  PG_RETURN_TEMPORAL_P(result);
}

PGDLLEXPORT Datum Jsonb_field(PG_FUNCTION_ARGS);
PG_FUNCTION_INFO_V1(Jsonb_field);
Datum
Jsonb_field(PG_FUNCTION_ARGS)
{
  return Jsonb_field_common(fcinfo, false);
}

PGDLLEXPORT Datum Jsonb_field_text(PG_FUNCTION_ARGS);
PG_FUNCTION_INFO_V1(Jsonb_field_text);
Datum
Jsonb_field_text(PG_FUNCTION_ARGS)
{
  return Jsonb_field_common(fcinfo, true);
}
"""


class DelegatingWrapperTests(unittest.TestCase):
    """A wrapper that binds its literal at the DELEGATION, not at the MEOS call."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = Path(self.tmp.name) / "src"
        src.mkdir()
        (src / "deleg.c").write_text(DELEGATING)

    def tearDown(self):
        self.tmp.cleanup()

    def _idl(self, wrapper):
        return {"functions": [
            {"name": "jsonb_field", "mdbC": wrapper,
             "params": [{"name": "temp"}, {"name": "key"}, {"name": "astext"}]}]}

    def test_literal_behind_the_helper_parameter_is_captured(self):
        idl, n, drift = merge_boundargs(self._idl("Jsonb_field"), self.tmp.name)
        self.assertEqual(idl["functions"][0]["shape"]["boundArgs"], {"astext": "false"})
        self.assertEqual(drift, [])

    def test_the_sibling_wrapper_binds_the_other_literal(self):
        # Same helper, same MEOS function, opposite literal: the value follows the
        # wrapper each catalog entry names, so the two never merge into one.
        idl, n, drift = merge_boundargs(self._idl("Jsonb_field_text"), self.tmp.name)
        self.assertEqual(idl["functions"][0]["shape"]["boundArgs"], {"astext": "true"})

    def test_caller_read_args_stay_out(self):
        idl, _, _ = merge_boundargs(self._idl("Jsonb_field"), self.tmp.name)
        bound = idl["functions"][0]["shape"]["boundArgs"]
        self.assertNotIn("temp", bound)
        self.assertNotIn("key", bound)


SPECIALIZED = """
Datum
Box_constructor(FunctionCallInfo fcinfo, bool hasx, bool hast)
{
  double xmin = 0, xmax = 0, zmin = 0;
  int32_t srid = 0;
  Span *period = NULL;
  int i = 0;
  if (hasx)
  {
    xmin = PG_GETARG_FLOAT8(i++);
    xmax = PG_GETARG_FLOAT8(i++);
  }
  if (hast)
  {
    MeosType basetype = oid_meostype(get_fn_expr_argtype(fcinfo->flinfo, i));
    if (basetype == T_TSTZSPAN)
      period = PG_GETARG_SPAN_P(i++);
    else
    {
      TimestampTz t = PG_GETARG_TIMESTAMPTZ(i++);
      period = span_make(t, t, true, true, T_TIMESTAMPTZ);
    }
  }
  if (hasx)
    srid = PG_GETARG_INT32(i++);
  PG_RETURN_POINTER(box_make(hasx, srid, xmin, xmax, zmin, period));
}

Datum
Box_constructor_x(PG_FUNCTION_ARGS)
{
  return Box_constructor(fcinfo, true, false);
}

Datum
Box_constructor_xt(PG_FUNCTION_ARGS)
{
  return Box_constructor(fcinfo, true, true);
}

Datum
Terrain_index(FunctionCallInfo fcinfo, bool has_extent,
  Raster * (*func)(const Raster *, int, const Raster *, const char *))
{
  int i = has_extent ? 1 : 0;
  Raster *rast = (Raster *) PG_DETOAST_DATUM(PG_GETARG_DATUM(0));
  int band = PG_GETARG_INT32(1);
  Raster *extent = (has_extent && ! PG_ARGISNULL(2)) ?
    (Raster *) PG_DETOAST_DATUM(PG_GETARG_DATUM(2)) : NULL;
  char *pixeltype = text_to_cstring(PG_GETARG_TEXT_P(2 + i));
  PG_RETURN_POINTER(func(rast, band, extent, pixeltype));
}

Datum
Raster_tpi(PG_FUNCTION_ARGS)
{
  return Terrain_index(fcinfo, false, &raster_tpi);
}
"""


class SpecializedHelperTests(unittest.TestCase):
    """A shared helper read as the wrapper's call makes it run, as
    #DelegatingWrapperTests reads the literal a wrapper hands its helper: the flags
    decide the branches, a running index numbers the arguments, a function-pointer
    parameter calls the function the wrapper hands it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = Path(self.tmp.name) / "src"
        src.mkdir()
        (src / "specialized.c").write_text(SPECIALIZED)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _box(wrapper):
        return {"functions": [
            {"name": "box_make", "mdbC": wrapper,
             "params": [{"name": n} for n in
                        ("hasx", "srid", "xmin", "xmax", "zmin", "s")]}]}

    def test_constants_the_flags_leave_unread_are_bound(self):
        # per signature, as #test_signature_omitting_the_argument_carries_the_default
        # reads a default the call leaves the local at
        idl = self._box("Box_constructor_x")
        idl["functions"][0]["sqlSignatures"] = [
            {"args": ["float", "float", "integer"], "ret": "box"}]
        idl, _, _ = merge_boundargs(idl, self.tmp.name)
        self.assertEqual(idl["functions"][0]["shape"]["boundArgs"],
                         {"hasx": "true", "zmin": "0", "s": "NULL"})

    def test_a_running_index_states_the_argument_order(self):
        idl, n = merge_sql_arg_params(self._box("Box_constructor_x"), self.tmp.name)
        self.assertEqual(idl["functions"][0]["shape"]["sqlArgParams"],
                         ["xmin", "xmax", "srid"])
        self.assertEqual(n, 1)

    def test_a_branch_reading_one_argument_either_way_keeps_the_order(self):
        idl, _ = merge_sql_arg_params(self._box("Box_constructor_xt"), self.tmp.name)
        self.assertEqual(idl["functions"][0]["shape"]["sqlArgParams"],
                         ["xmin", "xmax", "s", "srid"])

    def test_the_function_a_pointer_parameter_receives_is_read(self):
        idl, _, _ = merge_boundargs(
            {"functions": [{"name": "raster_tpi", "mdbC": "Raster_tpi",
                            "params": [{"name": "rast"}, {"name": "band"},
                                       {"name": "customextent"},
                                       {"name": "pixeltype"}],
                            "sqlSignatures": [{"args": ["raster", "integer", "text"],
                                               "ret": "raster"}]}]},
            self.tmp.name)
        self.assertEqual(idl["functions"][0]["shape"]["boundArgs"],
                         {"customextent": "NULL"})


PG_TWIN = """
static Datum
Jsonb_path_exists_common(FunctionCallInfo fcinfo, bool tz)
{
  Jsonb *jb = PG_GETARG_JSONB_P(0);
  JsonPath *jp = PG_GETARG_JSONPATH_P(1);
  Jsonb *vars = PG_GETARG_JSONB_P(2);
  bool silent = PG_GETARG_BOOL(3);
  int result = pg_jsonb_path_exists(jb, jp, vars, silent, tz);
  PG_RETURN_BOOL(result == 1);
}

PGDLLEXPORT Datum Jsonb_path_exists(PG_FUNCTION_ARGS);
PG_FUNCTION_INFO_V1(Jsonb_path_exists);
Datum
Jsonb_path_exists(PG_FUNCTION_ARGS)
{
  return Jsonb_path_exists_common(fcinfo, false);
}

PGDLLEXPORT Datum Jsonb_path_exists_tz(PG_FUNCTION_ARGS);
PG_FUNCTION_INFO_V1(Jsonb_path_exists_tz);
Datum
Jsonb_path_exists_tz(PG_FUNCTION_ARGS)
{
  return Jsonb_path_exists_common(fcinfo, true);
}

static Datum
Jsonb_path_match_common(FunctionCallInfo fcinfo, bool tz)
{
  Jsonb *jb = PG_GETARG_JSONB_P(0);
  JsonPath *jp = PG_GETARG_JSONPATH_P(1);
  bool result = path_match_other(jb, jp, tz);
  PG_RETURN_BOOL(result);
}

PGDLLEXPORT Datum Jsonb_path_match(PG_FUNCTION_ARGS);
PG_FUNCTION_INFO_V1(Jsonb_path_match);
Datum
Jsonb_path_match(PG_FUNCTION_ARGS)
{
  return Jsonb_path_match_common(fcinfo, false);
}
"""


class PgTwinTests(unittest.TestCase):
    """A wrapper reaching a function MEOS takes from PostgreSQL through its pg_ twin, the
    delegation #DelegatingWrapperTests reads, whose helper calls the twin."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = Path(self.tmp.name) / "src"
        src.mkdir()
        (src / "twin.c").write_text(PG_TWIN)

    def tearDown(self):
        self.tmp.cleanup()

    def _idl(self, name, wrapper):
        return {"functions": [
            {"name": name, "mdbC": wrapper,
             "params": [{"name": "jb"}, {"name": "jp"}, {"name": "vars"},
                        {"name": "silent"}, {"name": "tz"}]}]}

    def test_the_twin_binds_the_literal_of_each_wrapper(self):
        for wrapper, tz in (("Jsonb_path_exists", "false"), ("Jsonb_path_exists_tz", "true")):
            idl, _, drift = merge_boundargs(self._idl("jsonb_path_exists", wrapper),
                                            self.tmp.name)
            self.assertEqual(idl["functions"][0]["shape"]["boundArgs"], {"tz": tz})
            self.assertEqual(drift, [])

    def test_a_callee_that_is_not_the_twin_binds_nothing(self):
        idl, _, _ = merge_boundargs(self._idl("jsonb_path_match", "Jsonb_path_match"),
                                    self.tmp.name)
        self.assertNotIn("boundArgs", idl["functions"][0].get("shape") or {})


class BoundArgsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = Path(self.tmp.name) / "src"
        src.mkdir()
        (src / "sample.c").write_text(SAMPLE)

    def tearDown(self):
        self.tmp.cleanup()

    def test_extract_wrappers_finds_bodies(self):
        w = extract_wrappers(self.tmp.name)
        self.assertIn("Temporal_value_at_timestamptz", w)
        self.assertIn("temporal_value_at_timestamptz(temp, t, true, &result)",
                      w["Temporal_value_at_timestamptz"])

    def test_hidden_literal_is_captured(self):
        idl, n, drift = merge_boundargs(_idl(), self.tmp.name)
        va = idl["functions"][0]
        self.assertEqual(va["shape"]["boundArgs"], {"strict": "true"})

    def test_caller_arg_is_not_captured(self):
        # beforeTimestamp exposes `strict` (PG_GETARG_BOOL(2)) -> NOT a bound literal
        idl, n, drift = merge_boundargs(_idl(), self.tmp.name)
        before = idl["functions"][1]
        self.assertNotIn("shape", before)

    def test_transform_local_is_not_drift(self):
        # `hexwkb` is caller-derived through text2cstring -> neither boundArg nor drift
        idl, n, drift = merge_boundargs(_idl(), self.tmp.name)
        hexf = idl["functions"][2]
        self.assertNotIn("shape", hexf)
        self.assertFalse([d for d in drift if d[0] == "span_from_hexwkb"])

    def test_multiple_literals_including_null_and_number(self):
        idl, n, drift = merge_boundargs(_idl(), self.tmp.name)
        app = idl["functions"][3]
        self.assertEqual(app["shape"]["boundArgs"],
                         {"maxdist": "0.0", "maxt": "NULL", "expand": "false"})
        # `interp` (a derived enum local) is not recorded as a literal
        self.assertNotIn("interp", app["shape"]["boundArgs"])

    def test_base_type_sibling_inherits_bound_literal(self):
        # tbool_value_at_timestamptz shares the wrapper but is never called by name; it
        # still inherits {strict:true} (its out-param `value` is not a bound literal)
        idl, n, drift = merge_boundargs(_idl(), self.tmp.name)
        sib = idl["functions"][5]
        self.assertEqual(sib["name"], "tbool_value_at_timestamptz")
        self.assertEqual(sib["shape"]["boundArgs"], {"strict": "true"})

    def test_orphan_and_count(self):
        idl, n, drift = merge_boundargs(_idl(), self.tmp.name)
        # orphan (no mdbC) untouched; total = generic strict + 3 append literals +
        # sibling strict = 5
        self.assertNotIn("shape", idl["functions"][4])
        self.assertEqual(n, 5)

    def test_documented_param_is_not_drift(self):
        # `count` (declared, filled via &count) is a documented @param -> caller-derived,
        # skipped systematically: no boundArg, no drift.
        idl, n, drift = merge_boundargs(_idl(), self.tmp.name,
                                        {"foo_from_array": {"arr", "count"}})
        self.assertFalse([d for d in drift if d[0] == "foo_from_array"])
        foo = next(f for f in idl["functions"] if f["name"] == "foo_from_array")
        self.assertNotIn("shape", foo)

    def test_undocumented_param_drifts(self):
        # with NO @param documentation for the parameter, the same bare identifier is
        # the exceptional gap worth inspecting -> reported as drift.
        idl, n, drift = merge_boundargs(_idl(), self.tmp.name)
        self.assertIn(("foo_from_array", "count", "unclassified-arg: count"), drift)


SIBLING_WRAPPERS = '''
Datum
Concat_jsonb_jsonbset(PG_FUNCTION_ARGS)
{
  Jsonb *jb = PG_GETARG_JSONB_P(0);
  Set *s = PG_GETARG_SET_P(1);
  Set *result = concat_jsonbset_jsonb(s, jb, INVERT);
  PG_RETURN_SET_P(result);
}

Datum
Concat_jsonbset_jsonb(PG_FUNCTION_ARGS)
{
  Set *s = PG_GETARG_SET_P(0);
  Jsonb *jb = PG_GETARG_JSONB_P(1);
  Set *result = concat_jsonbset_jsonb(s, jb, INVERT_NO);
  PG_RETURN_SET_P(result);
}

Datum
Round_left(PG_FUNCTION_ARGS)
{
  Set *s = PG_GETARG_SET_P(0);
  Set *result = set_round_to(s, 6);
  PG_RETURN_SET_P(result);
}

Datum
Round_right(PG_FUNCTION_ARGS)
{
  Set *s = PG_GETARG_SET_P(0);
  Set *result = set_round_to(s, 6);
  PG_RETURN_SET_P(result);
}
'''

SIBLING_SQL = '''
CREATE FUNCTION setConcat(jsonb, jsonbset)
  RETURNS jsonbset
  AS 'MODULE_PATHNAME', 'Concat_jsonb_jsonbset'
  LANGUAGE C IMMUTABLE STRICT;
CREATE FUNCTION setConcat(jsonbset, jsonb)
  RETURNS jsonbset
  AS 'MODULE_PATHNAME', 'Concat_jsonbset_jsonb'
  LANGUAGE C IMMUTABLE STRICT;
CREATE FUNCTION roundLeft(floatset)
  RETURNS floatset
  AS 'MODULE_PATHNAME', 'Round_left'
  LANGUAGE C IMMUTABLE STRICT;
CREATE FUNCTION roundRight(floatset)
  RETURNS floatset
  AS 'MODULE_PATHNAME', 'Round_right'
  LANGUAGE C IMMUTABLE STRICT;
'''

SIBLING_MEOS = '''
/**
 * @brief Concatenate a JSONB value to every element of a JSONB set
 * @csqlfn #Concat_jsonb_jsonbset() #Concat_jsonbset_jsonb()
 */
Set *
concat_jsonbset_jsonb(const Set *s, const Jsonb *jb, bool invert)
{
  return NULL;
}

/**
 * @brief Round the elements of a set
 * @csqlfn #Round_left() #Round_right()
 */
Set *
set_round_to(const Set *s, int maxdd)
{
  return NULL;
}
'''


DERIVED = r"""
Datum
Temporal_as_mfjson(PG_FUNCTION_ARGS)
{
  int option = 0;
  char *srs = NULL;
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  if (PG_NARGS() > 1 && ! PG_ARGISNULL(1))
    option = PG_GETARG_INT32(1);
  int32_t srid = tspatial_srid(temp);
  if (srid != SRID_UNKNOWN)
    srs = get_srs_cache_by_srid(fcinfo, srid, true);
  char *mfjson = temporal_as_mfjson(temp, option, srs);
  PG_RETURN_TEXT_P(cstring_to_text(mfjson));
}

Datum
Tgeo_space_boxes(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  int i = 1;
  GSERIALIZED *sorigin = PG_GETARG_GSERIALIZED_P(i++);
  STBox *boxes = tgeo_space_boxes(temp, sorigin);
  PG_RETURN_POINTER(boxes);
}

Datum
Raster_clip(PG_FUNCTION_ARGS)
{
  Raster *rast = PG_GETARG_RASTER_P(0);
  ArrayType *array = PG_GETARG_ARRAYTYPE_P(1);
  int nbands;
  int *bands = intarr_extract(array, &nbands);
  Raster *result = raster_clip(rast, bands, nbands);
  PG_RETURN_POINTER(result);
}

Datum
Temporal_tcount_transfn(PG_FUNCTION_ARGS)
{
  SkipList *state;
  INPUT_AGG_TRANS_STATE(fcinfo, state, ctx);
  Temporal *temp = PG_GETARG_TEMPORAL_P(1);
  state = temporal_tcount_transfn(state, temp);
  PG_RETURN_POINTER(state);
}
"""


def _derived_idl():
    def fn(name, wrapper, params, nullable):
        return {"name": name, "mdbC": wrapper, "params": [{"name": p} for p in params],
                "shape": {"nullable": nullable}}
    return {"functions": [
        fn("temporal_as_mfjson", "Temporal_as_mfjson", ["temp", "option", "srs"], ["srs"]),
        fn("tgeo_space_boxes", "Tgeo_space_boxes", ["temp", "sorigin"], ["sorigin"]),
        fn("raster_clip", "Raster_clip", ["rast", "bands", "nbands"], ["bands"]),
        fn("temporal_tcount_transfn", "Temporal_tcount_transfn", ["state", "temp"],
           ["state"])]}


class HostDerivedNullableTests(unittest.TestCase):
    """A nullable parameter the wrapper fills from a value it derives from an SQL argument
    another parameter carries itself is NULL for a binding; one carrying an SQL argument of
    its own, or threaded through the call, is not."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = Path(self.tmp.name) / "src"
        src.mkdir()
        (src / "derived.c").write_text(DERIVED)
        self.idl, _, _ = merge_boundargs(_derived_idl(), self.tmp.name)
        self.bound = {f["name"]: (f.get("shape") or {}).get("boundArgs")
                      for f in self.idl["functions"]}

    def tearDown(self):
        self.tmp.cleanup()

    def test_a_name_the_host_derives_is_null_for_a_binding(self):
        self.assertEqual(self.bound["temporal_as_mfjson"], {"srs": "NULL"})

    def test_an_argument_read_by_a_running_index_is_carried(self):
        self.assertIsNone(self.bound["tgeo_space_boxes"])

    def test_an_array_argument_derived_into_two_parameters_is_carried(self):
        self.assertIsNone(self.bound["raster_clip"])

    def test_a_state_threaded_through_the_call_is_not_bound(self):
        self.assertIsNone(self.bound["temporal_tcount_transfn"])


class SiblingWrapperTests(unittest.TestCase):
    """One MEOS function behind two wrappers, one per SQL signature."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for sub, name, text in (("src", "concat.c", SIBLING_WRAPPERS),
                                ("sql", "concat.in.sql", SIBLING_SQL),
                                ("meos", "concat_meos.c", SIBLING_MEOS)):
            (root / sub).mkdir()
            (root / sub / name).write_text(text)

    def tearDown(self):
        self.tmp.cleanup()

    def _merge(self):
        root = Path(self.tmp.name)
        idl = {"functions": [
            {"name": "concat_jsonbset_jsonb", "mdbC": "Concat_jsonb_jsonbset",
             "sqlfn": "setConcat",
             "params": [{"name": "s"}, {"name": "jb"}, {"name": "invert"}],
             "sqlSignatures": [{"args": ["jsonb", "jsonbset"], "ret": "jsonbset"},
                               {"args": ["jsonbset", "jsonb"], "ret": "jsonbset"}]},
            {"name": "set_round_to", "mdbC": "Round_left", "sqlfn": "roundLeft",
             "params": [{"name": "s"}, {"name": "maxdd"}],
             "sqlSignatures": [{"args": ["floatset"], "ret": "floatset"},
                               {"args": ["floatset"], "ret": "floatset",
                                "sqlName": "roundRight"}]}]}
        return merge_boundargs(idl, root / "src", sql_src=root / "sql",
                               meos_src=root / "meos")

    def test_each_signature_carries_its_own_wrappers_literal(self):
        idl, n, drift = self._merge()
        concat = idl["functions"][0]
        self.assertEqual([s.get("boundArgs") for s in concat["sqlSignatures"]],
                         [{"invert": "INVERT"}, {"invert": "INVERT_NO"}])
        self.assertNotIn("boundArgs", concat.get("shape", {}))

    def test_wrappers_that_agree_keep_the_function_level_map(self):
        idl, n, drift = self._merge()
        rnd = idl["functions"][1]
        self.assertEqual(rnd["shape"]["boundArgs"], {"maxdd": "6"})
        self.assertFalse([s for s in rnd["sqlSignatures"] if "boundArgs" in s])
        self.assertEqual(n, 3)

    def test_without_the_sources_only_the_primary_wrapper_is_read(self):
        idl = {"functions": [
            {"name": "concat_jsonbset_jsonb", "mdbC": "Concat_jsonb_jsonbset",
             "params": [{"name": "s"}, {"name": "jb"}, {"name": "invert"}]}]}
        idl, n, drift = merge_boundargs(idl, Path(self.tmp.name) / "src")
        self.assertEqual(idl["functions"][0]["shape"]["boundArgs"], {"invert": "INVERT"})


GENERIC_WRAPPERS = '''
Datum
Numset_shift(PG_FUNCTION_ARGS)
{
  Set *s = PG_GETARG_SET_P(0);
  Datum shift = PG_GETARG_DATUM(1);
  Set *result = numset_shift_scale(s, shift, 0, true, false);
  PG_FREE_IF_COPY(s, 0);
  PG_RETURN_SET_P(result);
}

Datum
Numset_round(PG_FUNCTION_ARGS)
{
  Set *s = PG_GETARG_SET_P(0);
  Set *result = numset_round_any(s, 3);
  PG_RETURN_SET_P(result);
}
'''

GENERIC_MEOS = '''
/**
 * @brief Return a number set shifted and/or scaled
 */
Set *
numset_shift_scale(const Set *s, Datum shift, Datum width, bool hasshift,
  bool haswidth)
{
  return NULL;
}

/**
 * @brief Return a number set rounded
 */
Set *
numset_round_any(const Set *s, int ndigits)
{
  return NULL;
}
'''


class GenericTwinTests(unittest.TestCase):
    """A wrapper calling the internal generic its tagged typed functions wrap."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for sub, name, text in (("src", "set.c", GENERIC_WRAPPERS),
                                ("meos", "set.c", GENERIC_MEOS)):
            (root / sub).mkdir()
            (root / sub / name).write_text(text)

    def tearDown(self):
        self.tmp.cleanup()

    def _idl(self):
        shift = [{"name": "s"}, {"name": "shift"}, {"name": "width"},
                 {"name": "hasshift"}, {"name": "haswidth"}]
        return {"functions": [
            {"name": "intset_shift_scale", "mdbC": "Numset_shift", "params": shift},
            {"name": "floatset_shift_scale", "mdbC": "Numset_shift", "params": shift},
            # the callee's parameters are not this member's, so it is not its generic
            {"name": "floatset_round", "mdbC": "Numset_round",
             "params": [{"name": "s"}, {"name": "maxdd"}]}]}

    def test_the_generics_literals_bind_every_member_by_name(self):
        root = Path(self.tmp.name)
        idl, n, drift = merge_boundargs(self._idl(), root / "src", meos_src=root / "meos")
        want = {"width": "0", "hasshift": "true", "haswidth": "false"}
        self.assertEqual(idl["functions"][0]["shape"]["boundArgs"], want)
        self.assertEqual(idl["functions"][1]["shape"]["boundArgs"], want)

    def test_a_callee_whose_parameters_differ_binds_nothing(self):
        root = Path(self.tmp.name)
        idl, n, drift = merge_boundargs(self._idl(), root / "src", meos_src=root / "meos")
        self.assertNotIn("boundArgs", idl["functions"][2].get("shape", {}))

    def test_without_the_meos_sources_the_generic_is_not_read(self):
        idl, n, drift = merge_boundargs(self._idl(), Path(self.tmp.name) / "src")
        self.assertNotIn("boundArgs", idl["functions"][0].get("shape", {}))


GUARDED_WRAPPERS = '''
Datum
Tspatial_as_text_common(FunctionCallInfo fcinfo, bool extended)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  int dbl_dig_for_wkt = OUT_DEFAULT_DECIMAL_DIGITS;
  if (PG_NARGS() > 1 && ! PG_ARGISNULL(1))
    dbl_dig_for_wkt = PG_GETARG_INT32(1);
  char *str = extended ? tspatial_as_ewkt(temp, dbl_dig_for_wkt) :
    tspatial_as_text(temp, dbl_dig_for_wkt);
  PG_RETURN_TEXT_P(cstring_to_text(str));
}

Datum
Tspatial_as_ewkt(PG_FUNCTION_ARGS)
{
  return Tspatial_as_text_common(fcinfo, true);
}

Datum
Tgeo_scale(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  double scale = PG_GETARG_FLOAT8(1);
  GSERIALIZED *sorigin = NULL;
  if (PG_NARGS() > 2 && !PG_ARGISNULL(2))
  {
    sorigin = PG_GETARG_GSERIALIZED_P(2);
  }
  Temporal *result = tgeo_scale(temp, scale, sorigin);
  PG_RETURN_TEMPORAL_P(result);
}

Datum
Tgeo_interp(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  interpType interp = STEP;
  if (PG_NARGS() > 1 && ! PG_ARGISNULL(1))
    interp = PG_GETARG_INT32(1);
  else
    interp = LINEAR;
  Temporal *result = tgeo_interp(temp, interp);
  PG_RETURN_TEMPORAL_P(result);
}

Datum
Tgeo_union(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  bool unary_union = temptype_supports_linear(temp->temptype);
  if (PG_NARGS() > 1 && ! PG_ARGISNULL(1))
    unary_union = PG_GETARG_BOOL(1);
  GSERIALIZED *result = tgeo_union(temp, unary_union);
  PG_RETURN_GSERIALIZED_P(result);
}

/* The default #Tgeo_scale guards with an if, chosen by a ternary */
Datum
Tpoint_to_geomeas(PG_FUNCTION_ARGS)
{
  Temporal *tpoint = PG_GETARG_TEMPORAL_P(0);
  bool segmentize = (PG_NARGS() == 2) ? PG_GETARG_BOOL(1) : false;
  GSERIALIZED *result;
  tpoint_tfloat_to_geomeas(tpoint, NULL, segmentize, &result);
  PG_RETURN_GSERIALIZED_P(result);
}

/* A literal as #Tgeo_scale passes NULL, behind a comment naming its parameter */
Datum
Union_tpcbox_tpcbox(PG_FUNCTION_ARGS)
{
  TPCBox *a = PG_GETARG_TPCBOX_P(0);
  TPCBox *b = PG_GETARG_TPCBOX_P(1);
  TPCBox *result = union_tpcbox_tpcbox(a, b, /* strict */ false);
  PG_RETURN_TPCBOX_P(result);
}
'''


class GuardedDefaultTests(unittest.TestCase):
    """A local the wrapper reads from argument k only when the call carries it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = Path(self.tmp.name) / "src"
        src.mkdir()
        (src / "guarded.c").write_text(GUARDED_WRAPPERS)

    def tearDown(self):
        self.tmp.cleanup()

    def _merge(self, func):
        return merge_boundargs({"functions": [func]}, self.tmp.name)

    def test_signature_omitting_the_argument_carries_the_default(self):
        idl, n, drift = self._merge(
            {"name": "tspatial_as_ewkt", "mdbC": "Tspatial_as_ewkt",
             "params": [{"name": "temp"}, {"name": "maxdd"}],
             "sqlSignatures": [{"args": ["tgeompoint", "integer"], "ret": "text"},
                               {"args": ["th3index"], "ret": "text"}]})
        f = idl["functions"][0]
        self.assertEqual([s.get("boundArgs") for s in f["sqlSignatures"]],
                         [None, {"maxdd": "OUT_DEFAULT_DECIMAL_DIGITS"}])
        self.assertNotIn("boundArgs", f.get("shape", {}))
        self.assertEqual((n, drift), (1, []))

    def test_every_signature_omitting_it_keeps_the_function_level_map(self):
        idl, n, _ = self._merge(
            {"name": "tspatial_as_ewkt", "mdbC": "Tspatial_as_ewkt",
             "params": [{"name": "temp"}, {"name": "maxdd"}],
             "sqlSignatures": [{"args": ["th3index"], "ret": "text"},
                               {"args": ["tquadbin"], "ret": "text"}]})
        f = idl["functions"][0]
        self.assertEqual(f["shape"]["boundArgs"], {"maxdd": "OUT_DEFAULT_DECIMAL_DIGITS"})
        self.assertFalse([s for s in f["sqlSignatures"] if "boundArgs" in s])

    def test_every_signature_stating_it_binds_nothing(self):
        idl, n, _ = self._merge(
            {"name": "tspatial_as_ewkt", "mdbC": "Tspatial_as_ewkt",
             "params": [{"name": "temp"}, {"name": "maxdd"}],
             "sqlSignatures": [{"args": ["tgeompoint", "integer"], "ret": "text"}]})
        f = idl["functions"][0]
        self.assertNotIn("shape", f)
        self.assertEqual(n, 0)

    def test_guarded_block_and_null_initializer(self):
        idl, _, _ = self._merge(
            {"name": "tgeo_scale", "mdbC": "Tgeo_scale",
             "params": [{"name": "temp"}, {"name": "scale"}, {"name": "sorigin"}],
             "sqlSignatures": [{"args": ["tgeometry", "float"], "ret": "tgeometry"},
                               {"args": ["tgeometry", "float", "geometry"],
                                "ret": "tgeometry"}]})
        f = idl["functions"][0]
        self.assertEqual([s.get("boundArgs") for s in f["sqlSignatures"]],
                         [{"sorigin": "NULL"}, None])

    def test_assignment_outside_the_guard_is_not_a_default(self):
        # the else branch assigns LINEAR when the argument is omitted, so STEP is not what
        # the call reads
        idl, n, _ = self._merge(
            {"name": "tgeo_interp", "mdbC": "Tgeo_interp",
             "params": [{"name": "temp"}, {"name": "interp"}],
             "sqlSignatures": [{"args": ["tgeompoint"], "ret": "tgeompoint"}]})
        self.assertNotIn("shape", idl["functions"][0])
        self.assertEqual(n, 0)

    def test_value_chosen_by_the_number_of_arguments_is_the_default(self):
        # #test_signature_omitting_the_argument_carries_the_default for the wrapper's own
        # ternary, which reads as an inlined helper's does
        idl, _, _ = self._merge(
            {"name": "tpoint_tfloat_to_geomeas", "mdbC": "Tpoint_to_geomeas",
             "params": [{"name": "tpoint"}, {"name": "measure"}, {"name": "segmentize"},
                        {"name": "result"}],
             "sqlSignatures": [{"args": ["tgeompoint"], "ret": "geometry"},
                               {"args": ["tgeompoint", "boolean"], "ret": "geometry"}]})
        f = idl["functions"][0]
        self.assertEqual([s.get("boundArgs") for s in f["sqlSignatures"]],
                         [{"measure": "NULL", "segmentize": "false"},
                          {"measure": "NULL"}])

    def test_literal_behind_a_comment_naming_its_parameter_is_bound(self):
        # #test_literal_behind_the_helper_parameter_is_captured for a literal the wrapper
        # writes behind a comment
        idl, n, _ = self._merge(
            {"name": "union_tpcbox_tpcbox", "mdbC": "Union_tpcbox_tpcbox",
             "params": [{"name": "box1"}, {"name": "box2"}, {"name": "strict"}],
             "sqlSignatures": [{"args": ["tpcbox", "tpcbox"], "ret": "tpcbox"}]})
        self.assertEqual(idl["functions"][0]["shape"]["boundArgs"], {"strict": "false"})
        self.assertEqual(n, 1)

    def test_computed_initializer_is_not_a_default(self):
        idl, n, _ = self._merge(
            {"name": "tgeo_union", "mdbC": "Tgeo_union",
             "params": [{"name": "temp"}, {"name": "unary_union"}],
             "sqlSignatures": [{"args": ["tgeompoint"], "ret": "geometry"}]})
        self.assertNotIn("shape", idl["functions"][0])
        self.assertEqual(n, 0)


# Tgeo_space_split and Tgeo_split_start as mobilitydb/src/geo/tgeo_tile.c states them
HELPER_INIT_WRAPPERS = '''
static void
Tgeo_split_start(FunctionCallInfo fcinfo, FuncCallContext *funcctx,
  const Temporal *temp, double xsize, double ysize, double zsize,
  const Interval *duration, const GSERIALIZED *sorigin, TimestampTz torigin,
  bool bitmatrix, bool border_inc)
{
  int ntiles;
  funcctx->user_fctx = tgeo_space_time_split_init(temp, xsize, ysize, zsize,
    duration, sorigin, torigin, bitmatrix, border_inc, &ntiles);
  get_call_result_type(fcinfo, 0, &funcctx->tuple_desc);
  BlessTupleDesc(funcctx->tuple_desc);
  return;
}

Datum
Tgeo_space_split(PG_FUNCTION_ARGS)
{
  if (SRF_IS_FIRSTCALL())
  {
    FuncCallContext *funcctx = SRF_FIRSTCALL_INIT();
    Temporal *temp = PG_GETARG_TEMPORAL_P(0);
    double xsize = PG_GETARG_FLOAT8(1);
    double ysize = 0;
    double zsize = 0;
    int i = 2;
    if (PG_NARGS() > 5)
      ysize = PG_GETARG_FLOAT8(i++);
    if (PG_NARGS() > 6)
      zsize = PG_GETARG_FLOAT8(i++);
    GSERIALIZED *sorigin = PG_GETARG_GSERIALIZED_P(i++);
    bool bitmatrix = PG_GETARG_BOOL(i++);
    bool border_inc = PG_GETARG_BOOL(i++);
    Tgeo_split_start(fcinfo, funcctx, temp, xsize, ysize, zsize, NULL,
      sorigin, 0, bitmatrix, border_inc);
  }
  return Tgeo_split_next(fcinfo);
}

static void
Other_start(FunctionCallInfo fcinfo, const Temporal *temp, double size)
{
  other_init(temp, size);
}

Datum
Tgeo_other_split(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  double size = 0;
  if (PG_NARGS() > 1)
    size = PG_GETARG_FLOAT8(1);
  Other_start(fcinfo, temp, size);
  PG_RETURN_VOID();
}
'''

HELPER_INIT_MEOS = '''
/**
 * @brief Return the state of a split of a temporal value over a grid
 */
STboxGridState *
tgeo_space_time_split_init(const Temporal *temp, double xsize, double ysize,
  double zsize, const Interval *duration, const GSERIALIZED *sorigin,
  TimestampTz torigin, bool bitmatrix, bool border_inc, int *ntiles)
{
  return NULL;
}

/**
 * @brief Return the state of another split
 */
void *
other_init(const Temporal *value, double step)
{
  return NULL;
}
'''


class HelperInitTests(unittest.TestCase):
    """A wrapper reading its sizes under PG_NARGS guards and handing them to a helper that
    calls the members' initialiser, bound by parameter name as #GenericTwinTests binds a
    generic."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        for sub, name, text in (("src", "tile.c", HELPER_INIT_WRAPPERS),
                                ("meos", "tile.c", HELPER_INIT_MEOS)):
            (root / sub).mkdir()
            (root / sub / name).write_text(text)

    def tearDown(self):
        self.tmp.cleanup()

    def _split(self):
        return {"name": "tgeo_space_split", "mdbC": "Tgeo_space_split",
                "params": [{"name": n} for n in (
                    "temp", "xsize", "ysize", "zsize", "sorigin", "bitmatrix",
                    "border_inc", "space_bins", "count")],
                "sqlSignatures": [
                    {"args": ["tgeompoint", "float", "float", "float", "geometry",
                              "boolean", "boolean"], "ret": "point_tpoint"},
                    {"args": ["tgeompoint", "float", "geometry", "boolean", "boolean"],
                     "ret": "point_tpoint"},
                    {"args": ["tgeompoint", "float", "float", "geometry", "boolean",
                              "boolean"], "ret": "point_tpoint"}]}

    def _merge(self, func, meos=True):
        root = Path(self.tmp.name)
        # the @param names run.py reads from the MEOS sources (parser.outparam)
        documented = {"tgeo_space_time_split_init": {
            "temp", "xsize", "ysize", "zsize", "duration", "sorigin", "torigin",
            "bitmatrix", "border_inc", "ntiles"}}
        return merge_boundargs({"functions": [func]}, root / "src", documented,
                               meos_src=root / "meos" if meos else None)

    def test_each_short_form_binds_the_sizes_it_omits(self):
        idl, n, drift = self._merge(self._split())
        f = idl["functions"][0]
        self.assertEqual([s.get("boundArgs") for s in f["sqlSignatures"]],
                         [None, {"ysize": "0", "zsize": "0"}, {"zsize": "0"}])
        self.assertNotIn("boundArgs", f.get("shape", {}))
        self.assertEqual((n, drift), (3, []))

    def test_a_literal_for_a_parameter_the_member_lacks_binds_nothing(self):
        # duration NULL and torigin 0 reach the initialiser, which tgeo_space_split lacks
        idl, _, _ = self._merge(self._split())
        for s in idl["functions"][0]["sqlSignatures"]:
            self.assertFalse({"duration", "torigin"} & set(s.get("boundArgs") or {}))

    def test_an_initialiser_sharing_no_parameter_name_binds_nothing(self):
        idl, n, _ = self._merge(
            {"name": "tgeo_other_split", "mdbC": "Tgeo_other_split",
             "params": [{"name": "temp"}, {"name": "width"}],
             "sqlSignatures": [{"args": ["tgeompoint"], "ret": "tgeompoint"}]})
        f = idl["functions"][0]
        self.assertFalse([s for s in f["sqlSignatures"] if "boundArgs" in s])
        self.assertNotIn("boundArgs", f.get("shape", {}))

    def test_without_the_meos_sources_the_initialiser_is_not_read(self):
        idl, _, _ = self._merge(self._split(), meos=False)
        f = idl["functions"][0]
        self.assertFalse([s for s in f["sqlSignatures"] if "boundArgs" in s])


class BoundNameValueTests(unittest.TestCase):
    """A bound literal naming a macro of a header the parse did not read gets its value."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # The family of a recorded macro comes from MobilityDB's ALL list, read from the
        # checkout MDB_SRC_ROOT names, as provisioning exports it.
        (Path(self.tmp.name) / "CMakeLists.txt").write_text(
            "if(ALL)\n  foreach(_family H3 POINTCLOUD)\n  endforeach()\nendif()\n")
        self._root = os.environ.get("MDB_SRC_ROOT")
        os.environ["MDB_SRC_ROOT"] = self.tmp.name
        self._clear_family_caches()
        inc = Path(self.tmp.name) / "include" / "temporal"
        inc.mkdir(parents=True)
        (inc / "temporal.h").write_text(
            "#define REST_AT         true\n"
            "#define REST_MINUS      false\n"
            "#define OUT_DEFAULT_DECIMAL_DIGITS 15   /* digits */\n"
            "#define SHADOWED 1\n"
            "#define GUARDED(x) (x)\n")
        (inc / "other.h").write_text("#define SHADOWED 2\n")

    def tearDown(self):
        if self._root is None:
            os.environ.pop("MDB_SRC_ROOT", None)
        else:
            os.environ["MDB_SRC_ROOT"] = self._root
        self._clear_family_caches()
        self.tmp.cleanup()

    @staticmethod
    def _clear_family_caches():
        from parser.extractors import _guard_families
        from parser.families import all_families, header_family, subdir_family
        for cached in (all_families, subdir_family, header_family, _guard_families):
            cached.cache_clear()

    def _resolve(self):
        idl = {"macros": [{"name": "WKB_NDR", "value": 8}],
               "enums": [{"name": "interpType", "values": [{"name": "LINEAR", "value": 3}]}],
               "functions": [
                   {"name": "temporal_restrict_value", "params": [],
                    "sqlSignatures": [
                        {"args": ["tint", "integer"], "boundArgs": {"atfunc": "REST_AT"}},
                        {"args": ["tint", "integer"], "boundArgs": {"atfunc": "REST_MINUS"}}]},
                   {"name": "tbox_out", "params": [],
                    "shape": {"boundArgs": {
                        "maxdd": "OUT_DEFAULT_DECIMAL_DIGITS", "variant": "WKB_NDR",
                        "interp": "LINEAR", "s": "NULL", "x": "SHADOWED", "y": "GUARDED",
                        "z": "MISSING"}}}]}
        return resolve_bound_names(idl, Path(self.tmp.name) / "include")

    def test_names_gain_their_values(self):
        idl, n, _ = self._resolve()
        vals = {m["name"]: m["value"] for m in idl["macros"]}
        self.assertIs(vals["REST_AT"], True)
        self.assertIs(vals["REST_MINUS"], False)
        self.assertEqual(vals["OUT_DEFAULT_DECIMAL_DIGITS"], 15)
        self.assertEqual(n, 3)

    def test_known_names_and_null_are_not_recorded(self):
        idl, _, _ = self._resolve()
        names = [m["name"] for m in idl["macros"]]
        self.assertEqual(names.count("WKB_NDR"), 1)
        self.assertNotIn("LINEAR", names)
        self.assertNotIn("NULL", names)

    def test_names_without_one_literal_stay_unresolved(self):
        # SHADOWED is defined twice with different values, GUARDED is function-like and
        # MISSING has no definition
        _, _, unresolved = self._resolve()
        self.assertEqual(unresolved, ["GUARDED", "MISSING", "SHADOWED"])


class CallLiteralTests(unittest.TestCase):
    """A parameter name MEOS's own calls pass one literal alone reads that literal."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        (Path(self.tmp.name) / "type_in.c").write_text(
            "GSERIALIZED *\n"
            "geom_in(const char *str, int32 typmod)\n"
            "{\n  return pg_geom_in(str, typmod);\n}\n"
            "void f(const char *s, int32 tm)\n"
            "{\n"
            "  /* geom_in(s, 7) in a comment is no call */\n"
            "  geom_in(s, -1);\n"
            "  geog_in(s, -1);\n"
            "  geog_in(s, tm);\n"
            "  geo_set_srid(g, 0);\n"
            "  geo_set_srid(g, SRID_UNKNOWN);\n"
            "}\n")
        self.functions = [
            {"name": "geom_in", "params": [{"name": "str"}, {"name": "typmod"}]},
            {"name": "geog_in", "params": [{"name": "str"}, {"name": "typmod"}]},
            {"name": "geo_set_srid", "params": [{"name": "gs"}, {"name": "srid"}]}]

    def tearDown(self):
        self.tmp.cleanup()

    def test_one_literal_alone_is_read(self):
        # the definition's typmod, the variable tm and the commented call are no literals
        lits = extract_call_literals(self.tmp.name, self.functions)
        self.assertEqual(lits, {"typmod": "-1"})

    def test_two_literals_state_none(self):
        self.assertNotIn("srid", extract_call_literals(self.tmp.name, self.functions))

    def test_the_literal_is_attached_then_stripped(self):
        idl, n = attach_call_literals({"functions": self.functions}, self.tmp.name)
        self.assertEqual(n, 2)
        self.assertEqual(idl["functions"][0]["params"][1]["_callLiteral"], "-1")
        self.assertNotIn("_callLiteral", idl["functions"][2]["params"][1])
        strip_call_literals(idl)
        self.assertFalse(any("_callLiteral" in p for f in idl["functions"]
                             for p in f["params"]))


# The sequence constructor reads the instants, the interpolation and the two inclusions, as
# Tsequence_constructor of mobilitydb/src/temporal/temporal.c does, and passes them in the
# C order of tsequence_make.
CONSTRUCTOR = """
  ArrayType *array = PG_GETARG_ARRAYTYPE_P(0);
  int count;
  TInstant **instants = (TInstant **) temparr_extract(array, &count);
  meosType temptype = instants[0]->temptype;
  interpType interp = temptype_supports_linear(temptype) ? LINEAR : STEP;
  if (PG_NARGS() > 1 && ! PG_ARGISNULL(1))
    interp = input_interp_string(fcinfo, 1);
  bool lower_inc = true, upper_inc = true;
  if (PG_NARGS() > 2 && ! PG_ARGISNULL(2))
    lower_inc = PG_GETARG_BOOL(2);
  if (PG_NARGS() > 3 && ! PG_ARGISNULL(3))
    upper_inc = PG_GETARG_BOOL(3);
  TSequence *result = tsequence_make(instants, count, lower_inc, upper_inc,
    interp, NORMALIZE);
"""
TSEQUENCE_MAKE = {"name": "tsequence_make", "params": [
    {"name": n} for n in ("instants", "count", "lower_inc", "upper_inc", "interp", "normalize")]}
# A commuted wrapper passes its arguments in the other order.
COMMUTED = """
  GSERIALIZED *gs = PG_GETARG_GSERIALIZED_P(0);
  Temporal *temp = PG_GETARG_TEMPORAL_P(1);
  Temporal *result = tdistance_tgeo_geo(temp, gs);
"""
TDISTANCE = {"name": "tdistance_tgeo_geo", "params": [{"name": "temp"}, {"name": "gs"}]}
# A commuted wrapper calling the kernel the public function calls, as Distance_value_set of
# mobilitydb/src/temporal/set_ops.c and distance_set_int of meos/src/temporal/set_ops_meos.c do;
# the public function passes its value converted.
KERNEL_WRAPPER = """
  Datum value = PG_GETARG_DATUM(0);
  Set *s = PG_GETARG_SET_P(1);
  Datum result = distance_set_value(s, value);
  PG_FREE_IF_COPY(s, 1);
  PG_RETURN_DATUM(result);
"""
KERNEL_PUBLIC = """
  VALIDATE_INTSET(s, INT_MAX);
  return (int) distance_set_value(s, (long) i);
"""
DISTANCE_SET_INT = {"name": "distance_set_int", "params": [{"name": "s"}, {"name": "i"}]}
# NAD_tbox_tnumber and nad_tint_tbox open a condition before the kernel call, in the shape of
# #KERNEL_WRAPPER and #KERNEL_PUBLIC
GUARDED_WRAPPER = """
  TBox *box = PG_GETARG_TBOX_P(0);
  Temporal *temp = PG_GETARG_TEMPORAL_P(1);
  Datum result = nad_tnumber_tbox(temp, box);
  if (datum_eq(result, 0, T_INT4))
    PG_RETURN_NULL();
  PG_RETURN_DATUM(result);
"""
GUARDED_PUBLIC = """
  if (! ensure_valid_tnumber_tbox(temp, box))
    return INT_MAX;
  return (int) nad_tnumber_tbox(temp, box);
"""
NAD_TINT_TBOX = {"name": "nad_tint_tbox", "params": [{"name": "temp"}, {"name": "box"}]}
# A wrapper reading a bytea first and passing it last, as an expression over its local.
BYTES_LAST = """
  bytea *pixels = PG_GETARG_BYTEA_PP(0);
  int width = PG_GETARG_INT32(1);
  char *pixeltype = text_to_cstring(PG_GETARG_TEXT_PP(3));
  Raster *result = raster_make(width, PG_GETARG_INT32(2), pixeltype,
    (const uint8_t *) VARDATA_ANY(pixels), (size_t) VARSIZE_ANY_EXHDR(pixels));
"""
RASTER_MAKE = {"name": "raster_make", "params": [
    {"name": n} for n in ("width", "height", "pixeltype", "pixels", "size")],
    "shape": {"inputArrays": [{"param": "pixels",
                               "lengthFrom": {"kind": "param", "name": "size"}}]}}


class SqlArgParamsTests(unittest.TestCase):
    """#_sql_arg_params of parser/boundargs.py over synthetic wrapper bodies."""

    def test_the_constructor_reads_its_arguments_in_another_order(self):
        self.assertEqual(_sql_arg_params(CONSTRUCTOR, TSEQUENCE_MAKE),
                         ["instants", "interp", "lower_inc", "upper_inc"])

    def test_a_commuted_wrapper_reads_the_second_parameter_first(self):
        self.assertEqual(_sql_arg_params(COMMUTED, TDISTANCE), ["gs", "temp"])

    def test_the_c_order_is_not_stated(self):
        body = COMMUTED.replace("tdistance_tgeo_geo(temp, gs)", "tdistance_tgeo_geo(gs, temp)")
        self.assertIsNone(_sql_arg_params(body, {"name": "tdistance_tgeo_geo",
                                                  "params": [{"name": "gs"}, {"name": "temp"}]}))

    def test_a_shared_kernel_states_the_order(self):
        """#test_a_commuted_wrapper_reads_the_second_parameter_first, through a kernel."""
        self.assertEqual(_sql_arg_params(KERNEL_WRAPPER, DISTANCE_SET_INT, KERNEL_PUBLIC),
                         ["i", "s"])

    def test_a_condition_is_not_the_kernel(self):
        """#test_a_shared_kernel_states_the_order where both bodies open an `if` before the
        kernel call: the condition is no call, so the kernel is nad_tnumber_tbox."""
        self.assertEqual(_sql_arg_params(GUARDED_WRAPPER, NAD_TINT_TBOX, GUARDED_PUBLIC),
                         ["box", "temp"])

    def test_a_shared_kernel_in_the_c_order_is_not_stated(self):
        """#test_the_c_order_is_not_stated, through a kernel."""
        body = KERNEL_WRAPPER.replace("PG_GETARG_DATUM(0)", "PG_GETARG_DATUM(1)").replace(
            "PG_GETARG_SET_P(1)", "PG_GETARG_SET_P(0)")
        self.assertIsNone(_sql_arg_params(body, DISTANCE_SET_INT, KERNEL_PUBLIC))

    def test_without_the_public_body_nothing_is_stated(self):
        """#test_a_shared_kernel_states_the_order without the body of the public function."""
        self.assertIsNone(_sql_arg_params(KERNEL_WRAPPER, DISTANCE_SET_INT))

    def test_an_expression_argument_carries_what_its_local_carries(self):
        """A bytea read first and passed last, as an expression over its local, with its
        length beside it: the length travels with its array and carries no argument."""
        self.assertEqual(_sql_arg_params(BYTES_LAST, RASTER_MAKE),
                         ["pixels", "width", "height", "pixeltype"])

    def test_an_expression_reading_two_arguments_carries_none(self):
        """#test_an_expression_argument_carries_what_its_local_carries where the expression
        reads two locals carrying different arguments, so the order cannot be read."""
        body = BYTES_LAST.replace("VARDATA_ANY(pixels)", "VARDATA_ANY(pixels) + width")
        self.assertIsNone(_sql_arg_params(body, RASTER_MAKE))


IDL = Path(__file__).resolve().parent.parent / "output" / "meos-idl.json"


class SqlArgParamsContractTests(unittest.TestCase):
    """Over the generated catalog: the sequence constructor states the order its wrapper reads."""

    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        import json
        self.fns = {f["name"]: f for f in json.loads(IDL.read_text())["functions"]}

    def test_the_sequence_constructor_reads_the_interpolation_second(self):
        self.assertEqual(self.fns["tsequence_make"]["shape"]["sqlArgParams"],
                         ["instants", "interp", "lower_inc", "upper_inc"])

    def test_the_number_first_reads_through_the_shared_kernel(self):
        """#test_the_sequence_constructor_reads_the_interpolation_second, for
        nearestApproachDistance(float, tfloat), whose wrapper NAD_number_tnumber calls the
        kernel nad_tfloat_float calls."""
        sigs = self.fns["nad_tfloat_float"]["sqlSignatures"]
        self.assertEqual([s.get("sqlArgParams") for s in sigs], [None, ["d", "temp"]])


# The interpolation two wrappers take from the class of a temporal type, of the call's
# argument 1 and of its return, where the call carries none (Temporal_append_tinstant,
# Tsequenceset_constructor_gaps).
TYPE_DERIVED = '''
PGDLLEXPORT Datum Temporal_append_tinstant(PG_FUNCTION_ARGS);
PG_FUNCTION_INFO_V1(Temporal_append_tinstant);
Datum
Temporal_append_tinstant(PG_FUNCTION_ARGS)
{
  Temporal *temp = PG_GETARG_TEMPORAL_P(0);
  TInstant *inst = PG_GETARG_TINSTANT_P(1);
  interpType interp;
  if (PG_NARGS() == 2 || PG_ARGISNULL(2))
  {
    MeosType temptype = oid_meostype(get_fn_expr_argtype(fcinfo->flinfo, 1));
    interp = temptype_supports_linear(temptype) ? LINEAR : STEP;
  }
  else
    interp = input_interp_string(fcinfo, 2);
  Temporal *result = temporal_append_tinstant(temp, inst, interp, 0.0, NULL, false);
  PG_RETURN_TEMPORAL_P(result);
}

PGDLLEXPORT Datum Tsequenceset_constructor_gaps(PG_FUNCTION_ARGS);
PG_FUNCTION_INFO_V1(Tsequenceset_constructor_gaps);
Datum
Tsequenceset_constructor_gaps(PG_FUNCTION_ARGS)
{
  ArrayType *array = PG_GETARG_ARRAYTYPE_P(0);
  double maxdist = -1.0;
  Interval *maxt = NULL;
  MeosType temptype = oid_meostype(get_fn_expr_rettype(fcinfo->flinfo));
  interpType interp = temptype_supports_linear(temptype) ? LINEAR : STEP;
  if (PG_NARGS() > 1 && ! PG_ARGISNULL(1))
    maxt = PG_GETARG_INTERVAL_P(1);
  if (PG_NARGS() > 2 && ! PG_ARGISNULL(2))
    maxdist = PG_GETARG_FLOAT8(2);
  if (PG_NARGS() > 3 && ! PG_ARGISNULL(3))
    interp = input_interp_string(fcinfo, 3);
  int count;
  TInstant **instants = (TInstant **) temparr_extract(array, &count);
  TSequenceSet *result = tsequenceset_make_gaps(instants, count, interp, maxt, maxdist);
  PG_RETURN_TSEQUENCESET_P(result);
}
'''


class TypeDerivedArgTests(unittest.TestCase):
    """#attach_type_derived_args binds the interpolation a wrapper derives from the
    temporal type of a signature, as #DelegatingWrapperTests reads a literal behind a
    helper, only on the signatures that do not carry it."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = Path(self.tmp.name) / "src"
        src.mkdir()
        (src / "temporal.c").write_text(TYPE_DERIVED)

    def tearDown(self):
        self.tmp.cleanup()

    @staticmethod
    def _idl():
        def sig(name, args, ret):
            return {"sqlName": name, "args": args, "ret": ret}
        return {
            "temporalTypes": {"tfloat": {"linear": True}, "tint": {"linear": False}},
            "functions": [
                {"name": "temporal_append_tinstant", "mdbC": "Temporal_append_tinstant",
                 "params": [{"name": n} for n in
                            ("temp", "inst", "interp", "maxdist", "maxt", "expand")],
                 "sqlSignatures": [sig("appendInstant", ["tfloat", "tfloat"], "tfloat"),
                                   sig("appendInstant", ["tint", "tint"], "tint"),
                                   sig("appendInstant", ["tfloat", "tfloat", "text"],
                                       "tfloat")]},
                {"name": "tsequenceset_make_gaps", "mdbC": "Tsequenceset_constructor_gaps",
                 "params": [{"name": n} for n in
                            ("instants", "count", "interp", "maxt", "maxdist")],
                 "sqlSignatures": [sig("tintSeqSetGaps", ["tint[]", "interval", "float"],
                                       "tint"),
                                   sig("tfloatSeqSetGaps",
                                       ["tfloat[]", "interval", "float", "text"],
                                       "tfloat")]}]}

    def test_the_class_of_the_argument_type_is_bound(self):
        idl, n = attach_type_derived_args(self._idl(), self.tmp.name)
        sigs = idl["functions"][0]["sqlSignatures"]
        self.assertEqual(sigs[0]["boundArgs"], {"interp": "LINEAR"})
        self.assertEqual(sigs[1]["boundArgs"], {"interp": "STEP"})

    def test_the_class_of_the_return_type_is_bound(self):
        idl, _ = attach_type_derived_args(self._idl(), self.tmp.name)
        self.assertEqual(idl["functions"][1]["sqlSignatures"][0]["boundArgs"],
                         {"interp": "STEP"})

    def test_a_signature_carrying_the_interpolation_binds_none(self):
        idl, n = attach_type_derived_args(self._idl(), self.tmp.name)
        self.assertNotIn("boundArgs", idl["functions"][0]["sqlSignatures"][2])
        self.assertNotIn("boundArgs", idl["functions"][1]["sqlSignatures"][1])
        self.assertEqual(n, 3)


class NullDefaultBindTests(unittest.TestCase):
    """#attach_null_default_binds states what a wrapper passes for an argument left to its
    NULL default, read from the wrapper of #TypeDerivedArgTests, and only for a CREATE
    FUNCTION that is not STRICT."""

    SQL = '''
CREATE FUNCTION tintSeqSetGaps(tint[], maxt interval DEFAULT NULL,
    maxdist float DEFAULT NULL)
  RETURNS tint
  AS 'MODULE_PATHNAME', 'Tsequenceset_constructor_gaps'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
CREATE FUNCTION tfloatSeqSetGaps(tfloat[], maxt interval DEFAULT NULL,
    maxdist float DEFAULT NULL)
  RETURNS tfloat
  AS 'MODULE_PATHNAME', 'Tsequenceset_constructor_gaps'
  LANGUAGE C IMMUTABLE STRICT PARALLEL SAFE;
'''

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "src").mkdir()
        (root / "src" / "temporal.c").write_text(TYPE_DERIVED)
        (root / "sql").mkdir()
        (root / "sql" / "022_temporal.in.sql").write_text(self.SQL)

    def tearDown(self):
        self.tmp.cleanup()

    def _idl(self):
        def sig(name, t):
            return {"sqlName": name, "args": [t + "[]", "interval", "float"], "ret": t,
                    "argDefaults": [None, "NULL", "NULL"]}
        return {
            "temporalTypes": {"tfloat": {"linear": True}, "tint": {"linear": False}},
            "functions": [
                {"name": "tsequenceset_make_gaps", "mdbC": "Tsequenceset_constructor_gaps",
                 "params": [{"name": n} for n in
                            ("instants", "count", "interp", "maxt", "maxdist")],
                 "sqlSignatures": [sig("tintSeqSetGaps", "tint"),
                                   sig("tfloatSeqSetGaps", "tfloat")]}]}

    def test_each_null_default_takes_the_value_the_wrapper_passes(self):
        idl, n = attach_null_default_binds(self._idl(), Path(self.tmp.name) / "src",
                                           Path(self.tmp.name) / "sql")
        self.assertEqual(idl["functions"][0]["sqlSignatures"][0]["nullDefaultBinds"],
                         {"1": {"maxt": "NULL"}, "2": {"maxdist": "-1.0"}})
        self.assertEqual(n, 2)

    def test_a_strict_function_binds_none(self):
        # PostgreSQL answers NULL for the NULL argument without calling the wrapper
        idl, _ = attach_null_default_binds(self._idl(), Path(self.tmp.name) / "src",
                                           Path(self.tmp.name) / "sql")
        self.assertNotIn("nullDefaultBinds", idl["functions"][0]["sqlSignatures"][1])


ARITY_BRANCH = r'''
Datum
Temporal_app_tinst_transfn(PG_FUNCTION_ARGS)
{
  Temporal *state = PG_ARGISNULL(0) ? NULL : PG_GETARG_TEMPORAL_P(0);
  Temporal *inst = PG_GETARG_TEMPORAL_P(1);
  interpType interp;
  if (PG_NARGS() == 2 || PG_ARGISNULL(2))
  {
    MeosType temptype = oid_meostype(get_fn_expr_argtype(fcinfo->flinfo, 1));
    interp = temptype_supports_linear(temptype) ? LINEAR : STEP;
  }
  else
    interp = input_interp_string(fcinfo, 2);
  double maxdist = -1.0;
  Interval *maxt = NULL;
  if (PG_NARGS() > 3)
  {
    if (PG_NARGS() == 4)
    {
      if (! PG_ARGISNULL(3))
        maxt = PG_GETARG_INTERVAL_P(3);
    }
    else /* PG_NARGS() == 5 */
    {
      if (! PG_ARGISNULL(3))
        maxdist = PG_GETARG_FLOAT8(3);
      if (! PG_ARGISNULL(4))
        maxt = PG_GETARG_INTERVAL_P(4);
    }
  }
  state = temporal_app_tinst_transfn(state, (TInstant *) inst, interp, maxdist, maxt);
  PG_RETURN_TEMPORAL_P(state);
}
'''


class ArityBranchTests(unittest.TestCase):
    """A wrapper choosing what it reads by a test of ``PG_NARGS()`` other than ``> k``, and
    by the ``else`` of such a test, read as #GuardedDefaultTests and #NullDefaultBindTests
    read one testing ``PG_NARGS() > k``: each signature binds what a call of its number of
    arguments leaves to the literal a local starts from, and each NULL default is paired
    with the parameter its argument feeds for that number."""

    SQL = '''
CREATE FUNCTION appendInstantTransition(tbool, tbool, interp text DEFAULT NULL,
    maxt interval DEFAULT NULL)
  RETURNS tbool
  AS 'MODULE_PATHNAME', 'Temporal_app_tinst_transfn'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
CREATE FUNCTION appendInstantTransition(tint, tint, interp text DEFAULT NULL,
    maxdist float DEFAULT NULL, maxt interval DEFAULT NULL)
  RETURNS tint
  AS 'MODULE_PATHNAME', 'Temporal_app_tinst_transfn'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
'''

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "src").mkdir()
        (root / "src" / "temporal_aggfuncs.c").write_text(ARITY_BRANCH)
        (root / "sql").mkdir()
        (root / "sql" / "040_temporal_aggfuncs.in.sql").write_text(self.SQL)

    def tearDown(self):
        self.tmp.cleanup()

    def _func(self):
        return {"name": "temporal_app_tinst_transfn", "mdbC": "Temporal_app_tinst_transfn",
                "sqlfn": "appendInstantTransition",
                "params": [{"name": n} for n in
                           ("state", "inst", "interp", "maxdist", "maxt")],
                "sqlSignatures": [
                    {"args": ["tbool", "tbool"], "ret": "tbool"},
                    {"args": ["tbool", "tbool", "text"], "ret": "tbool"},
                    {"args": ["tbool", "tbool", "text", "interval"], "ret": "tbool",
                     "argDefaults": [None, None, "NULL", "NULL"]},
                    {"args": ["tint", "tint", "text", "float", "interval"], "ret": "tint",
                     "argDefaults": [None, None, "NULL", "NULL", "NULL"]}]}

    def test_each_number_of_arguments_binds_what_it_leaves(self):
        idl, _, _ = merge_boundargs({"functions": [self._func()]}, self.tmp.name)
        self.assertEqual(
            [s.get("boundArgs") for s in idl["functions"][0]["sqlSignatures"]],
            [{"maxdist": "-1.0", "maxt": "NULL"}, {"maxdist": "-1.0", "maxt": "NULL"},
             {"maxdist": "-1.0"}, None])

    def test_a_null_default_feeds_the_parameter_its_number_reads(self):
        idl = {"temporalTypes": {"tbool": {"linear": False}, "tint": {"linear": False}},
               "functions": [self._func()]}
        idl, _ = attach_null_default_binds(idl, Path(self.tmp.name) / "src",
                                           Path(self.tmp.name) / "sql")
        sigs = idl["functions"][0]["sqlSignatures"]
        self.assertEqual(sigs[2]["nullDefaultBinds"],
                         {"2": {"interp": "STEP"}, "3": {"maxt": "NULL"}})
        self.assertEqual(sigs[3]["nullDefaultBinds"],
                         {"2": {"interp": "STEP"}, "3": {"maxdist": "-1.0"},
                          "4": {"maxt": "NULL"}})


PRESENCE_FLAG = r'''
Datum
Raster_reclass(PG_FUNCTION_ARGS)
{
  Raster *rast = (Raster *) PG_DETOAST_DATUM(PG_GETARG_DATUM(0));
  int band = PG_GETARG_INT32(1);
  char *expr = text_to_cstring(PG_GETARG_TEXT_P(2));
  char *pixeltype = text_to_cstring(PG_GETARG_TEXT_P(3));
  bool has_nodata = ! PG_ARGISNULL(4);
  double nodataval = has_nodata ? PG_GETARG_FLOAT8(4) : 0.0;
  Raster *result = raster_reclass(rast, band, expr, pixeltype, has_nodata,
    nodataval);
  PG_RETURN_POINTER(result);
}

Datum
Raster_absent(PG_FUNCTION_ARGS)
{
  Raster *rast = (Raster *) PG_DETOAST_DATUM(PG_GETARG_DATUM(0));
  bool no_value = PG_ARGISNULL(1);
  double value = no_value ? -1.0 : PG_GETARG_FLOAT8(1);
  Raster *result = raster_absent(rast, value, no_value);
  PG_RETURN_POINTER(result);
}
'''


class PresenceFlagTests(unittest.TestCase):
    """#attach_null_default_binds reads a local stating whether a NULL default argument is
    given (#_presence_flag) as a parameter the argument feeds: ``nullDefaultBinds`` states
    what it and the value take when the argument is NULL, ``presentBinds`` what the flag
    takes when it is given, whichever way the flag is written."""

    SQL = '''
CREATE FUNCTION reclass(rast raster, nband integer, reclassexpr text,
    pixeltype text, nodataval float DEFAULT NULL)
  RETURNS raster
  AS 'MODULE_PATHNAME', 'Raster_reclass'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
CREATE FUNCTION absent(rast raster, value float DEFAULT NULL)
  RETURNS raster
  AS 'MODULE_PATHNAME', 'Raster_absent'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
'''

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "src").mkdir()
        (root / "src" / "temporal_raster.c").write_text(PRESENCE_FLAG)
        (root / "sql").mkdir()
        (root / "sql" / "500_raster.in.sql").write_text(self.SQL)

    def tearDown(self):
        self.tmp.cleanup()

    def _idl(self):
        return {"functions": [
            {"name": "raster_reclass", "mdbC": "Raster_reclass", "sqlfn": "reclass",
             "params": [{"name": n} for n in
                        ("rast", "band", "expr", "pixeltype", "has_nodata", "nodataval")],
             "sqlSignatures": [{"args": ["raster", "integer", "text", "text", "float"],
                                "ret": "raster",
                                "argDefaults": [None, None, None, None, "NULL"]}]},
            {"name": "raster_absent", "mdbC": "Raster_absent", "sqlfn": "absent",
             "params": [{"name": n} for n in ("rast", "value", "no_value")],
             "sqlSignatures": [{"args": ["raster", "float"], "ret": "raster",
                                "argDefaults": [None, "NULL"]}]}]}

    def test_a_flag_and_its_value_take_what_the_wrapper_passes(self):
        idl, n = attach_null_default_binds(self._idl(), Path(self.tmp.name) / "src",
                                           Path(self.tmp.name) / "sql")
        sig = idl["functions"][0]["sqlSignatures"][0]
        self.assertEqual(sig["nullDefaultBinds"],
                         {"4": {"has_nodata": "false", "nodataval": "0.0"}})
        self.assertEqual(sig["presentBinds"], {"4": {"has_nodata": "true"}})

    def test_a_flag_stating_the_argument_is_absent_is_read_the_other_way(self):
        idl, n = attach_null_default_binds(self._idl(), Path(self.tmp.name) / "src",
                                           Path(self.tmp.name) / "sql")
        sig = idl["functions"][1]["sqlSignatures"][0]
        self.assertEqual(sig["nullDefaultBinds"],
                         {"1": {"value": "-1.0", "no_value": "true"}})
        self.assertEqual(sig["presentBinds"], {"1": {"no_value": "false"}})
        self.assertEqual(n, 2)


ARGUMENT_HELPER = r'''
static bool
value_count_args(FunctionCallInfo fcinfo, bool scalar, int *band,
  bool *exclude_nodata, double **searchvalues, int *nsearch, double *roundto)
{
  int nargs = PG_NARGS();
  /* The search values stand before the rounding, the last argument */
  int s = nargs - 2;
  if (PG_ARGISNULL(0) || PG_ARGISNULL(nargs - 1))
    return false;
  *band = nargs >= 4 ? PG_GETARG_INT32(1) : 1;
  *exclude_nodata = nargs == 5 ? PG_GETARG_BOOL(2) : true;
  *roundto = PG_GETARG_FLOAT8(nargs - 1);
  *searchvalues = NULL;
  *nsearch = 0;
  if (scalar)
  {
    *searchvalues = palloc(sizeof(double));
    (*searchvalues)[0] = PG_GETARG_FLOAT8(s);
    *nsearch = 1;
  }
  else if (! PG_ARGISNULL(s))
  {
    Datum *elems = datumarr_extract(PG_GETARG_ARRAYTYPE_P(s), nsearch);
    if (*nsearch > 0)
      *searchvalues = palloc(sizeof(double) * (size_t) *nsearch);
  }
  return true;
}

static Datum
Raster_value_count_rows(FunctionCallInfo fcinfo, bool percent)
{
  int band, nsearch, count = 0;
  bool exclude_nodata;
  double *searchvalues, roundto;
  ValueCount *rows = NULL;
  if (value_count_args(fcinfo, false, &band, &exclude_nodata, &searchvalues,
      &nsearch, &roundto))
  {
    Raster *rast = (Raster *) PG_DETOAST_DATUM(PG_GETARG_DATUM(0));
    rows = raster_value_count(rast, band, exclude_nodata, searchvalues,
      nsearch, roundto, &count);
  }
  PG_RETURN_POINTER(rows);
}

Datum
Raster_value_count(PG_FUNCTION_ARGS)
{
  return Raster_value_count_rows(fcinfo, false);
}

static void
jsonb_path_args(FunctionCallInfo fcinfo, Jsonb **vars, bool *silent)
{
  *vars = PG_NARGS() > 2 ? PG_GETARG_JSONB_P(2) : NULL;
  *silent = PG_NARGS() > 3 ? PG_GETARG_BOOL(3) : false;
}

Datum
Jsonb_path_exists(PG_FUNCTION_ARGS)
{
  Jsonb *jb = PG_GETARG_JSONB_P(0);
  JsonPath *jp = PG_GETARG_JSONPATH_P(1);
  Jsonb *vars;
  bool silent;
  jsonb_path_args(fcinfo, &vars, &silent);
  int result = pg_jsonb_path_exists(jb, jp, vars, silent, false);
  PG_RETURN_BOOL(result > 0);
}
'''


class ArgumentHelperTests(unittest.TestCase):
    """A wrapper whose locals an argument helper fills through their addresses, choosing
    each by the number of arguments, is read as #ArityBranchTests reads one choosing in
    its own body: each signature binds what its number of arguments leaves to a literal,
    and a NULL default the helper replaces only when the argument is not NULL, at an
    index it computes from the number of arguments, passes the values it starts from."""

    SQL = '''
CREATE FUNCTION valueCount(rast raster, nband integer DEFAULT 1,
    exclude_nodata_value boolean DEFAULT true,
    searchvalues float[] DEFAULT NULL, roundto float DEFAULT 0,
    OUT value float, OUT count integer)
  RETURNS SETOF record
  AS 'MODULE_PATHNAME', 'Raster_value_count'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
CREATE FUNCTION valueCount(rast raster, nband integer, searchvalues float[],
    roundto float DEFAULT 0, OUT value float, OUT count integer)
  RETURNS SETOF record
  AS 'MODULE_PATHNAME', 'Raster_value_count'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
CREATE FUNCTION valueCount(rast raster, searchvalues float[],
    roundto float DEFAULT 0, OUT value float, OUT count integer)
  RETURNS SETOF record
  AS 'MODULE_PATHNAME', 'Raster_value_count'
  LANGUAGE C IMMUTABLE PARALLEL SAFE;
'''

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / "src").mkdir()
        (root / "src" / "temporal_raster.c").write_text(ARGUMENT_HELPER)
        (root / "sql").mkdir()
        (root / "sql" / "500_raster.in.sql").write_text(self.SQL)

    def tearDown(self):
        self.tmp.cleanup()

    def _func(self):
        def sig(args, defaults):
            return {"sqlName": "valueCount", "args": args, "ret": "record",
                    "retSet": True, "argDefaults": defaults}
        return {"name": "raster_value_count", "mdbC": "Raster_value_count",
                "sqlfn": "valueCount",
                "params": [{"name": n} for n in
                           ("rast", "band", "exclude_nodata", "searchvalues", "nsearch",
                            "roundto", "count")],
                "sqlSignatures": [
                    sig(["raster", "integer", "boolean", "float[]", "float"],
                        [None, "1", "true", "NULL", "0"]),
                    sig(["raster", "integer", "float[]", "float"], [None, None, None, "0"]),
                    sig(["raster", "float[]", "float"], [None, None, "0"])]}

    def test_each_number_of_arguments_binds_what_the_helper_leaves(self):
        idl, _, drift = merge_boundargs({"functions": [self._func()]}, self.tmp.name)
        self.assertEqual(
            [s.get("boundArgs") for s in idl["functions"][0]["sqlSignatures"]],
            [None, {"exclude_nodata": "true"},
             {"band": "1", "exclude_nodata": "true"}])
        self.assertEqual(drift, [])

    def test_a_null_default_replaced_only_when_given_passes_its_start(self):
        idl = {"functions": [self._func()]}
        idl, n = attach_null_default_binds(idl, Path(self.tmp.name) / "src",
                                           Path(self.tmp.name) / "sql")
        sigs = idl["functions"][0]["sqlSignatures"]
        self.assertEqual(sigs[0]["nullDefaultBinds"],
                         {"3": {"searchvalues": "NULL", "nsearch": "0"}})
        self.assertEqual(n, 1)

    def test_every_signature_stating_every_argument_binds_nothing(self):
        # jsonbPathExists states all four of the arguments the helper reads
        func = {"name": "jsonb_path_exists", "mdbC": "Jsonb_path_exists",
                "params": [{"name": n} for n in ("jb", "jp", "vars", "silent", "tz")],
                "sqlSignatures": [{"args": ["jsonb", "jsonpath", "jsonb", "boolean"],
                                   "ret": "boolean"}]}
        idl, _, _ = merge_boundargs({"functions": [func]}, self.tmp.name)
        self.assertEqual(idl["functions"][0].get("shape", {}).get("boundArgs"),
                         {"tz": "false"})
        self.assertNotIn("boundArgs", idl["functions"][0]["sqlSignatures"][0])



SET_VALUES_WRAPPER = r"""
Datum
Raster_set_values(PG_FUNCTION_ARGS)
{
  Raster *rast = (Raster *) PG_DETOAST_DATUM(PG_GETARG_DATUM(0));
  bool *noset = NULL;
  int nrows = 0, ncols = 0;
  if (! PG_ARGISNULL(5))
  {
    int ncount;
    bool *nnulls;
    Datum *nelems = raster_block_extract(PG_GETARG_ARRAYTYPE_P(5), &nrows,
      &ncols, &nnulls, &ncount);
  }
  Raster *result = raster_set_values(rast, noset, ncols, nrows);
  PG_RETURN_POINTER(result);
}
"""


class DeclarationListTests(unittest.TestCase):
    """#_notnull_default over the locals of one declaration, as #_guarded_default reads
    them: `int nrows = 0, ncols = 0;` starts both from 0, each written only where
    argument 5 is not NULL, as Raster_set_values reads the size of its flags."""

    def test_each_local_of_the_declaration_starts_from_its_literal(self):
        self.assertEqual(_notnull_default(SET_VALUES_WRAPPER, "ncols", 7), (5, "0"))
        self.assertEqual(_notnull_default(SET_VALUES_WRAPPER, "nrows", 7), (5, "0"))

    def test_a_local_starting_from_a_call_states_no_literal(self):
        body = SET_VALUES_WRAPPER.replace("int nrows = 0, ncols = 0;",
                                          "int nrows = f(a, b), ncols = 0;")
        self.assertIsNone(_notnull_default(body, "nrows", 7))
        self.assertEqual(_notnull_default(body, "ncols", 7), (5, "0"))


if __name__ == "__main__":
    unittest.main()
