"""Coverage non-regression gate.  python3 tests/test_coverage_gate.py

Skipped unless an enriched ``output/meos-idl.json`` is present (so CI
without libclang still passes). When present, it asserts public coverage
does not regress below the established floor and that the worklist stays
consistent — a heuristic change that silently drops coverage fails here.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from report import build_report

_CATALOG = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"
_FLOOR = 90.0  # public %, ratchet up as upstream uniformization lands


@unittest.skipUnless(_CATALOG.exists(), "run `python run.py` first")
class CoverageGateTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.r = build_report(json.loads(_CATALOG.read_text()))

    def test_public_coverage_not_regressed(self):
        self.assertGreaterEqual(
            self.r["coveragePct"], _FLOOR,
            f"public coverage {self.r['coveragePct']}% < floor {_FLOOR}% "
            "— a heuristic change dropped coverage")

    def test_worklist_consistent(self):
        # one actionable entry per gap; classes partition the gap
        self.assertEqual(len(self.r["worklist"]), self.r["gap"])
        self.assertEqual(sum(self.r["byClass"].values()), self.r["gap"])
        self.assertTrue(all(w["suggest"] and w["class"]
                            for w in self.r["worklist"]))

    def test_no_internal_or_silent_gap(self):
        # internal never counted as public; every gap has a reason
        cat = json.loads(_CATALOG.read_text())
        for f in cat["functions"]:
            if f.get("api") == "internal":
                continue
            if not f["network"]["exposable"]:
                self.assertTrue(f["network"]["reason"],
                                f"{f['name']}: gap without a reason")


if __name__ == "__main__":
    unittest.main(verbosity=2)
