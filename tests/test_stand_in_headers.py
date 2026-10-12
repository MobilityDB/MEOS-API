"""Regression tests for the headers the catalog parse reads.

MobilityDB's header tree holds `geos-none/geos_c.h`, a stand-in its build reads only where
GEOS is absent and installs then in place of GEOS's own `geos_c.h`. Parsed beside the
library's header, which `meos_internal_geo.h` includes, every enum it declares is a
redefinition clang drops: the catalog carried `enum (unnamed at .../geos_c.h:84:6)` with no
value, and a binding writing one Java enum per catalog enum (JMEOS `FunctionsGenerator`)
failed opening a file of that name. #meos_headers of parser/parser.py leaves the stand-in
out, as an install of MEOS does, so the catalog states neither its enums nor its functions.

The catalog half mirrors #StructLayoutTests of tests/test_struct_layout.py: it reads the
generated IDL and skips where there is none; run ``python run.py`` first.
"""
import json
import re
import tempfile
import unittest
from pathlib import Path

from parser.parser import meos_headers, build_entry_point

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"
C_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class StandInHeaderTests(unittest.TestCase):
    def test_the_stand_in_directory_is_not_read(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            for rel in ("meos.h", "geo/postgis_funcs.h", "geos-none/geos_c.h"):
                (root / rel).parent.mkdir(parents=True, exist_ok=True)
                (root / rel).write_text("")
            read = [h.relative_to(root).as_posix() for h in meos_headers(root)]
            self.assertEqual(read, ["geo/postgis_funcs.h", "meos.h"])
            self.assertNotIn("geos-none", build_entry_point(root))

    def test_a_header_named_like_the_stand_in_elsewhere_is_read(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "geo").mkdir()
            (root / "geo" / "geos-none.h").write_text("")
            self.assertEqual([h.name for h in meos_headers(root)], ["geos-none.h"])


class CatalogEnumNameTests(unittest.TestCase):
    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        self.idl = json.loads(IDL.read_text())

    def test_every_enum_is_named_and_carries_values(self):
        for e in self.idl["enums"]:
            self.assertRegex(e["name"], C_IDENTIFIER, e["name"])
            self.assertTrue(e["values"], e["name"])

    def test_no_declaration_comes_from_the_stand_in(self):
        for section in ("functions", "structs", "enums"):
            files = {x.get("file") for x in self.idl[section]}
            self.assertNotIn("geos_c.h", files, section)


if __name__ == "__main__":
    unittest.main()
