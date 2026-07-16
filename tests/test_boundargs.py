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
    ]}


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

    def test_orphan_and_count(self):
        idl, n, drift = merge_boundargs(_idl(), self.tmp.name)
        # orphan (no mdbC) untouched; total = strict + 3 append literals = 4
        self.assertNotIn("shape", idl["functions"][4])
        self.assertEqual(n, 4)


if __name__ == "__main__":
    unittest.main()
