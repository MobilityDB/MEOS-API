"""Regression tests for parser/nullable.py.

Nullability is read from the C Doxygen (the source of truth), the `@nullable` tag
inside a `@param` entry or the `@param ... may be NULL` note, and folded into each
function's ``shape.nullable`` for the params that actually exist on the IDL
function.

Plain unittest, no pytest dependency; writes a tiny synthetic source tree.
"""
import tempfile
import unittest
from pathlib import Path

from parser.nullable import extract_nullable, extract_param_docs, merge_nullable

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

/**
 * @brief Replace a value at a path
 * @param[in] jb JSONB value
 * @param[in] newjb New value @nullable
 * @param[in] handle_null Null value treatment, one of the four names the
 * function accepts @nullable
 * @param[in] create When true, create the missing key
 * @nullable create
 * @param[out] count Number of values @nullable
 */
#if MEOS
Jsonb *
jsonb_set_lax(const Jsonb *jb, const Jsonb *newjb, const text *handle_null,
  bool create, int *count)
{
  return NULL;
}
#endif /* MEOS */
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

    def test_reads_the_tag_inside_the_param_entry(self):
        """The @nullable tag, as #test_extracts_only_may_be_null_params reads the
        prose note."""
        nul = extract_nullable(self.tmp.name)
        # On the entry's line and on its continuation line, behind an `#if MEOS`
        # guard, and on an output a caller may pass as NULL
        self.assertEqual(nul["jsonb_set_lax"], ["newjb", "handle_null", "count"])
        # A line opening with @nullable qualifies no entry
        self.assertNotIn("create", nul["jsonb_set_lax"])
        self.assertNotIn("jb", nul["jsonb_set_lax"])

    def test_param_docs_leave_out_the_tag(self):
        """The description #test_extracts_only_may_be_null_params reads, without the
        tag."""
        docs = extract_param_docs(self.tmp.name)
        self.assertEqual(docs["jsonb_set_lax"]["newjb"], "New value")
        self.assertNotIn("@nullable", docs["jsonb_set_lax"]["handle_null"])

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
