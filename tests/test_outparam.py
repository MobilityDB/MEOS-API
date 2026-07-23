"""Unit tests for parser/outparam.py.

Runs without libclang or pytest:  python3 tests/test_outparam.py

Focuses on the two behaviours that let the catalog carry `@param[out]` from the
vendored PostgreSQL base-type layer: (1) `_FUNC` steps over the `#if MEOS` guard
that separates a base function's doxygen block from its definition, and (2) the
extractors scan the sibling `pgtypes/` tree in addition to `meos/src` +
`meos/include` (mirrors doxygroup.py / run.py).
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser.outparam import (extract_outparams, extract_param_names,
                             merge_outparams)


def _write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode())
    return p


# Plain meos/src function: return type on its own line, name right after.
PLAIN = """\
/**
 * @ingroup meos_temporal_analytics_tile
 * @brief doc
 * @param[in] temp Temporal value
 * @param[out] count Number of elements in the output array
 */
TBox *
tint_value_boxes(const Temporal *temp, int vsize, int vorigin, int *count)
{
  return NULL;
}
"""

# A base function guarded by `#if MEOS` between the doxygen close and the
# definition — the exact shape of `json_array_elements` in pgtypes/utils/jsonfuncs.c.
GUARDED = """\
/**
 * @ingroup meos_json_base_accessor
 * @brief doc
 * @param[in] js JSON value
 * @param[out] count Number of elements in the output array
 */
#if MEOS
text **
json_array_elements(const text *js, int *count)
{
  return NULL;
}
#endif /* MEOS */
"""

# A @param[out] tag on a by-VALUE parameter: the cross-check must drop it and
# report drift, never fold a non-pointer as an out-param.
BYVALUE = """\
/**
 * @ingroup meos_base
 * @brief doc
 * @param[out] n Not really an out-param
 */
int
some_fn(int n)
{
  return n;
}
"""


class TestExtract(unittest.TestCase):
    def test_plain_meos_src(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "meos/src/temporal_tile_meos.c", PLAIN)
            out = extract_outparams(Path(root) / "meos")
            self.assertEqual(out.get("tint_value_boxes"), ["count"])

    def test_pgtypes_guarded_twin(self):
        # The base function lives in the sibling pgtypes/ tree AND is behind a
        # `#if MEOS` guard — both must be handled to pick up its out-param.
        with tempfile.TemporaryDirectory() as root:
            _write(root, "pgtypes/utils/jsonfuncs.c", GUARDED)
            out = extract_outparams(Path(root) / "meos")
            self.assertEqual(out.get("json_array_elements"), ["count"])

    def test_param_names_include_pgtypes(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "pgtypes/utils/jsonfuncs.c", GUARDED)
            names = extract_param_names(Path(root) / "meos")
            self.assertEqual(names.get("json_array_elements"), {"js", "count"})

    def test_missing_pgtypes_is_skipped(self):
        # A meos-only tree (no sibling pgtypes/) must not raise.
        with tempfile.TemporaryDirectory() as root:
            _write(root, "meos/src/foo.c", PLAIN)
            out = extract_outparams(Path(root) / "meos")
            self.assertEqual(out.get("tint_value_boxes"), ["count"])


class TestMergeCrossCheck(unittest.TestCase):
    def test_byvalue_tag_is_dropped_as_drift(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "pgtypes/foo.c", BYVALUE)
            idl = {"functions": [
                {"name": "some_fn", "params": [{"name": "n", "canonical": "int"}]}]}
            idl, n, drift = merge_outparams(idl, Path(root) / "meos")
            self.assertEqual(n, 0)
            self.assertNotIn("shape", idl["functions"][0])
            self.assertTrue(any(d[0] == "some_fn" for d in drift))

    def test_nonconst_pointer_is_folded(self):
        with tempfile.TemporaryDirectory() as root:
            _write(root, "pgtypes/utils/jsonfuncs.c", GUARDED)
            idl = {"functions": [{"name": "json_array_elements", "params": [
                {"name": "js", "canonical": "const text *"},
                {"name": "count", "canonical": "int *"}]}]}
            idl, n, drift = merge_outparams(idl, Path(root) / "meos")
            self.assertEqual(n, 1)
            self.assertEqual(idl["functions"][0]["shape"]["outParams"], ["count"])


if __name__ == "__main__":
    unittest.main()
