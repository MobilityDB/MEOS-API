"""Unit tests + CI gate for object_model_parity.py.

    python3 tests/test_object_model_parity.py

The gate: every structural divergence from the PyMEOS oracle must be
explained by a curated `corrections` item (status `known`) — none may be
`needs-correction` and none may be silently dropped. This is the
object-model analogue of the portable-parity 0-unbacked gate.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.object_model import attach_object_model
from object_model_parity import build_parity, _parse_oracle, PYMEOS

MODEL = ROOT / "meta" / "object-model.json"
_CATALOG = ROOT / "output" / "meos-idl.json"


def _idl(names):
    return attach_object_model(
        {"functions": [{"name": n} for n in names]}, MODEL, None)


class ParityLogicTests(unittest.TestCase):
    def test_requires_object_model(self):
        with self.assertRaises(ValueError):
            build_parity({"functions": []}, None)

    def test_oracle_unavailable_is_honest(self):
        rep = build_parity(_idl(["temporal_merge"]), None)
        self.assertEqual(rep["status"], "oracle-unavailable")
        # curated corrections still carried; no fabricated parity verdict
        self.assertGreater(rep["divergences"], 0)
        self.assertTrue(all(w["status"] == "known"
                            for w in rep["worklist"]))

    def test_audited_against_fake_oracle(self):
        # PyMEOS-shaped oracle missing every spatial leaf & abstract.
        oracle = {
            "temporal": {("T_TBOOL", "TINSTANT"): "TBoolInst",
                         ("T_TFLOAT", "TINSTANT"): "TFloatInst"},
            "collection": {"T_INTSET": "IntSet"},
        }
        rep = build_parity(_idl(["temporal_merge"]), oracle)
        self.assertEqual(rep["status"], "audited")
        kinds = rep["byKind"]
        self.assertIn("concrete-missing-in-pymeos", kinds)
        self.assertIn("abstract-missing-in-pymeos", kinds)
        self.assertIn("collection-missing-in-pymeos", kinds)
        # nothing silently dropped
        self.assertEqual(rep["knownCorrections"] + rep["needsCorrection"],
                         rep["divergences"])

    def test_every_divergence_has_a_correction(self):
        oracle = _parse_oracle(PYMEOS)
        if oracle is None:
            self.skipTest("PyMEOS oracle not available")
        rep = build_parity(
            _idl(["temporal_merge", "tnumber_integral"]), oracle)
        self.assertEqual(rep["status"], "audited")
        self.assertEqual(rep["needsCorrection"], 0,
                         [w for w in rep["worklist"]
                          if w["status"] == "needs-correction"])
        self.assertEqual(rep["aligned"], 18)   # PyMEOS's 18 concrete classes


@unittest.skipUnless(_CATALOG.exists(), "run `python run.py` first")
class LiveParityGate(unittest.TestCase):
    def test_live_no_divergence_unexplained(self):
        cat = json.loads(_CATALOG.read_text())
        if "objectModel" not in cat:
            self.skipTest("catalog has no objectModel")
        rep = build_parity(cat, _parse_oracle(PYMEOS))
        # honest accounting: every divergence classified, none dropped
        self.assertEqual(rep["knownCorrections"] + rep["needsCorrection"],
                         rep["divergences"])
        if rep["status"] == "audited":
            self.assertEqual(rep["needsCorrection"], 0,
                             [w for w in rep["worklist"]
                              if w["status"] == "needs-correction"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
