"""Unit tests for covering_parity.py.  python3 tests/test_covering_parity.py

Also the CI gate: when an enriched catalog with `temporalCovering` is
present, every C symbol the descriptor names must be backed by the catalog
and every covered type must be a real MeosType — never silently missing.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.covering import attach_temporal_covering
from tools.covering_parity import build_parity

MAP = ROOT / "meta" / "temporal-covering.json"
_CATALOG = ROOT / "output" / "meos-idl.json"


def _catalog(fn_names, enum_types=None):
    cat = attach_temporal_covering(
        {"functions": [{"name": n} for n in fn_names]}, MAP)
    if enum_types is not None:
        cat["enums"] = [{"name": "MeosType",
                         "values": [{"name": "T_" + t.upper()}
                                    for t in enum_types]}]
    return cat


class ParityLogicTests(unittest.TestCase):
    def test_all_backed_when_symbols_present(self):
        cov = attach_temporal_covering({}, MAP)["temporalCovering"]
        r = build_parity(_catalog(cov["symbols"], enum_types=cov["types"]))
        self.assertEqual(r["symbolsMissing"], [])
        self.assertEqual(r["symbolsBacked"], r["symbolsTotal"])
        self.assertEqual(r["parityPct"], 100.0)
        self.assertEqual(r["typesInvalid"], [])
        self.assertTrue(r["typesChecked"])

    def test_missing_symbol_reported_not_dropped(self):
        cov = attach_temporal_covering({}, MAP)["temporalCovering"]
        present = [s for s in cov["symbols"] if s != "stbox_xmin"]
        r = build_parity(_catalog(present, enum_types=cov["types"]))
        self.assertIn("stbox_xmin", r["symbolsMissing"])
        self.assertEqual(r["symbolsBacked"] + len(r["symbolsMissing"]),
                         r["symbolsTotal"])

    def test_invalid_type_reported(self):
        # a type absent from the MeosType enum is flagged, not silently ok
        r = build_parity(_catalog([], enum_types=["tgeompoint"]))
        self.assertIn("tfloat", r["typesInvalid"])

    def test_types_unverified_without_enum(self):
        r = build_parity(_catalog([]))           # no MeosType enum present
        self.assertFalse(r["typesChecked"])
        self.assertEqual(r["typesInvalid"], [])

    def test_requires_temporal_covering(self):
        with self.assertRaises(ValueError):
            build_parity({"functions": []})


@unittest.skipUnless(_CATALOG.exists(), "run `python run.py` first")
class LiveParityGate(unittest.TestCase):
    def test_every_symbol_backed_and_types_valid(self):
        cat = json.loads(_CATALOG.read_text())
        cat = attach_temporal_covering(cat, MAP)
        r = build_parity(cat)
        self.assertEqual(r["symbolsMissing"], [],
                         "covering descriptor references unexported symbols")
        self.assertEqual(r["typesInvalid"], [],
                         "covering descriptor references non-MeosType types")
        self.assertEqual(r["parityPct"], 100.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
