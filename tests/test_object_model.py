"""Unit tests + drift gate for the explicit object model.

Runs without libclang or pytest:  python3 tests/test_object_model.py

The DriftGate re-derives every lattice membership set from the MobilityDB
sources (the predicate bodies, MEOS_TEMPTYPE_CATALOG, the tempSubtype and
errorCode enums) and asserts the curated meta matches — so the source of
truth cannot silently drift away from MEOS.
"""

import json
import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.object_model import attach_object_model, find_mobilitydb_src

MODEL = ROOT / "meta" / "object-model.json"
_INTERNAL = {"T_TDOUBLE2", "T_TDOUBLE3", "T_TDOUBLE4"}  # not public classes


def _nodes(d):
    return {k: v for k, v in d.items() if not k.startswith("_")}


class ModelFileTests(unittest.TestCase):
    def setUp(self):
        self.d = json.loads(MODEL.read_text())
        self.lat = _nodes(self.d["lattice"])

    def test_lattice_is_a_well_formed_tree(self):
        roots = [n for n, s in self.lat.items() if s["parent"] is None]
        self.assertEqual(roots, ["Temporal"])
        for n, s in self.lat.items():
            if s["parent"] is not None:
                self.assertIn(s["parent"], self.lat, f"{n} parent missing")
            # no cycle: walking parents terminates at the root
            seen, p = {n}, s["parent"]
            while p:
                self.assertNotIn(p, seen, f"cycle through {n}")
                seen.add(p)
                p = self.lat[p]["parent"]
            self.assertIn("Temporal", seen | {n})

    def test_node_kinds_consistent(self):
        for n, s in self.lat.items():
            self.assertIn(s["kind"], ("root", "abstract", "leaf"))
            if s["kind"] == "leaf":
                self.assertIn("cBaseType", s, n)
                self.assertEqual(len(s["temptypes"]), 1, n)
            if s["kind"] in ("root", "abstract"):
                self.assertIsNotNone(s.get("predicate"), n)

    def test_companions_are_well_formed_trees(self):
        for fam in ("Box", "Collection"):
            nodes = _nodes(self.d["companions"][fam]["nodes"])
            roots = [n for n, s in nodes.items() if s["parent"] is None]
            self.assertEqual(len(roots), 1, fam)
            for n, s in nodes.items():
                if s["parent"]:
                    self.assertIn(s["parent"], nodes)
                if s["kind"] == "leaf":
                    self.assertIn("temptype", s, n)

    def test_traits_are_not_inheritance(self):
        # geometry/geodetic is a TRAIT axis, never a parent (no diamond).
        trait_preds = {t["predicate"]
                       for t in _nodes(self.d["traits"]).values()}
        for s in self.lat.values():
            self.assertNotIn(s.get("predicate"), trait_preds)

    def test_corrections_well_formed_and_unique(self):
        items = self.d["corrections"]["items"]
        ids = [c["id"] for c in items]
        self.assertEqual(len(ids), len(set(ids)), "duplicate correction id")
        for c in items:
            self.assertIn(c["side"], ("meos", "pymeos"))
            for k in ("location", "observed", "suggested"):
                self.assertTrue(c[k].strip(), c["id"])
        self.assertIn("OM-P7", ids)         # abstract spatial intermediates

    def test_matches_manual_figure_7_1(self):
        # The MobilityDB manual Ch.7 Figure 7.1 is authoritative for the
        # conceptual spatial tree. The model must contain exactly the
        # figure's spatial nodes plus the single API-level addition TPoint
        # (documented as OM-M6), and the figure's parent edges must hold.
        man = self.d["provenance"]["manual"]
        spatial = {n for n in self.lat
                   if n == "TSpatial" or self._under(n, "TSpatial")}
        self.assertEqual(spatial,
                         set(man["figureNodes"]) | {"TPoint"})
        # TGeo -> {TGeometry, TGeography, TGeomPoint, TGeogPoint} (via TPoint)
        for child in ("TGeometry", "TGeography"):
            self.assertEqual(self.lat[child]["parent"], "TGeo")
        for pt in ("TGeomPoint", "TGeogPoint"):
            self.assertEqual(self.lat[pt]["parent"], "TPoint")
        self.assertEqual(self.lat["TPoint"]["parent"], "TGeo")
        self.assertEqual(self.lat["TGeo"]["parent"], "TSpatial")
        # TSpatial -> {TGeo, TCbuffer, TNpoint, TPose, TRGeometry}
        for leaf in ("TCbuffer", "TNpoint", "TPose", "TRGeometry"):
            self.assertEqual(self.lat[leaf]["parent"], "TSpatial")
        # the broad TGeo == tgeo_type_all (manual), not the narrow predicate
        self.assertEqual(self.lat["TGeo"]["predicate"], "tgeo_type_all")
        self.assertEqual(self.lat["TGeo"]["apiPredicate"], "tgeo_type")

    def _under(self, node, root):
        p = self.lat[node]["parent"]
        while p:
            if p == root:
                return True
            p = self.lat[p]["parent"]
        return False

    def test_scope_keeps_special_types_in(self):
        for fam in ("cbuffer", "npoint", "pose", "rgeo"):
            self.assertIn(fam, self.d["scope"]["inScopeTypeFamilies"])
        self.assertNotIn("excludedFamilies", self.d)
        self.assertIn("never deferred or excluded", self.d["scope"]["note"])


class AttachTests(unittest.TestCase):
    CASES = {
        "temporal_merge": ("Temporal", "superclass"),
        "tnumber_integral": ("TNumber", "family"),
        "tpoint_speed": ("TPoint", "family"),
        "tgeo_centroid": ("TGeo", "family"),
        "tfloat_degrees": ("TFloat", "exact"),
        "tfloatinst_make": ("TFloatInst", "constructor"),
        "tfloatseqset_from_base_tstzspanset": ("TFloatSeqSet", "constructor"),
        "tgeompointinst_make": ("TGeomPointInst", "constructor"),
        "trgeoinst_make": ("TRGeometryInst", "constructor"),
        "trgeo_affine": ("TRGeometry", "exact"),
        "tsequenceset_make": ("TSequenceSet", "subtype"),
        "tcbuffer_make": ("TCbuffer", "exact"),
        "span_lower": ("Span", "companion"),
        "intset_make": ("IntSet", "companion"),
        "stbox_expand": ("STBox", "companion"),
    }

    def _attach(self, names):
        return attach_object_model(
            {"functions": [{"name": n} for n in names]}, MODEL, None)

    def test_classification(self):
        idl = self._attach(list(self.CASES) + ["add_int_int"])
        ftc = idl["objectModel"]["functionToClass"]
        for fn, (cls, scope) in self.CASES.items():
            self.assertEqual(ftc[fn]["class"], cls, fn)
            self.assertEqual(ftc[fn]["scope"], scope, fn)
            self.assertEqual(ftc[fn]["backing"], fn)      # by construction
        # honest unclassified — never force-fitted
        self.assertIsNone(ftc["add_int_int"]["class"])
        self.assertIn("no-prefix-match", ftc["add_int_int"]["reason"])

    def test_tree_derived(self):
        om = self._attach(["temporal_merge"])["objectModel"]
        lat = om["lattice"]
        self.assertEqual(lat["Temporal"]["depth"], 0)
        self.assertEqual(lat["TFloat"]["ancestors"], ["TNumber", "Temporal"])
        self.assertIn("TNumber", lat["Temporal"]["children"])
        self.assertEqual(lat["TFloat"]["depth"], 2)

    def test_longest_prefix_wins(self):
        # tgeompoint_ must beat tgeo_; tsequenceset_ must beat tsequence_
        idl = self._attach([
            "tgeompoint_trajectory", "tgeo_centroid",
            "tsequenceset_make", "tsequence_make"])
        ftc = idl["objectModel"]["functionToClass"]
        self.assertEqual(ftc["tgeompoint_trajectory"]["class"], "TGeomPoint")
        self.assertEqual(ftc["tgeo_centroid"]["class"], "TGeo")
        self.assertEqual(ftc["tsequenceset_make"]["class"], "TSequenceSet")
        self.assertEqual(ftc["tsequence_make"]["class"], "TSequence")

    def test_missing_file_is_noop(self):
        idl = attach_object_model({"x": 1}, ROOT / "nope.json", None)
        self.assertNotIn("objectModel", idl)

    def test_errors_source_unavailable_is_honest(self):
        om = self._attach(["temporal_merge"])["objectModel"]
        self.assertEqual(om["errors"]["status"], "source-unavailable")
        self.assertEqual(om["errors"]["raises"], {})       # not fabricated
        self.assertEqual(len(om["errors"]["codes"]), 21)


# ---------------------------------------------------------------------------
# Drift gate: the curated lattice must equal what MEOS actually defines.
# ---------------------------------------------------------------------------

def _brace_body(text: str, start: int) -> str:
    depth, i = 0, text.index("{", start)
    j = i
    while j < len(text):
        depth += (text[j] == "{") - (text[j] == "}")
        if depth == 0:
            return text[i:j + 1]
        j += 1
    return text[i:]


def _predicate_temptypes(cat_src: str, name: str) -> set:
    m = re.search(r"\n" + name + r"\(MeosType \w+\)\s*", cat_src)
    body = _brace_body(cat_src, m.end())
    return {t for t in re.findall(r"\bT_T[A-Z0-9_]+\b", body)}


def _enum_block(text: str, end_marker: str) -> dict:
    end = text.index(end_marker)
    start = text.rindex("typedef enum", 0, end)
    block = text[start:end]
    return {n: int(v) for n, v in
            re.findall(r"\b([A-Z][A-Z0-9_]+)\s*=\s*(\d+)", block)}


_SRC = find_mobilitydb_src(ROOT / "meos" / "include")
_CAT_C = (_SRC / "temporal" / "meos_catalog.c") if _SRC else None
_MEOS_H = None
if _SRC:
    for cand in (_SRC.parent / "include" / "meos.h",
                 ROOT / "meos" / "include" / "meos.h"):
        if cand.exists():
            _MEOS_H = cand
            break


@unittest.skipUnless(_CAT_C and _CAT_C.exists(),
                     "MobilityDB sources not available (run setup.py)")
class DriftGate(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.d = json.loads(MODEL.read_text())
        cls.cat = _CAT_C.read_text(errors="ignore")
        cls.lat = _nodes(cls.d["lattice"])

    def test_predicate_membership_matches_source(self):
        for node, spec in self.lat.items():
            pred = spec.get("predicate")
            if not pred:
                continue
            derived = _predicate_temptypes(self.cat, pred) - _INTERNAL
            self.assertEqual(set(spec["temptypes"]), derived,
                             f"{node} ({pred}) drifted from MEOS")

    def test_traits_match_source(self):
        for name, t in _nodes(self.d["traits"]).items():
            derived = _predicate_temptypes(self.cat, t["predicate"])
            self.assertEqual(set(t["temptypes"]), derived, name)

    def test_leaf_base_types_match_catalog(self):
        pairs = dict(re.findall(
            r"\{\s*(T_T[A-Z0-9_]+)\s*,\s*(T_[A-Z0-9_]+)\s*\}", self.cat))
        for node, spec in self.lat.items():
            if spec["kind"] == "leaf":
                tt = spec["temptypes"][0]
                self.assertEqual(spec["cBaseType"], pairs[tt],
                                 f"{node} base type drifted")

    @unittest.skipUnless(_MEOS_H and _MEOS_H.exists(), "meos.h not available")
    def test_enums_match_source(self):
        h = _MEOS_H.read_text(errors="ignore")
        sub = _enum_block(h, "} tempSubtype;")
        for v in self.d["axes"]["subtype"]["values"]:
            self.assertEqual(sub[v["name"]], v["value"], v["name"])
        err = _enum_block(h, "} errorCode;")
        for c in self.d["errors"]["codes"]:
            self.assertEqual(err[c["name"]], c["value"], c["name"])
        self.assertEqual(len(self.d["errors"]["codes"]), len(err))


if __name__ == "__main__":
    unittest.main(verbosity=2)
