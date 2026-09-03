"""Unit tests + drift gate for the explicit object model.

Runs without libclang or pytest:  python3 tests/test_object_model.py

The DriftGate re-derives every lattice membership set from the MobilityDB
sources (the predicate bodies, MEOS_TEMPTYPE_CATALOG, the tempSubtype and
errorCode enums) and asserts the curated meta matches — so the source of
truth cannot silently drift away from MEOS.
"""

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from parser.typerelations import bbox_types, temptype_basetypes
from parser.object_model import (MembershipUnavailable, byreference_basetypes,
                                  derive_membership, predicate_temptypes)
from parser.object_model import (
    _scan_errors, attach_object_model, find_mobilitydb_src)

MODEL = ROOT / "meta" / "object-model.json"
_INTERNAL = {"T_TDOUBLE2", "T_TDOUBLE3", "T_TDOUBLE4"}  # not public classes

#: Temporal types MEOS admits that no leaf class models yet, so the lattice
#: cannot express them and a binding projected from it reaches them only
#: through the flat C surface. This set is a RATCHET, not a permission: the
#: coverage test compares against it exactly, so a new uncovered type fails the
#: suite, and giving one of these a class fails it too until the entry goes.
#:
#: The three cell-index families are one open question rather than three: H3,
#: quadbin and S2 are the same kind of thing — a temporal value over a discrete
#: global grid — and whether they sit under a shared abstract parent, the way
#: TPoint groups TGeomPoint and TGeogPoint, is a taxonomy decision the model
#: owner makes. The other three have obvious homes beside their siblings.
_UNMODELLED = {
    "T_TH3INDEX", "T_TQUADBIN", "T_TS2CELL",   # cell-index grouping undecided
    "T_TPOSECHAIN",                            # sibling of TPose
    "T_TPCPOINT", "T_TPCPATCH",                # the point-cloud pair
}


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
                # A leaf names the ONE type it models; its base type is the
                # catalog's to give and is derived onto the attached model.
                self.assertEqual(len(s["temptypes"]), 1, n)
            if s["kind"] in ("root", "abstract"):
                self.assertIsNotNone(s.get("predicate"), n)

    def test_companions_are_well_formed_trees(self):
        fams = _nodes(self.d["companions"])
        self.assertEqual(set(fams), {"Box", "Collection", "Value"})
        for fam in fams:
            nodes = _nodes(self.d["companions"][fam]["nodes"])
            roots = [n for n, s in nodes.items() if s["parent"] is None]
            self.assertEqual(len(roots), 1, fam)
            for n, s in nodes.items():
                if s["parent"]:
                    self.assertIn(s["parent"], nodes)
                if s["kind"] == "leaf":
                    # A leaf names the ONE type it models, and a null says MEOS
                    # registers it in no enum — stated rather than left out,
                    # since an absent field reads the same as one nobody wrote.
                    self.assertIn("temptype", s, n)
                    if s["temptype"] is None:
                        self.assertTrue(s.get("doc"), n)

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

    def test_internal_api_methods_are_excluded(self):
        # A function the catalog marks internal is classified to its class (so
        # the reverse index stays complete) but flagged ooExclude, so a binding
        # generating from classes[*].methods keeps only the public surface.
        idl = attach_object_model({"functions": [
            {"name": "temporal_num_instants", "api": "public"},
            {"name": "temporal_inst_n", "api": "internal"},
        ]}, MODEL, None)
        meths = {m["backing"]: m
                 for m in idl["objectModel"]["classes"]["Temporal"]["methods"]}
        self.assertNotIn("ooExclude", meths["temporal_num_instants"])
        self.assertTrue(meths["temporal_inst_n"].get("ooExclude"))

    def test_ooname_drops_the_prefix_the_classifier_matched(self):
        # A class reached by a prefix that is not its lower-cased name —
        # `geom_*` for Geometry, `geog_*` for Geography, `geoset_*` for
        # GeomSet — otherwise keeps that prefix in the member name, so the
        # method reads `geometry.geomBuffer()`.
        om = attach_object_model({"functions": [
            {"name": "geom_from_hexewkb"}, {"name": "geog_from_hexewkb"},
            {"name": "geoset_start_value"}, {"name": "set_set_subspan"},
        ]}, MODEL, None)["objectModel"]
        names = {m["function"]: m["ooName"]
                 for s in om["classes"].values() for m in s["methods"]}
        self.assertEqual(names["geom_from_hexewkb"], "fromHexewkb")
        self.assertEqual(names["geog_from_hexewkb"], "fromHexewkb")
        self.assertEqual(names["geoset_start_value"], "startValue")
        # One token is dropped, not every repetition of it.
        self.assertEqual(names["set_set_subspan"], "setSubspan")

    def test_a_class_nothing_is_called_on_takes_what_it_makes(self):
        # Nothing is called ON a point-cloud schema — every accessor takes one
        # as an ARGUMENT — so what the class MAKES says what it is, and the
        # `char *` among those answers says nothing, being a string.
        om = attach_object_model({"functions": [
            {"name": "meos_pc_schema", "returnType": {"c": "PCSCHEMA *"},
             "params": [{"name": "pcid", "cType": "uint32_t"}]},
            {"name": "meos_pc_schema_xml", "returnType": {"c": "const char *"},
             "params": [{"name": "pcid", "cType": "uint32_t"}]},
            {"name": "meos_pc_schema_compression",
             "returnType": {"c": "const char *"},
             "params": [{"name": "pcid", "cType": "uint32_t"}]},
        ]}, MODEL, None)["objectModel"]
        self.assertEqual(om["classes"]["Pcschema"]["cType"], "PCSCHEMA")

    def test_a_type_meos_registers_in_no_enum_still_gets_a_class(self):
        # A pointer to a struct is one thing to a binding whether MEOS
        # publishes the layout or forward-declares it, so these sit in `Value`
        # beside the base values and say with a null that they name no type.
        nodes = _nodes(json.loads(MODEL.read_text())["companions"]["Value"]
                       ["nodes"])
        unregistered = {n for n, s in nodes.items()
                        if s["kind"] == "leaf" and s["temptype"] is None}
        self.assertEqual(unregistered,
                         {"Pcschema", "RTree", "SPTree", "MeosArray"})

    def test_a_function_named_as_its_own_prefix_still_has_a_member_name(self):
        # `meos_pc_schema` IS its class's prefix, so dropping the prefix leaves
        # nothing — the one case the override table exists for.
        om = attach_object_model({"functions": [
            {"name": "meos_pc_schema"}, {"name": "meos_pc_schema_ndims"},
        ]}, MODEL, None)["objectModel"]
        names = {m["function"]: m["ooName"]
                 for m in om["classes"]["Pcschema"]["methods"]}
        self.assertEqual(names, {"meos_pc_schema": "get",
                                 "meos_pc_schema_ndims": "ndims"})

    def test_class_ctype_comes_from_the_receiver(self):
        # A receiver-role method takes the value it is called on first, so its
        # pointee names what the class's instances are — which is what a
        # binding declares every wrapper in terms of.
        om = attach_object_model({"functions": [
            {"name": "cbuffer_srid",
             "params": [{"name": "cbuf", "cType": "const Cbuffer *"}]},
            {"name": "geom_to_geog",
             "params": [{"name": "geo", "cType": "const GSERIALIZED *"}]},
        ]}, MODEL, None)["objectModel"]
        self.assertEqual(om["classes"]["Cbuffer"]["cType"], "Cbuffer")
        self.assertEqual(om["classes"]["Geometry"]["cType"], "GSERIALIZED")

    def test_a_constructor_only_class_takes_its_subtypes_ctype(self):
        # `tfloatinst_make` builds a value out of a base value and a time, so
        # its first parameter says nothing about the class; the concrete class
        # is the product of a leaf and a subtype, and the subtype answers.
        om = attach_object_model({"functions": [
            {"name": "tfloatinst_make",
             "params": [{"name": "d", "cType": "double"},
                        {"name": "t", "cType": "TimestampTz"}]},
            {"name": "tinstant_value",
             "params": [{"name": "inst", "cType": "const TInstant *"}]},
        ]}, MODEL, None)["objectModel"]
        self.assertEqual(om["classes"]["TFloatInst"]["cType"], "TInstant")

    def test_a_class_with_no_receiver_takes_its_parents_ctype(self):
        # A collection leaf whose only method builds a set out of geographies
        # says nothing about itself either, and it is no product of a subtype;
        # its parent answers, which is the same C type by construction.
        om = attach_object_model({"functions": [
            {"name": "geogset_make",
             "params": [{"name": "values", "cType": "const GSERIALIZED **"},
                        {"name": "count", "cType": "int"}]},
            {"name": "set_num_values",
             "params": [{"name": "s", "cType": "const Set *"}]},
        ]}, MODEL, None)["objectModel"]
        self.assertEqual(om["classes"]["GeogSet"]["cType"], "Set")

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
        # The contract's codes carry through with no source; how many there are
        # is the model's to state, and a literal here is one more copy of it.
        self.assertEqual(om["errors"]["codes"],
                         json.loads(MODEL.read_text())["errors"]["codes"])

    def test_scanned_errors_are_sorted_for_reproducibility(self):
        # The raises map is keyed by the public function set; iterating a set is
        # hash-seed dependent, so the keys are sorted to keep the emitted catalog
        # byte-identical across runs.
        src = (
            'Datum zzz_fn(int x) {\n'
            '  meos_error(ERROR, MEOS_ERR_INVALID_ARG_VALUE, "bad");\n}\n'
            'Datum aaa_fn(int y) {\n'
            '  meos_error(ERROR, MEOS_ERR_INVALID_ARG_TYPE, "bad");\n}\n'
            'Datum mmm_fn(int z) {\n'
            '  meos_error(ERROR, MEOS_ERR_INVALID_ARG_VALUE, "bad");\n}\n'
        )
        with tempfile.TemporaryDirectory() as d:
            (Path(d) / "x.c").write_text(src)
            result = _scan_errors(Path(d), {"zzz_fn", "aaa_fn", "mmm_fn"})
        self.assertEqual(list(result), ["aaa_fn", "mmm_fn", "zzz_fn"])
        self.assertEqual(list(result), sorted(result))


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


class SourceRootResolutionTest(unittest.TestCase):
    """The resolver answers over the checkout the provisioning names.

    The directory name belongs to the provisioner: the CI action checks MobilityDB
    out as ``_mobilitydb_src`` and hands the parse ``MDB_SRC_ROOT``, while the
    literal probe names ``_mobilitydb``.  A resolver reading only the literal name
    reports no source over a tree that is present, and the catalog then carries
    neither the object model nor the type relations the source states.
    """

    def _tree(self, d):
        src = Path(d) / "meos" / "src" / "temporal"
        src.mkdir(parents=True)
        (src / "meos_catalog.c").write_text("/* catalog */\n")
        return Path(d)

    def test_provisioned_root_resolves_whatever_the_directory_is_called(self):
        with tempfile.TemporaryDirectory() as d:
            root = self._tree(Path(d) / "_mobilitydb_src")
            saved = {k: os.environ.pop(k, None) for k in ("MOBILITYDB_SRC", "MDB_SRC_ROOT")}
            try:
                os.environ["MDB_SRC_ROOT"] = str(root)
                self.assertEqual(find_mobilitydb_src(), root / "meos" / "src")
            finally:
                for k, v in saved.items():
                    if v is None:
                        os.environ.pop(k, None)
                    else:
                        os.environ[k] = v

    def test_no_source_still_answers_none(self):
        # From a directory holding no checkout, so the relative `_mobilitydb`
        # probe cannot answer and the result is the resolver's own.
        saved = {k: os.environ.pop(k, None) for k in ("MOBILITYDB_SRC", "MDB_SRC_ROOT")}
        cwd = os.getcwd()
        try:
            os.environ["MDB_SRC_ROOT"] = "/no/such/checkout"
            with tempfile.TemporaryDirectory() as empty:
                os.chdir(empty)
                self.assertIsNone(find_mobilitydb_src())
        finally:
            os.chdir(cwd)
            for k, v in saved.items():
                if v is not None:
                    os.environ[k] = v


_SRC = find_mobilitydb_src(ROOT / "meos" / "include")
_CAT_C = (_SRC / "temporal" / "meos_catalog.c") if _SRC else None

#: The public headers, in whichever of the two trees carries them. An enum is
#: looked up across all of them rather than in one named file: `errorCode` is
#: declared in `meos_error.h` and `tempSubtype` in `meos.h`, and either can move
#: to another public header without ceasing to be the source of truth.
_HEADER_DIRS = [d for d in ((_SRC.parent / "include") if _SRC else None,
                            ROOT / "meos" / "include") if d and d.is_dir()]
_HEADERS = [h for d in _HEADER_DIRS for h in sorted(d.glob("meos*.h"))]


def _enum_from_headers(end_marker: str) -> dict:
    """The enum ending at ``end_marker``, from the public header declaring it."""
    for h in _HEADERS:
        text = h.read_text(errors="ignore")
        if end_marker in text:
            return _enum_block(text, end_marker)
    raise AssertionError(
        f"no public header declares `{end_marker}` "
        f"(searched {len(_HEADERS)} under {[str(d) for d in _HEADER_DIRS]})")


_SYNTHETIC_CATALOG = """
static const reltype_catalog_struct MEOS_RELTYPE_CATALOG[] =
{
  [T_TBOOL] = { .type_bboxtype = T_TSTZSPAN, .temptype_basetype = T_BOOL },
  [T_TNEW]  = { .type_bboxtype = T_TSTZSPAN, .temptype_basetype = T_NEW },
};

bool
demo_type(MeosType type)
{
  return (type == T_TBOOL ||
    /* the doubleX are internal aggregation types */
    type == T_TDOUBLE2 ||
    type == T_TNEW);
}
"""


class MembershipDerivationTest(unittest.TestCase):
    """The membership comes from the source, so a type MEOS adds arrives free."""

    def test_a_type_added_to_a_predicate_is_read_with_no_model_edit(self):
        # The property the derivation exists for.
        self.assertEqual(predicate_temptypes(_SYNTHETIC_CATALOG, "demo_type"),
                         ["T_TBOOL", "T_TNEW"])

    def test_the_internal_aggregation_types_stay_out_of_the_model(self):
        self.assertNotIn("T_TDOUBLE2",
                         predicate_temptypes(_SYNTHETIC_CATALOG, "demo_type"))

    def test_a_predicate_the_source_does_not_declare_raises(self):
        with self.assertRaises(MembershipUnavailable):
            predicate_temptypes(_SYNTHETIC_CATALOG, "no_such_type")

    def test_derive_fills_predicate_nodes_and_leaf_base_types(self):
        nodes = {
            "Demo": {"kind": "root", "predicate": "demo_type"},
            "TBool": {"kind": "leaf", "predicate": None,
                      "temptypes": ["T_TBOOL"]},
        }
        derive_membership(nodes, _SYNTHETIC_CATALOG,
                          temptype_basetypes(_SYNTHETIC_CATALOG))
        self.assertEqual(nodes["Demo"]["temptypes"], ["T_TBOOL", "T_TNEW"])
        self.assertEqual(nodes["TBool"]["cBaseType"], "T_BOOL")

    def test_a_leaf_whose_type_the_catalog_does_not_relate_raises(self):
        nodes = {"TGhost": {"kind": "leaf", "predicate": None,
                            "temptypes": ["T_TGHOST"]}}
        with self.assertRaises(MembershipUnavailable):
            derive_membership(nodes, _SYNTHETIC_CATALOG,
                              temptype_basetypes(_SYNTHETIC_CATALOG))

    def test_attaching_without_the_source_says_so_rather_than_emptying(self):
        # A class naming no type would read the same as one MEOS has no type
        # for, so the status says which it is — as the error contract does.
        saved = os.environ.pop("MDB_SRC_ROOT", None)
        try:
            om = attach_object_model({"functions": []}, MODEL, None)["objectModel"]
            self.assertEqual(om["membership"]["status"], "source-unavailable")
            self.assertNotIn("temptypes", om["lattice"]["Temporal"])
        finally:
            if saved is not None:
                os.environ["MDB_SRC_ROOT"] = saved

    @unittest.skipUnless(_CAT_C and _CAT_C.exists(),
                         "MobilityDB sources not available (run setup.py)")
    def test_attaching_with_the_source_says_it_derived(self):
        om = attach_object_model({"functions": []}, MODEL, _SRC)["objectModel"]
        self.assertEqual(om["membership"]["status"], "derived")
        self.assertIn("T_TPOSECHAIN", om["lattice"]["Temporal"]["temptypes"])


@unittest.skipUnless(_CAT_C and _CAT_C.exists(),
                     "MobilityDB sources not available (run setup.py)")
class DriftGate(unittest.TestCase):
    """What the ATTACHED model says, against what MEOS says.

    The model file no longer states the membership, so there is no copy left to
    drift; what these hold is that the derivation reaches the published lattice
    and answers what the source answers.
    """

    @classmethod
    def setUpClass(cls):
        cls.d = json.loads(MODEL.read_text())
        cls.cat = _CAT_C.read_text(errors="ignore")
        cls.attached = attach_object_model(
            {"functions": []}, MODEL, _SRC)["objectModel"]
        cls.lat = _nodes(cls.attached["lattice"])

    def test_the_model_file_states_no_membership_of_its_own(self):
        # The property the derivation exists for: a copy is what drifts, so
        # there must be none to drift. A leaf still names the ONE type it
        # models — that is the class's identity, not a copy of MEOS.
        for node, spec in _nodes(self.d["lattice"]).items():
            if spec.get("predicate"):
                self.assertNotIn("temptypes", spec, f"{node} copies membership")
            self.assertNotIn("cBaseType", spec, f"{node} copies its base type")
        for name, t in _nodes(self.d["traits"]).items():
            self.assertNotIn("temptypes", t, f"{name} copies membership")

    def test_predicate_membership_matches_source(self):
        checked = 0
        for node, spec in self.lat.items():
            pred = spec.get("predicate")
            if not pred:
                continue
            derived = set(_predicate_temptypes(self.cat, pred)) - _INTERNAL
            self.assertEqual(set(spec["temptypes"]), derived,
                             f"{node} ({pred}) does not answer its predicate")
            checked += 1
        self.assertTrue(checked, "no node carries a predicate — derivation ran?")

    def test_traits_match_source(self):
        traits = _nodes(self.attached["traits"])
        self.assertTrue(traits, "no trait attached")
        for name, t in traits.items():
            derived = set(_predicate_temptypes(self.cat, t["predicate"]))
            self.assertEqual(set(t["temptypes"]), derived - _INTERNAL, name)

    def test_every_temporal_type_has_a_leaf_class(self):
        # The root's membership comes from `temporal_type()`, so the root
        # already knows every temporal type MEOS admits. A LEAF is what gives
        # one of them a class, and a type the root admits while no leaf claims
        # it is a type the object model cannot express: a binding projected
        # from this lattice reaches it only through the flat C surface.
        #
        # This is coverage, not membership — the assertions above hold each
        # node to its own predicate and pass whether or not a type has a class
        # at all, which is why the gap survived them.
        root = self.lat["Temporal"]
        admitted = set(root["temptypes"]) - _INTERNAL
        self.assertTrue(admitted, "the root claims no temporal type")

        claimed = {t for spec in self.lat.values() if spec["kind"] == "leaf"
                   for t in spec["temptypes"]}
        self.assertTrue(claimed, "no leaf claims a temporal type")

        # A type may not be claimed twice: two classes for one type is an
        # ambiguity the projection cannot resolve.
        seen = {}
        for node, spec in self.lat.items():
            if spec["kind"] != "leaf":
                continue
            for t in spec["temptypes"]:
                self.assertNotIn(t, seen,
                                 f"{t} is claimed by both {seen.get(t)} and {node}")
                seen[t] = node

        self.assertEqual(admitted - claimed, _UNMODELLED,
                         "the set of temporal types with no leaf class moved; "
                         "give the new one a class, or record it in _UNMODELLED "
                         "with the reason it has none")
        self.assertEqual(claimed - admitted, set(),
                         "leaf classes modelling a type MEOS does not admit")

    def test_every_bbox_type_has_a_companion_class(self):
        # A temporal value stores a box, and `MEOS_RELTYPE_CATALOG` names which
        # one per type in its `type_bboxtype` column. Every box named there is
        # a class a method returns, so the column is the coverage oracle: a box
        # type MEOS gains fails here until a companion class carries it.
        boxes = set(bbox_types(self.cat))
        self.assertTrue(boxes, "the relation catalog names no bounding box")
        claimed = {spec["temptype"]
                   for fam in _nodes(self.attached["companions"])
                   for spec in _nodes(
                       self.attached["companions"][fam]["nodes"]).values()
                   if spec["kind"] == "leaf" and spec.get("temptype")}
        self.assertEqual(boxes - claimed, set(),
                         "a box type the relation catalog names has no "
                         "companion class, so the method answering it is "
                         "untypable")

    def test_every_byreference_base_type_has_a_value_class(self):
        # The Value hierarchy exists so a method taking or answering a base
        # value can be typed, and a base value crosses the boundary as a
        # pointer unless `basetype_byvalue` names its type. So the types that
        # predicate does NOT name are exactly the ones needing a class, and
        # MEOS gaining one reaches here with no edit in the model file.
        #
        # Coverage, not membership: the extras below are types no membership
        # predicate admits, so nothing derives them and the file states them.
        needed = set(byreference_basetypes(self.cat))
        self.assertTrue(needed, "no base type crosses by reference")

        nodes = _nodes(self.attached["companions"]["Value"]["nodes"])
        seen = {}
        for node, spec in nodes.items():
            if spec["kind"] != "leaf":
                continue
            tt = spec["temptype"]
            if tt is None:
                # A type MEOS registers in no enum: a class for it is still
                # needed to type the methods naming it, and the coverage this
                # gate measures is over the types MEOS DOES register.
                continue
            self.assertNotIn(tt, seen,
                             f"{tt} is claimed by both {seen.get(tt)} and {node}")
            seen[tt] = node

        self.assertEqual(needed - set(seen), set(),
                         "a base type MEOS passes by reference has no Value "
                         "class, so every method naming it stays untypable")
        self.assertEqual(set(seen) - needed,
                         {"T_NSEGMENT", "T_RAQUET", "T_JSONPATH"},
                         "the Value classes MEOS admits without making them "
                         "base types moved; each is a MeosType a signature "
                         "names, and this states which")

    def test_leaf_base_types_match_catalog(self):
        # The relation is a `.temptype_basetype` field of the type-indexed
        # MEOS_RELTYPE_CATALOG; read it through the parser that already reads
        # that array rather than matching its shape a second time here.
        pairs = temptype_basetypes(self.cat)
        self.assertTrue(pairs, "MEOS_RELTYPE_CATALOG yielded no base type")
        leaves = 0
        for node, spec in self.lat.items():
            if spec["kind"] == "leaf":
                tt = spec["temptypes"][0]
                self.assertEqual(spec["cBaseType"], pairs[tt],
                                 f"{node} base type does not answer the catalog")
                leaves += 1
        self.assertTrue(leaves, "no leaf attached")

    @unittest.skipUnless(_HEADERS, "MEOS public headers not available")
    def test_enums_match_source(self):
        sub = _enum_from_headers("} tempSubtype;")
        for v in self.d["axes"]["subtype"]["values"]:
            self.assertEqual(sub[v["name"]], v["value"], v["name"])
        err = _enum_from_headers("} errorCode;")
        for c in self.d["errors"]["codes"]:
            self.assertEqual(err[c["name"]], c["value"], c["name"])
        self.assertEqual(len(self.d["errors"]["codes"]), len(err))


if __name__ == "__main__":
    unittest.main(verbosity=2)
