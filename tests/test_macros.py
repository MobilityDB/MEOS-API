"""Regression tests for public ``#define`` macro extraction into the IDL.

A handful of MEOS public constants are object-like ``#define`` macros, not
``enum`` values — the WKB / WKT output-variant flags and the ``MEOS_FLAG_*``
bit masks. An ``enum`` walk never sees them, so the parser records the
preprocessor definitions separately (``options`` enables the detailed
preprocessing record). A binding's FFI needs them: ``meos-rs`` selects a WKB
variant with ``WKB_EXTENDED`` / ``WKB_NDR`` / ``WKB_XDR``.

The IDL is generated, not committed; run ``python run.py`` first.
"""
import json
import unittest
from pathlib import Path

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"


class MacroTests(unittest.TestCase):
    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        idl = json.loads(IDL.read_text())
        self.macros = idl.get("macros", [])
        self.by_name = {m["name"]: m for m in self.macros}

    def test_macros_present(self):
        self.assertTrue(self.macros, "no macros extracted")

    def test_wkb_variant_flags(self):
        # The variant flags meos-rs uses to select a WKB encoding.
        for name, value in (("WKB_EXTENDED", 4), ("WKB_NDR", 8), ("WKB_XDR", 16)):
            self.assertIn(name, self.by_name, f"{name} not extracted")
            self.assertEqual(self.by_name[name]["value"], value)

    def test_values_are_integers(self):
        for m in self.macros:
            self.assertIsInstance(m["value"], int, f"{m['name']} value not int")

    def test_function_like_macros_excluded(self):
        # Object-like integer macros only; a function-like macro (e.g. a
        # ``#define FOO(x) ...``) must never leak in as a constant.
        for m in self.macros:
            self.assertNotIn("(", m["name"])

    def test_each_macro_has_family_and_file(self):
        for m in self.macros:
            self.assertTrue(m.get("family"))
            self.assertTrue(m.get("file", "").endswith(".h"))


if __name__ == "__main__":
    unittest.main()
