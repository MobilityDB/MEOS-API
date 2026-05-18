"""Unit tests for the portable bare-name mapping.

Runs without libclang or pytest:  python3 tests/test_portable.py
Validates the canonical mapping file *and* guards the corrected
scope rule: cbuffer/npoint/pose/rgeo are in scope, never excluded.
"""

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.portable import attach_portable_aliases

MAP = ROOT / "meta" / "portable-aliases.json"
SCHEMA = ROOT / "meta" / "portable-aliases.schema.json"
_EXPECTED_FAMILY_SIZES = {
    "topology": 4, "timePosition": 4, "spaceX": 4, "spaceY": 4,
    "spaceZ": 4, "temporalComparison": 6, "distance": 2, "same": 1,
}


class MappingFileTests(unittest.TestCase):
    def setUp(self):
        self.d = json.loads(MAP.read_text())

    def test_families_complete_and_sized(self):
        self.assertEqual(set(self.d["families"]),
                         set(_EXPECTED_FAMILY_SIZES))
        for fam, n in _EXPECTED_FAMILY_SIZES.items():
            self.assertEqual(len(self.d["families"][fam]), n, fam)

    def test_known_mappings_verbatim(self):
        flat = {p["operator"]: p["bareName"]
                for fam in self.d["families"].values() for p in fam}
        for op, bn in [("&&", "overlaps"), ("@>", "contains"),
                       ("-|-", "adjacent"), ("<<#", "before"),
                       ("#&>", "overafter"), ("|&>", "overabove"),
                       ("/&>", "overback"), ("#=", "teq"), ("#<>", "tne"),
                       ("|=|", "nearestApproachDistance"), ("~=", "same")]:
            self.assertEqual(flat[op], bn)
        self.assertEqual(sum(_EXPECTED_FAMILY_SIZES.values()), 29)
        self.assertEqual(len(flat), 29)

    def test_scope_correction_no_exclusion(self):
        # The corrected 100%-parity rule: these are IN scope, never deferred.
        s = self.d["scope"]
        for t in ("cbuffer", "npoint", "pose", "rgeo"):
            self.assertIn(t, s["inScopeTypeFamilies"])
        # no exclusion machinery anywhere in the artifact
        self.assertNotIn("deferredFamilies", self.d)
        self.assertNotIn("excludedFamilies", self.d)
        # PR #8 review item #1: tests on the structured flag, not on prose.
        # The prose `note` is human-readable supplement only; can be freely
        # reworded without breaking this test.
        self.assertEqual(s["deferralIsError"], True)

    def test_already_canonical_has_kind_discriminator(self):
        # PR #8 review item #2: every alreadyCanonical entry declares its
        # `kind` so downstream codegens discriminate by field, not by guessing.
        for entry in self.d["alreadyCanonical"]:
            self.assertIn("kind", entry,
                          f"alreadyCanonical entry missing `kind`: {entry}")
            self.assertIn(entry["kind"], ("family", "functions"))
            if entry["kind"] == "family":
                for k in ("family", "operators", "pattern"):
                    self.assertIn(k, entry)
            elif entry["kind"] == "functions":
                self.assertIn("functions", entry)

    def test_already_canonical_and_provenance(self):
        pats = {a.get("pattern") for a in self.d["alreadyCanonical"]
                if a.get("kind") == "family"}
        self.assertIn("ever_*", pats)
        self.assertIn("always_*", pats)
        self.assertEqual(self.d["provenance"]["nativePR"][:14],
                         "MobilityDB#107")

    def test_explicit_backing_verified(self):
        # verified (not guessed): nearestApproachDistance ↔ nad_*
        self.assertEqual(self.d["explicitBacking"],
                         {"nearestApproachDistance": ["nad"]})

    def test_schema_validation(self):
        """PR #8 review item #3: catch shape regressions earlier than the
        unit tests by validating portable-aliases.json against its
        JSON Schema. Skipped when `jsonschema` isn't installed (it's not a
        hard runtime dep — only enforced when available)."""
        try:
            import jsonschema
        except ImportError:
            self.skipTest("jsonschema not installed; install with `pip install jsonschema`")
        schema = json.loads(SCHEMA.read_text())
        # validate() raises jsonschema.ValidationError on failure
        jsonschema.validate(instance=self.d, schema=schema)


class AttachTests(unittest.TestCase):
    def test_attach_and_derive(self):
        idl = attach_portable_aliases({"functions": []}, MAP)
        pa = idl["portableAliases"]
        self.assertEqual(pa["count"], 29)
        self.assertEqual(pa["byOperator"]["&&"], "overlaps")
        self.assertEqual(pa["byBareName"]["overlaps"], "&&")
        self.assertEqual(pa["bareNames"], sorted(pa["byBareName"]))
        # bijective: 29 distinct operators and 29 distinct bare names
        self.assertEqual(len(pa["byOperator"]), 29)
        self.assertEqual(len(pa["byBareName"]), 29)
        self.assertIn("cbuffer", pa["scope"]["inScopeTypeFamilies"])
        self.assertEqual(pa["explicitBacking"],
                         {"nearestApproachDistance": ["nad"]})

    def test_missing_file_is_noop(self):
        idl = attach_portable_aliases({"x": 1}, ROOT / "nope.json")
        self.assertNotIn("portableAliases", idl)

    def test_duplicate_detection(self):
        bad = {"families": {"a": [{"operator": "&&", "bareName": "x"},
                                  {"operator": "@>", "bareName": "x"}]},
               "provenance": {}, "alreadyCanonical": [], "scope": {},
               "notes": []}
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False) as f:
            json.dump(bad, f)
            p = f.name
        with self.assertRaises(ValueError):
            attach_portable_aliases({}, Path(p))


if __name__ == "__main__":
    unittest.main(verbosity=2)
