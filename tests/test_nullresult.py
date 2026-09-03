"""Unit tests for parser/nullresult.py.

Runs without libclang or pytest:  python3 tests/test_nullresult.py

Covers the three behaviours the field depends on: (1) the guard is read from the
PG wrapper that states it, (2) a wrapper that delegates `fcinfo` to a shared
helper inherits the helper's guard -- a third of the tree takes that shape, and
reading only the wrapper reports the whole restriction family as always
answering -- and (3) a `PG_ARGISNULL` guard is NOT an absence guard, since it
propagates a null argument rather than saying whether an answer exists.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser.nullresult import attach_null_result, extract_null_results


def _tree(text, rel="src/temporal/temporal.c"):
    """A mobilitydb-shaped checkout holding one wrapper source."""
    root = tempfile.mkdtemp()
    p = Path(root) / "mobilitydb" / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text)
    return str(Path(root) / "mobilitydb" / "src")


class GuardTests(unittest.TestCase):
    def test_reads_the_guard_the_wrapper_states(self):
        src = _tree("""
Datum
Temporal_at_value(PG_FUNCTION_ARGS)
{
  Temporal *result = temporal_at_value(temp, value);
  if (! result)
    PG_RETURN_NULL();
  PG_RETURN_TEMPORAL_P(result);
}
""")
        self.assertEqual({"Temporal_at_value": "if (! result)"},
                         extract_null_results(src))

    def test_a_sentinel_guard_is_carried_verbatim(self):
        src = _tree("""
Datum
NAD_tpoint_geo(PG_FUNCTION_ARGS)
{
  double result = nad_tpoint_geo(temp, gs);
  if (result == DBL_MAX)
    PG_RETURN_NULL();
  PG_RETURN_FLOAT8(result);
}
""")
        self.assertEqual({"NAD_tpoint_geo": "if (result == DBL_MAX)"},
                         extract_null_results(src))

    def test_a_wrapper_that_always_answers_carries_no_guard(self):
        src = _tree("""
Datum
Tbool_out(PG_FUNCTION_ARGS)
{
  char *result = tbool_out(temp);
  PG_RETURN_CSTRING(result);
}
""")
        self.assertEqual({}, extract_null_results(src))


class DelegationTests(unittest.TestCase):
    def test_a_delegating_wrapper_inherits_its_helper_guard(self):
        src = _tree("""
static Datum
Temporal_restrict_timestamptz(FunctionCallInfo fcinfo, bool atfunc)
{
  Temporal *result = temporal_restrict_timestamptz(temp, t, atfunc);
  if (! result)
    PG_RETURN_NULL();
  PG_RETURN_TEMPORAL_P(result);
}

Datum
Temporal_at_timestamptz(PG_FUNCTION_ARGS)
{
  return Temporal_restrict_timestamptz(fcinfo, REST_AT);
}
""")
        found = extract_null_results(src)
        self.assertEqual("if (! result)", found.get("Temporal_at_timestamptz"))

    def test_a_delegation_cycle_terminates(self):
        # A ring names no guard and must not hang or recurse without end.
        src = _tree("""
Datum
A_wrapper(PG_FUNCTION_ARGS)
{
  return B_wrapper(fcinfo);
}

Datum
B_wrapper(PG_FUNCTION_ARGS)
{
  return A_wrapper(fcinfo);
}
""")
        self.assertEqual({}, extract_null_results(src))


class ArgIsNullTests(unittest.TestCase):
    def test_an_argument_null_guard_is_not_an_absence_guard(self):
        # PG_ARGISNULL says the CALLER passed nothing, which is already carried
        # by shape.nullable and says nothing about whether an answer exists.
        src = _tree("""
Datum
Temporal_tcount_transfn(PG_FUNCTION_ARGS)
{
  if (PG_ARGISNULL(0))
    PG_RETURN_NULL();
  PG_RETURN_POINTER(result);
}
""")
        self.assertEqual({}, extract_null_results(src))


class AttachTests(unittest.TestCase):
    def test_the_field_rides_the_existing_wrapper_link(self):
        src = _tree("""
Datum
Temporal_at_value(PG_FUNCTION_ARGS)
{
  if (! result)
    PG_RETURN_NULL();
  PG_RETURN_TEMPORAL_P(result);
}
""")
        idl = {"functions": [
            {"name": "temporal_at_value", "mdbC": "Temporal_at_value"},
            {"name": "temporal_out", "mdbC": "Temporal_out"},
            {"name": "unwrapped"},
        ]}
        idl, n = attach_null_result(idl, src)
        self.assertEqual(1, n)
        self.assertEqual("if (! result)",
                         idl["functions"][0]["shape"]["nullableResult"])
        # A wrapper with no guard, and a function with no wrapper at all, are
        # left without the field rather than given a false one.
        self.assertNotIn("shape", idl["functions"][1])
        self.assertNotIn("shape", idl["functions"][2])


if __name__ == "__main__":
    unittest.main()
