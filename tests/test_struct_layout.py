"""Regression tests for FFI-accurate struct layouts in the IDL.

The amalgamation parse must resolve struct field types to their real C types
(``uint8`` / ``bool`` / ``Datum`` — not the ``int`` the preprocessor degrades
them to when ``<stdbool.h>`` is unreachable) and must compute real byte offsets
(not ``-1``). Both depend on ``parser.parser._clang_extra_args`` putting clang's
builtin resource dir (``stdbool.h`` / ``stddef.h`` / ``stdint.h``) on the parse
path. If that regresses, every field collapses to ``int`` with ``offset_bits ==
-1`` and the FFI bindings (``#[repr(C)]`` structs, cgo/cffi field access) lose
their layout — so these assertions fail loudly.

Field types are also normalised to the catalog's ``bool`` spelling (clang spells
the underlying ``_Bool`` keyword) and offsets are in bits.

The IDL is generated, not committed; run ``python run.py`` first.
"""
import json
import unittest
from pathlib import Path

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"


class StructLayoutTests(unittest.TestCase):
    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        idl = json.loads(IDL.read_text())
        self.by_name = {s["name"]: s for s in idl["structs"]}

    def _fields(self, name):
        self.assertIn(name, self.by_name, f"{name} missing from IDL structs")
        return {f["name"]: f for f in self.by_name[name]["fields"]}

    def test_span_layout_resolved(self):
        # Pre-fix every field was ``int`` at offset ``-1``.
        f = self._fields("Span")
        self.assertEqual((f["spantype"]["cType"], f["spantype"]["offset_bits"]),
                         ("uint8", 0))
        self.assertEqual((f["basetype"]["cType"], f["basetype"]["offset_bits"]),
                         ("uint8", 8))
        self.assertEqual(f["lower_inc"]["cType"], "bool")
        self.assertEqual(f["upper_inc"]["cType"], "bool")
        self.assertEqual((f["lower"]["cType"], f["lower"]["offset_bits"]),
                         ("Datum", 64))
        self.assertEqual((f["upper"]["cType"], f["upper"]["offset_bits"]),
                         ("Datum", 128))

    def test_stbox_layout_resolved(self):
        f = self._fields("STBox")
        self.assertEqual(f["period"]["cType"], "Span")
        self.assertEqual(f["period"]["offset_bits"], 0)
        self.assertEqual((f["xmin"]["cType"], f["xmin"]["offset_bits"]),
                         ("double", 192))

    def test_tinstant_layout_resolved(self):
        f = self._fields("TInstant")
        self.assertEqual((f["temptype"]["cType"], f["temptype"]["offset_bits"]),
                         ("uint8", 32))
        self.assertEqual((f["t"]["cType"], f["t"]["offset_bits"]),
                         ("TimestampTz", 64))
        self.assertEqual((f["value"]["cType"], f["value"]["offset_bits"]),
                         ("Datum", 128))

    def test_no_bool_keyword_left_as_underscore_Bool(self):
        # Clang spells the keyword ``_Bool``; the catalog normalises to ``bool``.
        for s in self.by_name.values():
            for fld in s["fields"]:
                self.assertNotIn("_Bool", fld["cType"],
                                 f"{s['name']}.{fld['name']} not normalised")

    def test_core_structs_have_real_offsets(self):
        # No CORE-family struct field may keep the degraded ``-1`` offset.
        for s in self.by_name.values():
            if s.get("family") != "CORE":
                continue
            for fld in s["fields"]:
                self.assertGreaterEqual(
                    fld["offset_bits"], 0,
                    f"{s['name']}.{fld['name']} has unresolved offset")


if __name__ == "__main__":
    unittest.main()
