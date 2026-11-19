"""A record declared without a tag is named by its typedef, in its slots and as a structure.

MEOS declares several structures without a tag (``typedef struct {...} GeomVal;``,
``Pixel``, ``ways_record``, ``SERIALIZED_PATCH``). C names such a record by its typedef
alone: a slot spelling it ``struct GeomVal *`` names a tag no header declares, which a
binding spelling the type as C does (GoMEOS's cgo ``C.struct_GeomVal``) cannot compile.
And the structure the catalog lays out is the one the typedef defines, never a typedef
whose type only contains the record: ``ways_fetch_fn``, a function pointer taking a
``ways_record *``, is a callback, not the layout of ``ways_record``.

The catalog half mirrors #StructLayoutTests of tests/test_struct_layout.py: it reads the
generated IDL and skips where there is none; run ``python run.py`` first. The spelling
half runs #_canonical_spelling of parser/extractors.py on the spellings libclang gives.
"""
import json
import re
import unittest
from pathlib import Path

from parser.extractors import _canonical_spelling

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"


class _Type:
    """The two spellings #_canonical_spelling reads of a libclang type."""

    def __init__(self, spelling, canonical):
        self.spelling = spelling
        self._canonical = canonical

    def get_canonical(self):
        return _Type(self._canonical, self._canonical)


class AnonymousRecordSpellingTests(unittest.TestCase):
    def test_an_untagged_record_is_spelled_by_its_typedef(self):
        at = "(unnamed at /x/meos/include/meos_raster.h:130:9)"
        cases = {
            ("GeomVal *", f"struct {at} *"): "GeomVal *",
            ("const GeomVal *", f"const struct {at} *"): "const GeomVal *",
            ("GeomVal", f"struct {at}"): "GeomVal",
            ("GeomVal *", "struct (unnamed struct at /x/meos_raster.h:130:9) *"): "GeomVal *",
            ("Span *", "struct Span *"): "struct Span *",
        }
        for (spelling, canonical), expected in cases.items():
            self.assertEqual(_canonical_spelling(_Type(spelling, canonical)), expected,
                             canonical)


class AnonymousRecordCatalogTests(unittest.TestCase):
    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        self.idl = json.loads(IDL.read_text())

    def test_no_slot_names_an_unnamed_record(self):
        found = set()

        def visit(o):
            if isinstance(o, dict):
                if isinstance(o.get("canonical"), str) and "unnamed" in o["canonical"]:
                    found.add(o["canonical"])
                for v in o.values():
                    visit(v)
            elif isinstance(o, list):
                for v in o:
                    visit(v)

        visit(self.idl)
        self.assertEqual(sorted(found), [])

    def test_the_raster_records_are_their_typedefs(self):
        by_name = {f["name"]: f for f in self.idl["functions"]}
        if "geomval_arr_free" not in by_name:
            self.skipTest("the catalog was built without the raster family")
        self.assertEqual(by_name["geomval_arr_free"]["params"][0]["canonical"], "GeomVal *")
        self.assertEqual(by_name["pixel_arr_free"]["params"][0]["canonical"], "Pixel *")

    def test_a_structure_is_never_a_callback(self):
        structs = {s["name"] for s in self.idl["structs"]}
        callbacks = {c["name"] for c in self.idl.get("callbacks", [])}
        self.assertEqual(sorted(structs & callbacks), [])
        if any(f["name"] == "ways_cache_lookup" for f in self.idl["functions"]):
            self.assertIn("ways_record", structs)
            self.assertNotIn("ways_fetch_fn", structs)
            self.assertFalse(any(re.search(r"\bstruct\s+ways_record\b", p["canonical"])
                                 for f in self.idl["functions"]
                                 if f["name"] == "ways_cache_lookup"
                                 for p in f["params"]))


if __name__ == "__main__":
    unittest.main()
