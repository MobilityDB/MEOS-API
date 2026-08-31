"""Unit tests for the vendored-declaration marker.

Runs without libclang or pytest:  python3 tests/test_vendored.py

A hermetic fixture exercises the classification; a source check asserts it against
the installed headers when they are available (skipped, never fabricated, when not).
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.extractors import _is_vendored, _vendored_cache

_OWN = """/*****************************************************************************
 *
 * This MobilityDB code is provided under The PostgreSQL License.
 * Copyright (c) 2016-2026, Universite libre de Bruxelles and MobilityDB
 * contributors
 *
 *****************************************************************************/
extern int meos_thing(void);
"""

# The wording drifts across first-party headers (meos_internal.h reads "code seq
# provided"), so the marker must not depend on the rest of the sentence.
_OWN_VARIANT = """/*****************************************************************************
 *
 * This MobilityDB code seq provided under The PostgreSQL License.
 * Copyright (c) 2016-2026, Universite libre de Bruxelles and MobilityDB
 *
 *****************************************************************************/
extern int meos_other_thing(void);
"""

_FOREIGN = """/***********************************************************************
 * pc_api.h
 *
 *  PgSQL Pointcloud is free and open source software provided
 *  by the Government of Canada
 *  Copyright (c) 2013 Natural Resources Canada
 *
 ***********************************************************************/
extern int pc_patch_sort(void);
"""


class VendoredMarkerTest(unittest.TestCase):

    def setUp(self):
        _vendored_cache.clear()

    def _write(self, d, name, text):
        p = Path(d) / name
        p.write_text(text)
        return str(p)

    def test_first_party_header_is_not_vendored(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(_is_vendored(self._write(d, "meos_geo.h", _OWN)))

    def test_first_party_wording_variant_is_not_vendored(self):
        # A header whose licence block says "code seq provided" is still MobilityDB's;
        # classifying it as vendored would drop 611 of its declarations from the surface
        # a consumer holds to the export invariant.
        with tempfile.TemporaryDirectory() as d:
            self.assertFalse(_is_vendored(self._write(d, "meos_internal.h", _OWN_VARIANT)))

    def test_foreign_header_is_vendored(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertTrue(_is_vendored(self._write(d, "pc_api.h", _FOREIGN)))

    def test_unreadable_header_is_not_vendored(self):
        # An unreadable path must not silently mark a first-party header vendored and
        # excuse it from the export invariant; the honest default is first-party.
        self.assertFalse(_is_vendored("/no/such/header.h"))


class VendoredSourceTest(unittest.TestCase):

    def test_installed_headers_split_by_provenance(self):
        inc = os.environ.get("MEOS_INCLUDE_DIR")
        if not inc or not Path(inc, "meos.h").exists():
            self.skipTest("installed MEOS headers not available")
        _vendored_cache.clear()
        self.assertFalse(_is_vendored(str(Path(inc, "meos.h"))))
        self.assertFalse(_is_vendored(str(Path(inc, "meos_internal.h"))))
        for name in ("pointcloud/pc_api.h", "pointcloud/hashtable.h"):
            path = Path(inc, name)
            if path.exists():
                self.assertTrue(_is_vendored(str(path)), name)


if __name__ == "__main__":
    unittest.main()
