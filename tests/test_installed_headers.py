"""Regression tests for the header set the catalog states.

MEOS installs the per-type headers pg_*.h of its vendored PostgreSQL base types and not the
internal umbrella pgtypes.h the library is built against. That umbrella declares the
`pg_`-prefixed twins (`pg_date_in` beside the public `date_in`) through which the PostgreSQL
extension build reaches the names PostgreSQL also owns, and helpers only pgtypes and MEOS call.
#_public_pgtypes_headers of run.py reads the pg_*.h set alone, so no catalog entry comes from
pgtypes.h and every binding projects the clean names.

The class mirrors #CatalogEnumNameTests of tests/test_stand_in_headers.py: it reads the
generated IDL and skips where there is none; run ``python run.py`` first.
"""
import json
import unittest
from pathlib import Path

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"


class InstalledHeaderTests(unittest.TestCase):
    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        self.idl = json.loads(IDL.read_text())

    def test_no_entry_comes_from_the_internal_umbrella(self):
        for section in ("functions", "structs", "enums", "macros"):
            files = {x.get("file") for x in self.idl.get(section, [])}
            self.assertNotIn("pgtypes.h", files, section)

    def test_the_base_types_keep_their_clean_names(self):
        names = {f["name"] for f in self.idl["functions"]}
        for clean in ("date_in", "timestamptz_in", "interval_cmp"):
            self.assertIn(clean, names)
            self.assertNotIn(f"pg_{clean}", names)


if __name__ == "__main__":
    unittest.main()
