"""Tests for reading the optional families out of MobilityDB's ``ALL`` list.

``parser/families.py`` is the ecosystem's only reader of MobilityDB's
``if(ALL) foreach(_family ...)`` block. These cover the reader itself against
written-out CMake text (no MobilityDB checkout needed), and — when a checkout is
reachable — that the real list agrees with the catalog the pipeline published.

Plain unittest, no pytest dependency.
"""
import json
import os
import tempfile
import unittest
from pathlib import Path

import parser.families as families
from parser.families import (FamiliesUnavailable, all_families,
                             find_mobilitydb_root, header_family,
                             read_all_families, subdir_family, use_headers_dir)

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"

# The shape of MobilityDB's own block, with a decoy `foreach` before it: the
# reader must key on `if(ALL)` and not on the first `foreach` in the file.
_CMAKE = """\
foreach(_lang ${PROJECT_SUPPORTED_LANGUAGES})
  set(${_lang} ON)
endforeach()

option(ALL "Set ON|OFF (default=OFF) to include all the optional families" OFF)
option(CBUFFER "circular buffers" OFF)

if(ALL)
  foreach(_family CBUFFER POSE RGEO POSECHAIN S2CELL)
    set(${_family} ON CACHE BOOL "Enabled by ALL=ON" FORCE)
  endforeach()
endif()
"""


def _write(text: str) -> Path:
    d = Path(tempfile.mkdtemp())
    (d / "CMakeLists.txt").write_text(text)
    return d / "CMakeLists.txt"


class ReadAllFamiliesTests(unittest.TestCase):
    def test_reads_the_foreach_of_the_if_all_block(self):
        self.assertEqual(read_all_families(_write(_CMAKE)),
                         ("CBUFFER", "POSE", "POSECHAIN", "RGEO", "S2CELL"))

    def test_a_family_added_to_the_block_is_read_with_no_code_change(self):
        # The property the whole module exists for.
        grown = _CMAKE.replace("S2CELL)", "S2CELL NEWFAMILY)")
        self.assertIn("NEWFAMILY", read_all_families(_write(grown)))

    def test_the_option_declarations_alone_are_not_the_list(self):
        # `option(...)` also names families, but `ALL` and `GEOS` are options
        # that are not families, so the `foreach` is the list — not the options.
        read = read_all_families(_write(_CMAKE))
        self.assertNotIn("ALL", read)

    def test_a_file_without_the_block_raises_rather_than_defaulting(self):
        # There is no honest default: a short list is invisible in the output.
        with self.assertRaises(FamiliesUnavailable):
            read_all_families(_write("project(mobilitydb)\n"))


class RootResolutionTests(unittest.TestCase):
    """A caller that names only a header tree still locates the checkout.

    The OpenAPI regeneration clones MobilityDB itself and passes
    ``<root>/meos/include`` as the sole argument, exporting no source-root
    variable; the root is two levels above those headers.
    """

    def setUp(self):
        self.addCleanup(use_headers_dir, families._headers_dir)
        for var in ("MDB_SRC_ROOT", "MOBILITYDB_SRC"):
            self.addCleanup(self._restore, var, os.environ.get(var))
            os.environ.pop(var, None)

    @staticmethod
    def _restore(var, val):
        if val is None:
            os.environ.pop(var, None)
        else:
            os.environ[var] = val

    def test_the_root_is_found_two_levels_above_the_headers(self):
        root = Path(tempfile.mkdtemp())
        (root / "CMakeLists.txt").write_text(_CMAKE)
        headers = root / "meos" / "include"
        headers.mkdir(parents=True)
        use_headers_dir(headers)
        self.assertEqual(find_mobilitydb_root(), root)
        self.assertIn("S2CELL", all_families())

    def test_headers_staged_away_from_a_checkout_do_not_answer_for_one(self):
        # An installed prefix's include dir has no checkout above it, so the
        # root two levels up holds no CMakeLists.txt and must not be claimed;
        # the resolver falls through to the next candidate instead (to a
        # `_mobilitydb` / `_mobilitydb_src` checkout where one is present).
        staged = Path(tempfile.mkdtemp()) / "prefix" / "include"
        staged.mkdir(parents=True)
        use_headers_dir(staged)
        self.assertNotEqual(find_mobilitydb_root(), staged.parent.parent)


class DerivedMapTests(unittest.TestCase):
    @unittest.skipUnless(find_mobilitydb_root(), "MobilityDB checkout not reachable")
    def test_subdir_and_header_are_spelled_from_the_token(self):
        for family in all_families():
            self.assertEqual(subdir_family()[family.lower()], family)
            self.assertEqual(header_family()[f"meos_{family.lower()}.h"], family)


class PublishedListTests(unittest.TestCase):
    @unittest.skipUnless(find_mobilitydb_root(), "MobilityDB checkout not reachable")
    def test_the_catalog_publishes_the_list_the_source_states(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        published = json.loads(IDL.read_text())["families"]
        self.assertEqual(list(all_families()), published)


if __name__ == "__main__":
    unittest.main()
