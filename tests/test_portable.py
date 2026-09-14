"""Unit tests for the portable bare-name mapping.

Runs without libclang or pytest:  python3 tests/test_portable.py
Validates the canonical mapping file *and* guards the corrected
scope rule: cbuffer/npoint/pose/rgeo are in scope, never excluded.
"""

import json
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.portable import (attach_portable_aliases, attach_position_names,
                             classify_backing_sqlfn)

MAP = ROOT / "meta" / "portable-aliases.json"
SCHEMA = ROOT / "meta" / "portable-aliases.schema.json"
_EXPECTED_FAMILY_SIZES = {
    "topology": 4, "temporalComparison": 6, "everComparison": 6,
    "alwaysComparison": 6, "distance": 2, "same": 1,
}
_EXPECTED_POSITION_SIZES = {
    "timePosition": 4, "spaceX": 4, "spaceY": 4, "spaceZ": 4,
}


def _attach_map(data):
    """Attach a mapping held in memory, as the pipeline attaches the file."""
    with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
        json.dump(data, f)
        p = f.name
    return attach_portable_aliases({}, Path(p))


def _stbox_position_functions(skip=()):
    """One stbox function per position operator, named as MobilityDB names it
    (`left_stbox_stbox`, @sqlfn stboxLeft, @sqlop <<)."""
    d = json.loads(MAP.read_text())
    return [{"name": f"{p['position']}_stbox_stbox",
             "sqlfn": "stbox" + p["position"][0].upper() + p["position"][1:],
             "sqlop": p["operator"]}
            for fam in d["positionFamilies"].values() for p in fam
            if p["operator"] not in skip]


class MappingFileTests(unittest.TestCase):
    def setUp(self):
        self.d = json.loads(MAP.read_text())

    def test_families_complete_and_sized(self):
        self.assertEqual(set(self.d["families"]),
                         set(_EXPECTED_FAMILY_SIZES))
        for fam, n in _EXPECTED_FAMILY_SIZES.items():
            self.assertEqual(len(self.d["families"][fam]), n, fam)

    def test_position_families_complete_and_sized(self):
        self.assertEqual(set(self.d["positionFamilies"]),
                         set(_EXPECTED_POSITION_SIZES))
        for fam, n in _EXPECTED_POSITION_SIZES.items():
            self.assertEqual(len(self.d["positionFamilies"][fam]), n, fam)

    def test_known_mappings_verbatim(self):
        flat = {p["operator"]: p["bareName"]
                for fam in self.d["families"].values() for p in fam}
        for op, bn in [("&&", "overlaps"), ("@>", "contains"),
                       ("-|-", "adjacent"), ("#=", "tEqual"),
                       ("#<>", "tNotEqual"), ("?=", "eEqual"), ("%=", "aEqual"),
                       ("|=|", "nearestApproachDistance"), ("~=", "same")]:
            self.assertEqual(flat[op], bn)
        self.assertEqual(sum(_EXPECTED_FAMILY_SIZES.values()), 25)
        self.assertEqual(len(flat), 25)

    def test_known_positions_verbatim(self):
        flat = {p["operator"]: p["position"]
                for fam in self.d["positionFamilies"].values() for p in fam}
        for op, pos in [("<<#", "before"), ("#&>", "overafter"),
                        ("<<", "left"), ("&>", "overright"),
                        ("|&>", "overabove"), ("/&>", "overback")]:
            self.assertEqual(flat[op], pos)
        self.assertEqual(sum(_EXPECTED_POSITION_SIZES.values()), 16)
        self.assertEqual(len(flat), 16)
        # a position operator has names by class, never a bare name
        bare_ops = {p["operator"]
                    for fam in self.d["families"].values() for p in fam}
        self.assertFalse(set(flat) & bare_ops)

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
        self.assertEqual(pa["count"], 25)
        self.assertEqual(pa["byOperator"]["&&"], "overlaps")
        self.assertEqual(pa["byBareName"]["overlaps"], "&&")
        self.assertEqual(pa["bareNames"], sorted(pa["byBareName"]))
        # bijective: 25 distinct operators and 25 distinct bare names
        self.assertEqual(len(pa["byOperator"]), 25)
        self.assertEqual(len(pa["byBareName"]), 25)
        self.assertIn("cbuffer", pa["scope"]["inScopeTypeFamilies"])
        self.assertEqual(pa["explicitBacking"],
                         {"nearestApproachDistance": ["nad"]})
        # the 16 position operators, with their position, and no bare name
        self.assertEqual(pa["byPositionOperator"]["<<"], "left")
        self.assertEqual(pa["byPositionOperator"]["<<#"], "before")
        self.assertEqual(len(pa["byPositionOperator"]), 16)
        self.assertNotIn("<<", pa["byOperator"])
        self.assertNotIn("left", pa["byBareName"])
        self.assertEqual(set(pa["positionFamilies"]),
                         set(_EXPECTED_POSITION_SIZES))

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
            {"name": "Left_stbox_stbox",     "sqlfn": "stboxLeft",     "sqlop": "<<"},
            {"name": "Tpoint_trajectory",    "sqlfn": "trajectory"},
        ]}, MAP)
        idl = classify_backing_sqlfn(idl)
        by = {f["name"]: f for f in idl["functions"]}
        # the two _bbox backing tags are flagged with the bare public name
        self.assertTrue(by["Same_stbox_stbox"]["sqlfnBackingOnly"])
        self.assertEqual(by["Same_stbox_stbox"]["publicSqlName"], "same")
        self.assertTrue(by["Contains_tbox_tnumber"]["sqlfnBackingOnly"])
        self.assertEqual(by["Contains_tbox_tnumber"]["publicSqlName"], "contains")
        # a position op whose @sqlfn IS the deployed name (stboxLeft) is untouched
        self.assertNotIn("sqlfnBackingOnly", by["Left_stbox_stbox"])
        # a plain function with no operator is untouched
        self.assertNotIn("sqlfnBackingOnly", by["Tpoint_trajectory"])

    def test_backing_sqlfn_noop_without_aliases(self):
        # No portableAliases attached -> nothing to classify, no crash.
        idl = classify_backing_sqlfn({"functions": [
            {"name": "X", "sqlfn": "same_bbox", "sqlop": "~="}]})
        self.assertNotIn("sqlfnBackingOnly", idl["functions"][0])

    def test_position_names_by_class(self):
        # Each position operator's names by class come from the @sqlfn of the
        # functions whose @sqlop it is; the class is the name less the position.
        fns = _stbox_position_functions() + [
            {"name": "left_set_set",         "sqlfn": "setLeft",  "sqlop": "<<"},
            {"name": "left_tnumber_tnumber", "sqlfn": "tboxLeft", "sqlop": "<<"},
            {"name": "left_tbox_tbox",       "sqlfn": "tboxLeft", "sqlop": "<<"},
            {"name": "same_stbox_stbox",     "sqlfn": "same_bbox", "sqlop": "~="},
        ]
        idl = attach_portable_aliases({"functions": fns}, MAP)
        pn = attach_position_names(idl)["portableAliases"]["positionNames"]
        self.assertEqual(len(pn), 16)
        self.assertEqual(pn["<<"], {"set": "setLeft", "stbox": "stboxLeft",
                                    "tbox": "tboxLeft"})
        self.assertEqual(pn["<<#"], {"stbox": "stboxBefore"})
        self.assertEqual(pn["&</"], {"stbox": "stboxOverfront"})
        # an operator with a bare name is not a position operator
        self.assertNotIn("~=", pn)

    def test_position_name_without_its_class_raises(self):
        # @sqlop << over an @sqlfn that is not <class>Left: the tags and the
        # mapping disagree, which must fail loudly rather than reach a binding.
        for sqlfn in ("temporal_left", "Left"):
            fns = _stbox_position_functions() + [
                {"name": "left_temporal_temporal", "sqlfn": sqlfn, "sqlop": "<<"}]
            idl = attach_portable_aliases({"functions": fns}, MAP)
            with self.assertRaises(ValueError):
                attach_position_names(idl)

    def test_position_operator_without_function_raises(self):
        idl = attach_portable_aliases(
            {"functions": _stbox_position_functions(skip=("/&>",))}, MAP)
        with self.assertRaisesRegex(ValueError, "/&>"):
            attach_position_names(idl)

    def test_position_names_noop_without_aliases(self):
        idl = attach_position_names({"functions": [
            {"name": "left_stbox_stbox", "sqlfn": "stboxLeft", "sqlop": "<<"}]})
        self.assertNotIn("portableAliases", idl)

    def test_duplicate_detection(self):
        bad = {"families": {"a": [{"operator": "&&", "bareName": "x"},
                                  {"operator": "@>", "bareName": "x"}]},
               "positionFamilies": {},
               "provenance": {}, "alreadyCanonical": [], "scope": {},
               "notes": []}
        with self.assertRaises(ValueError):
            _attach_map(bad)

    def test_position_duplicate_detection(self):
        base = {"families": {"a": [{"operator": "&&", "bareName": "overlaps"}]},
                "provenance": {}, "alreadyCanonical": [], "scope": {},
                "notes": []}
        for positions in (
                # one operator both a bare name and a position
                [{"operator": "&&", "position": "left"}],
                # one operator two positions
                [{"operator": "<<", "position": "left"},
                 {"operator": "<<", "position": "right"}],
                # one position two operators
                [{"operator": "<<", "position": "left"},
                 {"operator": ">>", "position": "left"}]):
            with self.assertRaises(ValueError):
                _attach_map(dict(base, positionFamilies={"p": positions}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
