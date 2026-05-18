"""Unit tests for parser/header_types.py.

Runs without libclang or pytest:  python3 tests/test_header_types.py
"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser.header_types import scan_headers, reconcile

_HEADER = """
/* a comment
   spanning lines */
extern TimestampTz add_timestamptz_interval(TimestampTz t,
                                             const Interval *interv);
extern bool contains_set_text(const Set *s, text *t);   // trailing comment
extern bool bigintset_value_n(const Set *s, int n, int64 *result);
extern Temporal *temporal_copy(const Temporal *temp);
extern void meos_initialize(void);
"""


class ScanTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        (Path(self.d.name) / "meos.h").write_text(_HEADER)
        self.h = scan_headers(Path(self.d.name))

    def tearDown(self):
        self.d.cleanup()

    def test_signatures_recovered(self):
        self.assertEqual(self.h["add_timestamptz_interval"]["params"],
                         ["TimestampTz", "const Interval *"])
        self.assertEqual(self.h["contains_set_text"]["params"],
                         ["const Set *", "text *"])
        self.assertEqual(self.h["meos_initialize"]["params"], [])
        self.assertEqual(self.h["temporal_copy"]["ret"], "Temporal *")


class ReconcileTests(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        (Path(self.d.name) / "meos.h").write_text(_HEADER)

    def tearDown(self):
        self.d.cleanup()

    def idl(self):
        # Mimic the libclang output *after* the stub erased the names.
        return {
            "enums": [],
            "functions": [
                {"name": "add_timestamptz_interval",
                 "returnType": {"c": "int", "canonical": "int"},
                 "params": [{"name": "t", "cType": "int", "canonical": "int"},
                            {"name": "interv", "cType": "const int *",
                             "canonical": "const int *"}]},
                {"name": "contains_set_text",
                 "returnType": {"c": "int", "canonical": "int"},
                 "params": [{"name": "s", "cType": "const struct Set *",
                             "canonical": "const struct Set *"},
                            {"name": "t", "cType": "int *",
                             "canonical": "int *"}]},
                {"name": "bigintset_value_n",
                 "returnType": {"c": "int", "canonical": "int"},
                 "params": [{"name": "s", "cType": "const struct Set *",
                             "canonical": "const struct Set *"},
                            {"name": "n", "cType": "int", "canonical": "int"},
                            {"name": "result", "cType": "int *",
                             "canonical": "int *"}]},
            ],
        }

    def test_opaque_pointers_restored_scalars_left_alone(self):
        idl = reconcile(self.idl(), Path(self.d.name))
        f = {x["name"]: x for x in idl["functions"]}
        # const Interval * restored from the header source
        self.assertEqual(f["add_timestamptz_interval"]["params"][1]["canonical"],
                         "const Interval *")
        # TimestampTz return stays the resolved scalar (int) — not restored
        self.assertEqual(f["add_timestamptz_interval"]["returnType"]["canonical"],
                         "int")
        # text * restored
        self.assertEqual(f["contains_set_text"]["params"][1]["canonical"],
                         "text *")
        # genuine int* out-param (header also says int64*) is a scalar
        # pointer -> left exactly as libclang produced it
        self.assertEqual(f["bigintset_value_n"]["params"][2]["canonical"],
                         "int *")


if __name__ == "__main__":
    unittest.main(verbosity=2)
