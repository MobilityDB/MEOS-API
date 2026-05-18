"""Unit tests for report.py.  python3 tests/test_report.py"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report import build_report


def f(name, api, exposable, reason=None, ret="int", params=None):
    return {"name": name, "api": api,
            "returnType": {"canonical": ret}, "params": params or [],
            "network": {"exposable": exposable, "reason": reason}}


CATALOG = {"functions": [
    f("temporal_eq", "public", True),
    f("tpoint_speed", "public", True),
    f("tsequence_make", "public", False, "array-or-out-param:instants"),
    f("geo_collect", "public", False,
      "array-or-out-param:a; no-decoder:GBOX"),
    f("temporal_tagg", "public", False, "no-decoder:SkipList"),
    f("meos_initialize", "public", False, "lifecycle"),
    f("some_internal", "internal", False, "internal; no-decoder:Datum"),
]}


class ReportTests(unittest.TestCase):
    def setUp(self):
        self.r = build_report(CATALOG)

    def test_counts(self):
        self.assertEqual(self.r["publicTotal"], 6)      # excludes internal
        self.assertEqual(self.r["exposable"], 2)
        self.assertEqual(self.r["gap"], 4)
        self.assertEqual(self.r["internalExcluded"], 1)
        self.assertEqual(self.r["coveragePct"], round(2 * 100 / 6, 1))

    def test_grouping_by_reason_tagset(self):
        br = self.r["byReason"]
        # detail is stripped; multi-tag reasons collapse to the tag set
        self.assertIn("array-or-out-param", br)
        self.assertEqual(br["array-or-out-param"], ["tsequence_make"])
        self.assertEqual(br["array-or-out-param; no-decoder"], ["geo_collect"])
        self.assertEqual(br["no-decoder"], ["temporal_tagg"])
        self.assertEqual(br["lifecycle"], ["meos_initialize"])
        # internal never appears as a public gap
        self.assertNotIn("some_internal",
                         [n for v in br.values() for n in v])

    def test_byreason_sorted_largest_first(self):
        sizes = [len(v) for v in self.r["byReason"].values()]
        self.assertEqual(sizes, sorted(sizes, reverse=True))

    def test_worklist_is_actionable(self):
        wl = {w["name"]: w for w in self.r["worklist"]}
        self.assertEqual(len(wl), self.r["gap"])         # one entry per gap
        self.assertNotIn("some_internal", wl)            # internal excluded
        # each gap gets a class + a concrete upstream suggestion
        self.assertEqual(wl["meos_initialize"]["class"], "plumbing")
        self.assertEqual(wl["temporal_tagg"]["class"], "stateful")
        gc = wl["geo_collect"]                            # no-decoder:GBOX
        self.assertEqual(gc["class"], "no-codec")
        self.assertIn("gbox_in", gc["suggest"])
        self.assertTrue(all(w["suggest"] for w in self.r["worklist"]))
        self.assertEqual(sum(self.r["byClass"].values()), self.r["gap"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
