"""Regression tests for parser/typerecover.py.

The recoverer rewrites IDL types that parsing collapsed to ``int`` /
``int *`` / ``int **`` back to the real type spelled in the header text,
preserving ``const`` and pointer depth. Two collapse mechanisms are covered:

* host-symbol-collision build: bool / int64 / Timestamp / TimestampTz / H3Index
* undeclared ``text`` (a PG varlena): no pg_config.h in the parse, so the
  implicit-int rule turns text / text * / text ** into int / int * / int **,
  silently mistyping the IDL that every downstream binding (PyMEOS-CFFI, GoMEOS,
  MEOS.NET, JMEOS, MEOS.js) consumes.

These assert the recovered shapes survive and that genuinely-int functions are
left untouched. Plain unittest, no pytest dependency.

The IDL is generated, not committed; run ``python run.py`` first.

Schema note: a function's ``returnType`` is a ``{"c", "canonical"}`` dict and a
parameter is a ``{"name", "cType", "canonical"}`` dict.
"""
import json
import unittest
from pathlib import Path

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"


class TypeRecoverTests(unittest.TestCase):
    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        idl = json.loads(IDL.read_text())
        self.by_name = {f["name"]: f for f in idl["functions"]}

    def _ret(self, name):
        self.assertIn(name, self.by_name, f"{name} missing from IDL")
        return self.by_name[name]["returnType"]["c"]

    def _param_ctypes(self, name):
        self.assertIn(name, self.by_name, f"{name} missing from IDL")
        return [p["cType"] for p in self.by_name[name]["params"]]

    # ---- text (the undeclared-varlena collapse) ----------------------------

    def test_text_pointer_returns_recovered(self):
        # Pre-fix these came back as ``int *``.
        for name in ("cstring_to_text", "ttext_start_value", "text_copy",
                     "text_upper", "textset_end_value"):
            self.assertEqual(self._ret(name), "text *", name)

    def test_text_const_pointer_params_recovered(self):
        # ``const text *`` collapses to ``const int *``.
        self.assertIn("const text *", self._param_ctypes("text_to_cstring"))
        self.assertIn("const text *", self._param_ctypes("textcat_ttext_text"))

    def test_text_double_pointer_recovered(self):
        # ``text **`` collapses to ``int **``.
        self.assertIn("text **", self._param_ctypes("textset_make"))

    def test_no_text_left_collapsed_to_int(self):
        # Hard guard: a healthy IDL carries many text* slots. 0 means the
        # recoverer (or its text coverage) regressed.
        text_fns = [f for f in self.by_name.values()
                    if "text *" in json.dumps(f)]
        self.assertGreater(len(text_fns), 50,
                           "text* collapsed toward int — typerecover regression?")

    # ---- GSERIALIZED (the opaque PG geometry, collapses to int) ------------

    def test_gserialized_returns_recovered(self):
        # Pre-fix these geo-returning functions came back as ``int *``.
        for name in ("tcbuffer_convex_hull", "tcbuffer_traversed_area",
                     "geo_round"):
            self.assertEqual(self._ret(name), "GSERIALIZED *", name)

    def test_no_gserialized_left_collapsed_to_int(self):
        # Hard guard: a healthy IDL carries many GSERIALIZED* slots (geo
        # accessors/constructors). 0 means GSERIALIZED recovery regressed.
        geo_fns = [f for f in self.by_name.values()
                   if "GSERIALIZED *" in json.dumps(f)]
        self.assertGreater(len(geo_fns), 50,
                           "GSERIALIZED* collapsed toward int — typerecover regression?")

    # ---- jsonb Jsonb / jsonpath JsonPath (opaque PG types, collapse to int) -

    def test_jsonb_recovered_when_tjsonb_present(self):
        # The temporal-JSONB surface is built only when MEOS is compiled with
        # JSON=ON, so this assertion is conditional on the parsed source
        # carrying it (skipped otherwise to stay source-agnostic).
        jsonb_fns = [n for n in self.by_name
                     if "jsonb" in n.lower() or "tjsonb" in n.lower()]
        if not jsonb_fns:
            self.skipTest("source parsed without the JSON=ON tjsonb surface")
        # An out-parameter that pre-fix came back as ``int **``.
        self.assertIn("Jsonb **", self._param_ctypes("jsonbset_value_n"))
        carriers = [f for f in self.by_name.values() if "Jsonb *" in json.dumps(f)]
        self.assertGreater(len(carriers), 20,
                           "Jsonb* collapsed toward int — typerecover regression?")

    # ---- other PG-vendored opaque types (Interval / DateADT / Datum / ...) -

    def test_interval_params_recovered(self):
        # ``const Interval *`` collapses to ``const int *`` (e.g. duration args).
        self.assertIn("const Interval *", self._param_ctypes("temporal_tprecision"))
        self.assertIn("const Interval *", self._param_ctypes("temporal_tsample"))

    def test_other_vendored_pointer_types_recovered(self):
        # Hard guard: each PG-vendored opaque type carries many pointer slots in
        # a healthy IDL; 0 means that type's recovery regressed.
        for typ, floor in (("Interval *", 30), ("DateADT", 20),
                           ("GBOX *", 3), ("BOX3D *", 3)):
            hits = [f for f in self.by_name.values() if typ in json.dumps(f)]
            self.assertGreater(len(hits), floor,
                               f"{typ} collapsed toward int — typerecover regression?")

    # ---- the host-symbol-collision collapses (incl. pointer returns) -------

    def test_bool_and_pointer_returns_recovered(self):
        self.assertEqual(self._ret("temporal_eq"), "bool")          # scalar
        self.assertEqual(self._ret("tbool_values"), "bool *")       # pointer return
        self.assertEqual(self._ret("temporal_timestamps"), "TimestampTz *")
        self.assertEqual(self._ret("bigintset_values"), "int64_t *")
        self.assertEqual(self._ret("th3index_values"), "uint64_t *")

    def test_uint64_recovered(self):
        # The bare PG ``uint64`` typedef collapses to ``int`` and must recover
        # to ``uint64_t`` (like ``int64`` -> ``int64_t``), else 64-bit values
        # (hash seeds, quadbin cells) truncate to 32 bits in generated
        # bindings. These ``*_hash_extended`` functions use the bare ``uint64``
        # typedef (not the H3Index/Quadbin aliases), so they recover only when
        # the ``uint64`` map entry is present — a guard against dropping it.
        self.assertEqual(self._ret("set_hash_extended"), "uint64_t")
        self.assertEqual(self._ret("span_hash_extended"), "uint64_t")
        self.assertIn("uint64_t", self._param_ctypes("set_hash_extended"))

    def test_cell_id_canonical_normalized_uniform(self):
        # H3Index (libh3's typedef, whose fully-resolved canonical is the platform
        # "unsigned long") and Quadbin (MobilityDB's typedef, recovered to "uint64_t")
        # are BOTH uint64 cell ids; as Tcell<T> subtypes they must be spelled identically.
        # The ``canonical`` field must normalize to "uint64_t" for both, not leave H3Index
        # at "unsigned long" — a guard on the _CANON_ALIAS canonical-normalization pass.
        for name in ("th3index_start_value", "th3index_end_value",
                     "tquadbin_start_value", "tquadbin_end_value", "h3index_in"):
            rt = self.by_name[name]["returnType"]
            self.assertEqual(rt["c"], "uint64_t", f"{name} c")
            self.assertEqual(rt["canonical"], "uint64_t", f"{name} canonical")

    def test_typedef_canonical_not_platform_resolved(self):
        # ``canonical`` is the MEOS typedef its ``cType`` names, never libclang's
        # fully-resolved platform type. On the self-contained (installed-header)
        # parse ``TimestampTz`` resolves to ``long`` and ``Jsonb *`` / ``JsonPath
        # *`` to ``varlena *`` while ``cType`` keeps the typedef; normalize_canonical
        # re-derives ``canonical`` from the faithful ``cType`` so a binding
        # generator (which keys on ``canonical``) marshals the semantic type
        # instead of dropping the function — a guard on that pass.
        def canon(name, pname):
            self.assertIn(name, self.by_name, f"{name} missing from IDL")
            p = next(p for p in self.by_name[name]["params"] if p["name"] == pname)
            return (p["cType"], p["canonical"])
        self.assertEqual(canon("tint_value_at_timestamptz", "t"),
                         ("TimestampTz", "TimestampTz"))
        if "jsonb_path_exists" in self.by_name:  # JSON=ON-conditional surface
            self.assertEqual(canon("jsonb_path_exists", "jb"),
                             ("const Jsonb *", "const Jsonb *"))
            self.assertEqual(canon("jsonb_path_exists", "jp"),
                             ("const JsonPath *", "const JsonPath *"))

    # ---- genuine-int controls (must NOT be rewritten) ----------------------

    def test_genuine_int_left_untouched(self):
        # ``int`` is not a recoverable base name.
        self.assertEqual(self._ret("intspan_width"), "int")   # genuine scalar int
        self.assertEqual(self._ret("tint_values"), "int *")   # genuine int array


if __name__ == "__main__":
    unittest.main()
