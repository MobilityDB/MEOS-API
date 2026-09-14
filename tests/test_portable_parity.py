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


def _catalog(fns):
    """A catalog of the given functions: a name, or a function record."""
    idl = attach_portable_aliases(
        {"functions": [f if isinstance(f, dict) else {"name": f}
                       for f in fns]}, MAP)
    return idl


class ParityLogicTests(unittest.TestCase):
    def test_backed_vs_needs_explicit(self):
        cat = _catalog([
            "overlaps_span_span", "overlaps_tbox_tbox",   # backs `overlaps`
            {"name": "teq_temporal_temporal",              # backs `tEqual`
             "sqlfn": "tEqual"},                           # by its SQL name
            {"name": "ever_eq_temporal_temporal"},         # no SQL name: backs
                                                           # nothing
            "same",                                        # exact-name back
            "nad_tfloat_tfloat",                           # explicit backing
                                                           # of nearestApproach…
            "left_set_set", "left_tspatial_tspatial",      # back `<<`
            "overleft_stbox_stbox",                        # backs `&<`, not `<<`
        ])
        r = build_parity(cat)
        self.assertEqual(r["total"], 41)
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
        self.assertEqual(r["byBareName"]["tEqual"]["operator"], "#=")
        # a C family whose prefix differs is backed by the functions carrying
        # the bare name as their @sqlfn, and only by those
        teq = r["byBareName"]["tEqual"]
        self.assertEqual(teq["status"], "backed")
        self.assertEqual(teq["via"], "sqlfn")
        self.assertEqual(teq["sample"], ["teq_temporal_temporal"])
        self.assertIn("eEqual", r["unbacked"])
        # a position operator is backed by the MEOS family its position prefixes
        left = r["byPosition"]["<<"]
        self.assertEqual(left["status"], "backed")
        self.assertEqual(left["position"], "left")
        self.assertEqual(left["family"], "spaceX")
        self.assertEqual(left["backedBy"], 2)
        self.assertEqual(r["byPosition"]["&<"]["backedBy"], 1)
        self.assertIn("<<#", r["unbackedPositions"])
        self.assertNotIn("<<", r["unbackedPositions"])
        self.assertNotIn("left", r["byBareName"])

    def test_position_sql_names_reported(self):
        # With the catalog's positionNames derived, each position operator
        # reports its SQL names by class next to its backing.
        cat = _catalog(["left_stbox_stbox"])
        cat["portableAliases"]["positionNames"] = {
            "<<": {"set": "setLeft", "stbox": "stboxLeft"}}
        r = build_parity(cat)
        self.assertEqual(r["byPosition"]["<<"]["sqlNames"],
                         {"set": "setLeft", "stbox": "stboxLeft"})
        self.assertEqual(r["byPosition"][">>"]["sqlNames"], {})

    def test_every_bare_name_classified(self):
        r = build_parity(_catalog([]))            # nothing backs anything
        self.assertEqual(r["total"], 41)
        self.assertEqual(r["backed"], 0)
        self.assertEqual(r["needsExplicitBacking"], 41)
        self.assertEqual(len(r["unbacked"]), 25)  # all flagged, 0 dropped
        self.assertEqual(len(r["unbackedPositions"]), 16)
        self.assertTrue(all(v["status"] in ("backed",
                                            "needs-explicit-backing")
                            for v in list(r["byBareName"].values())
                            + list(r["byPosition"].values())))

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
        self.assertEqual(r["total"], 41)
        # every operator of the contract is backed in the catalog
        self.assertEqual(r["unbacked"] + r["unbackedPositions"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
