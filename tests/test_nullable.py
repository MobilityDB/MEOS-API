"""Regression tests for parser/nullable.py.

Nullability is read from the C Doxygen `@param ... may be NULL` notes (the
source of truth) and folded into each function's ``shape.nullable`` for the
params that actually exist on the IDL function.

Plain unittest, no pytest dependency; writes a tiny synthetic source tree.
"""
import tempfile
import unittest
from pathlib import Path

from parser.nullable import extract_nullable, merge_nullable

SAMPLE = '''
/**
 * @ingroup meos_temporal_inout
 * @brief Return the MF-JSON representation
 * @param[in] temp Temporal value
 * @param[in] srs Spatial reference system, may be `NULL`
 */
char *
temporal_as_mfjson(const Temporal *temp, char *srs)
{
  return NULL;
}

/**
 * @brief Append an instant
 * @param[in] inst Instant
 * @param[in] maxt Maximum time interval, may be `NULL`
 * @param[in] interp Interpolation
 */
Temporal *
temporal_append_tinstant(const TInstant *inst, const Interval *maxt, int interp)
{
  return NULL;
}
'''


class NullableTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        src = Path(self.tmp.name) / "src"
        src.mkdir()
        (src / "sample.c").write_text(SAMPLE)
        (Path(self.tmp.name) / "include").mkdir()

    def tearDown(self):
        self.tmp.cleanup()

    def test_extracts_only_may_be_null_params(self):
        nul = extract_nullable(self.tmp.name)
        self.assertEqual(nul["temporal_as_mfjson"], ["srs"])
        self.assertEqual(nul["temporal_append_tinstant"], ["maxt"])
        # `temp`, `inst`, `interp` carry no NULL note -> not nullable
        self.assertNotIn("temp", nul["temporal_as_mfjson"])

    def test_merge_only_existing_params(self):
        idl = {"functions": [
            {"name": "temporal_as_mfjson",
             "params": [{"name": "temp"}, {"name": "srs"}]},
            # function whose nullable param is NOT in its IDL signature
            {"name": "temporal_append_tinstant", "params": [{"name": "inst"}]},
        ]}
        idl, n = merge_nullable(idl, self.tmp.name)
        self.assertEqual(idl["functions"][0]["shape"]["nullable"], ["srs"])
        # maxt absent from the IDL params -> not added
        self.assertNotIn("shape", idl["functions"][1])
        self.assertEqual(n, 1)


if __name__ == "__main__":
    unittest.main()
