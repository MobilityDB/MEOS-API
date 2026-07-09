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

from parser.portable import attach_portable_aliases, classify_backing_sqlfn

MAP = ROOT / "meta" / "portable-aliases.json"
SCHEMA = ROOT / "meta" / "portable-aliases.schema.json"
_EXPECTED_FAMILY_SIZES = {
    "topology": 4, "timePosition": 4, "spaceX": 4, "spaceY": 4,
    "spaceZ": 4, "temporalComparison": 6, "everComparison": 6,
    "alwaysComparison": 6, "distance": 2, "same": 1,
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
                       ("/&>", "overback"), ("#=", "tEq"), ("#<>", "tNe"),
                       ("?=", "eEq"), ("%=", "aEq"),
                       ("|=|", "nearestApproachDistance"), ("~=", "same")]:
            self.assertEqual(flat[op], bn)
        self.assertEqual(sum(_EXPECTED_FAMILY_SIZES.values()), 41)
        self.assertEqual(len(flat), 41)

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
        funcs = {f for a in self.d["alreadyCanonical"]
                 if a.get("kind") == "functions"
                 for f in a["functions"]}
        self.assertIn("eIntersects", funcs)
        self.assertIn("atTime", funcs)
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
        self.assertEqual(pa["count"], 41)
        self.assertEqual(pa["byOperator"]["&&"], "overlaps")
        self.assertEqual(pa["byBareName"]["overlaps"], "&&")
        self.assertEqual(pa["bareNames"], sorted(pa["byBareName"]))
        # bijective: 41 distinct operators and 41 distinct bare names
        self.assertEqual(len(pa["byOperator"]), 41)
        self.assertEqual(len(pa["byBareName"]), 41)
        self.assertIn("cbuffer", pa["scope"]["inScopeTypeFamilies"])
        self.assertEqual(pa["explicitBacking"],
                         {"nearestApproachDistance": ["nad"]})

    def test_missing_file_is_noop(self):
        idl = attach_portable_aliases({"x": 1}, ROOT / "nope.json")
        self.assertNotIn("portableAliases", idl)

    def test_backing_sqlfn_classification(self):
        # A bbox-topological function carries a shared `<op>_bbox` @sqlfn backing tag
        # that is never deployed as a CREATE FUNCTION; its public name is the operator's
        # bare alias. classify_backing_sqlfn must flag it and record publicSqlName.
        idl = attach_portable_aliases({"functions": [
            {"name": "Same_stbox_stbox",     "sqlfn": "same_bbox",     "sqlop": "~="},
            {"name": "Contains_tbox_tnumber", "sqlfn": "contains_bbox", "sqlop": "@>"},
            {"name": "Left_stbox_stbox",     "sqlfn": "temporal_left", "sqlop": "<<"},
            {"name": "Tpoint_trajectory",    "sqlfn": "trajectory"},
        ]}, MAP)
        idl = classify_backing_sqlfn(idl)
        by = {f["name"]: f for f in idl["functions"]}
        # the two _bbox backing tags are flagged with the bare public name
        self.assertTrue(by["Same_stbox_stbox"]["sqlfnBackingOnly"])
        self.assertEqual(by["Same_stbox_stbox"]["publicSqlName"], "same")
        self.assertTrue(by["Contains_tbox_tnumber"]["sqlfnBackingOnly"])
        self.assertEqual(by["Contains_tbox_tnumber"]["publicSqlName"], "contains")
        # a positional op whose @sqlfn IS the deployed name (temporal_left) is untouched
        self.assertNotIn("sqlfnBackingOnly", by["Left_stbox_stbox"])
        # a plain function with no operator is untouched
        self.assertNotIn("sqlfnBackingOnly", by["Tpoint_trajectory"])

    def test_backing_sqlfn_noop_without_aliases(self):
        # No portableAliases attached -> nothing to classify, no crash.
        idl = classify_backing_sqlfn({"functions": [
            {"name": "X", "sqlfn": "same_bbox", "sqlop": "~="}]})
        self.assertNotIn("sqlfnBackingOnly", idl["functions"][0])

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
