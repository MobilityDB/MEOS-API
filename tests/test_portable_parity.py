"""Unit tests for portable_parity.py.  python3 tests/test_portable_parity.py

Also the CI gate: when an enriched catalog with `portableAliases` is
present, every bare name must be either backed or explicitly flagged —
never silently dropped.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.portable import attach_portable_aliases
from tools.portable_parity import build_parity

MAP = ROOT / "meta" / "portable-aliases.json"
_CATALOG = ROOT / "output" / "meos-idl.json"


def _catalog(fn_names):
    idl = attach_portable_aliases(
        {"functions": [{"name": n} for n in fn_names]}, MAP)
    return idl


class ParityLogicTests(unittest.TestCase):
    def test_backed_vs_needs_explicit(self):
        cat = _catalog([
            "overlaps_span_span", "overlaps_tbox_tbox",   # backs `overlaps`
            "teq_temporal_temporal",                       # backs `teq`
            "same",                                        # exact-name back
            "nad_tfloat_tfloat",                           # explicit backing
        ])                                                 # of nearestApproach…
        r = build_parity(cat)
        self.assertEqual(r["total"], 29)
        self.assertEqual(r["byBareName"]["overlaps"]["status"], "backed")
        self.assertEqual(r["byBareName"]["overlaps"]["via"], "prefix")
        self.assertEqual(r["byBareName"]["overlaps"]["backedBy"], 2)
        self.assertEqual(r["byBareName"]["same"]["status"], "backed")
        # different C prefix -> resolved via the *verified* explicit map,
        # not a fake verdict and not a false gap
        nad = r["byBareName"]["nearestApproachDistance"]
        self.assertEqual(nad["status"], "backed")
        self.assertEqual(nad["via"], "explicit")
        self.assertNotIn("nearestApproachDistance", r["unbacked"])
        self.assertEqual(r["byBareName"]["overlaps"]["family"], "topology")
        self.assertEqual(r["byBareName"]["teq"]["operator"], "#=")

    def test_every_bare_name_classified(self):
        r = build_parity(_catalog([]))            # nothing backs anything
        self.assertEqual(r["total"], 29)
        self.assertEqual(r["backed"], 0)
        self.assertEqual(len(r["unbacked"]), 29)  # all flagged, 0 dropped
        self.assertTrue(all(v["status"] in ("backed",
                                            "needs-explicit-backing")
                            for v in r["byBareName"].values()))

    def test_requires_portable_aliases(self):
        with self.assertRaises(ValueError):
            build_parity({"functions": []})


@unittest.skipUnless(_CATALOG.exists(), "run `python run.py` first")
class LiveParityGate(unittest.TestCase):
    def test_no_bare_name_silently_dropped(self):
        cat = json.loads(_CATALOG.read_text())
        if "portableAliases" not in cat:
            self.skipTest("catalog has no portableAliases")
        r = build_parity(cat)
        self.assertEqual(
            r["backed"] + r["needsExplicitBacking"], r["total"])
        self.assertEqual(r["total"], 29)


if __name__ == "__main__":
    unittest.main(verbosity=2)
