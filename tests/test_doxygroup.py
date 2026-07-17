"""Unit tests for parser/doxygroup.py.

Runs without libclang or pytest:  python3 tests/test_doxygroup.py

Focuses on `_FNDEF` robustness — the doxygen `@ingroup` block and the function
definition it labels can be separated by preprocessor guards (`#if MEOS` …
`#endif`), ordinary comments, and blank lines, especially in the vendored
`pgtypes/` base-type sources — and on the multi-root scan that lets a binding
pick up both `meos/src` and `pgtypes/`.
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser.doxygroup import _name_to_group, attach_groups


def _write(root, rel, text):
    p = Path(root) / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(text.encode())
    return p


# The plain case: return type on its own line, name right after.
PLAIN = """\
/**
 * @ingroup meos_setspan_accessor
 * @brief doc
 */
struct Set *
set_out(const struct Set *s)
{
  return NULL;
}
"""

# A `#if MEOS` guard plus a multi-line comment between the doxygen close and the
# MEOS-build twin — the exact shape of `cstring_to_text` in pgtypes/varlena.c.
GUARDED = """\
/**
 * @ingroup meos_base_text
 * @brief doc
 */
#if MEOS
/* In the extension build libpostgres exports this same symbol; use the
 * backend's copy there to keep allocation consistent with it. */
text *
cstring_to_text(const char *str)
{
  return NULL;
}
#endif
text *
pg_cstring_to_text(const char *str)
{
  return NULL;
}
"""

# A blank line between the doxygen close and the return type (common in json/).
BLANK_LINE = """\
/**
 * @ingroup meos_json_inout
 * @brief doc
 */

Temporal *
tjsonb_from_mfjson(const char *mfjson)
{
  return NULL;
}
"""

# CRLF line endings must be tolerated (jsonbset.c ships CRLF).
CRLF = (
    "/**\r\n"
    " * @ingroup meos_json_set_accessor\r\n"
    " * @brief doc\r\n"
    " */\r\n"
    "\r\n"
    "bool\r\n"
    "jsonbset_value_n(const Set *s, int n, Jsonb **result)\r\n"
    "{\r\n"
    "  return true;\r\n"
    "}\r\n"
)

# Inline return type: the type and the name share one line (terse comparison
# one-liners such as `bool pcpatch_eq(...)`). The name is the last identifier
# before `(`; without this the @ingroup mis-binds to the next own-line
# definition. This fixture also pins that correction: the `_comp` group labels
# the inline `pcpatch_eq`, and the following own-line `pcpatch_copy` keeps its
# own `_constructor` group rather than being swallowed by `_comp`.
INLINE = """\
/**
 * @ingroup meos_pointcloud_comp
 * @brief doc
 */
bool pcpatch_eq(const Pcpatch *pa1, const Pcpatch *pa2)
{ return pcpatch_cmp(pa1, pa2) == 0; }

/**
 * @ingroup meos_pointcloud_constructor
 * @brief doc
 */
Pcpatch *
pcpatch_copy(const Pcpatch *pa)
{
  return NULL;
}
"""


class TestFndefRobustness(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = self._tmp.name

    def tearDown(self):
        self._tmp.cleanup()

    def _map(self):
        return _name_to_group(self.root)

    def test_plain(self):
        _write(self.root, "meos/src/set.c", PLAIN)
        self.assertEqual(self._map().get("set_out"), "meos_setspan_accessor")

    def test_guarded_twin_and_comment(self):
        _write(self.root, "pgtypes/varlena.c", GUARDED)
        m = self._map()
        # The @ingroup labels the guarded MEOS twin, not the pg_ fallback.
        self.assertEqual(m.get("cstring_to_text"), "meos_base_text")

    def test_blank_line(self):
        _write(self.root, "meos/src/json/tjsonb.c", BLANK_LINE)
        self.assertEqual(self._map().get("tjsonb_from_mfjson"), "meos_json_inout")

    def test_crlf(self):
        _write(self.root, "meos/src/json/jsonbset.c", CRLF)
        self.assertEqual(
            self._map().get("jsonbset_value_n"), "meos_json_set_accessor")

    def test_inline_return_type(self):
        _write(self.root, "meos/src/pointcloud/pcpatch.c", INLINE)
        m = self._map()
        # The inline `bool pcpatch_eq(...)` is captured under its own group ...
        self.assertEqual(m.get("pcpatch_eq"), "meos_pointcloud_comp")
        # ... and does not steal the following own-line definition's group.
        self.assertEqual(m.get("pcpatch_copy"), "meos_pointcloud_constructor")


class TestMultiRootScan(unittest.TestCase):
    def test_scans_every_root(self):
        with tempfile.TemporaryDirectory() as a, tempfile.TemporaryDirectory() as b:
            _write(a, "set.c", PLAIN)
            _write(b, "varlena.c", GUARDED)
            m = _name_to_group(a, b)
            self.assertEqual(m.get("set_out"), "meos_setspan_accessor")
            self.assertEqual(m.get("cstring_to_text"), "meos_base_text")

    def test_missing_root_is_skipped(self):
        with tempfile.TemporaryDirectory() as a:
            _write(a, "set.c", PLAIN)
            # A non-existent second root must not raise.
            m = _name_to_group(a, "/no/such/path")
            self.assertEqual(m.get("set_out"), "meos_setspan_accessor")


class TestAttachGroups(unittest.TestCase):
    def test_attach_sets_group_field(self):
        with tempfile.TemporaryDirectory() as a:
            _write(a, "set.c", PLAIN)
            idl = {"functions": [{"name": "set_out"}, {"name": "unknown_fn"}]}
            idl, n = attach_groups(idl, a)
            self.assertEqual(n, 1)
            self.assertEqual(idl["functions"][0]["group"], "meos_setspan_accessor")
            self.assertNotIn("group", idl["functions"][1])


if __name__ == "__main__":
    unittest.main()
