"""Regression tests for parser/boundargs.py.

A MobilityDB PG wrapper can BIND a MEOS input to a fixed literal instead of
exposing it as a SQL argument.  ``merge_boundargs`` reads the wrapper body (the
source of truth) and folds those literals into ``shape.boundArgs``, the
input-side sibling of ``shape.outParams``.

Plain unittest, no pytest dependency; writes a tiny synthetic wrapper tree.
"""
import tempfile
import unittest
from pathlib import Path

from parser.boundargs import extract_wrappers, merge_boundargs

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


if __name__ == "__main__":
    unittest.main()
