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

    def test_uint32_recovered(self):
        # The bare PG ``uint32`` typedef collapses to ``int`` and must recover
        # to ``uint32_t`` (like ``uint64`` -> ``uint64_t``), else the unsigned
        # 32-bit hash return renders as a signed ``int`` in every generated
        # binding (values >= 2**31 flip sign). Every MEOS ``*_hash`` returns the
        # bare ``uint32`` typedef, so they recover only when the ``uint32`` map
        # entry is present — a guard against dropping it, the sibling of the
        # recurrently-lost ``uint64`` entry above.
        for name in ("set_hash", "span_hash", "spanset_hash",
                     "tbox_hash", "temporal_hash"):
            self.assertEqual(self._ret(name), "uint32_t", name)
        # Genuine ``int`` returns (e.g. numValues) must stay untouched: the
        # recovery only fires where the header text spells ``uint32``.
        self.assertEqual(self._ret("set_num_values"), "int")

    def test_uint32_canonical_normalized(self):
        # A uint32_t slot's fully-resolved ``canonical`` is the platform builtin
        # "unsigned int" (libclang) while ``cType`` keeps the typedef. It must
        # normalize to "uint32_t" so the width is spelled identically catalog-wide
        # and bindings (which key on ``canonical``) keep the *_hash surface instead
        # of dropping it -- the uint32 sibling of the uint64 canonical guard below.
        for name in ("temporal_hash", "set_hash", "span_hash", "tbox_hash"):
            rt = self.by_name[name]["returnType"]
            self.assertEqual(rt["c"], "uint32_t", f"{name} c")
            self.assertEqual(rt["canonical"], "uint32_t", f"{name} canonical")
        # No uint32 typedef slot is left as the platform "unsigned int" (genuine
        # ``unsigned int`` params such as PG ``Oid`` keep cType "unsigned int").
        bad = [f["name"] for f in self.by_name.values()
               for t in [f["returnType"]] + f.get("params", [])
               if t.get("canonical") == "unsigned int"
               and (t.get("c") or t.get("cType")) in ("uint32_t", "uint32")]
        self.assertEqual(bad, [], f"uint32 typedefs left as 'unsigned int': {bad}")

    def test_cell_id_canonical_normalized_uniform(self):
        # H3Index (libh3's typedef, whose fully-resolved canonical is the platform
        # "unsigned long"), Quadbin and S2CellId (MobilityDB's typedefs, recovered to
        # "uint64_t") are ALL uint64 cell ids; as Tcell<T> subtypes they must be spelled
        # identically. The ``canonical`` field must normalize to "uint64_t" for each, not
        # leave one at "unsigned long" — a guard on the _CANON_ALIAS canonical-normalization
        # pass.
        for name in ("th3index_start_value", "th3index_end_value",
                     "tquadbin_start_value", "tquadbin_end_value", "h3index_in",
                     "ts2cell_start_value", "ts2cell_end_value", "s2cell_in"):
            rt = self.by_name[name]["returnType"]
            self.assertEqual(rt["c"], "uint64_t", f"{name} c")
            self.assertEqual(rt["canonical"], "uint64_t", f"{name} canonical")
        # An array of cell ids is an array of by-value uint64_t, whichever cell
        # typedef the header spells it with.
        for name in ("th3index_values", "tquadbin_values", "ts2cell_values"):
            rt = self.by_name[name]["returnType"]
            self.assertEqual(rt["c"], "uint64_t *", f"{name} c")
            self.assertEqual(rt["canonical"], "uint64_t *", f"{name} canonical")

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

    # ---- the class: no typedef reads as a platform integer ------------------

    def test_no_typedef_reads_as_a_platform_integer(self):
        # A slot declared by a name (`int32`, `TimeADT`, `H3Index`) states that name's
        # definition, never the C integer libclang resolves it to on the host: `long`
        # is 64 bits on Linux and 32 on Windows, so a binding keying on it reads the
        # wrong width. The assertion runs over every slot, so a new typedef is held
        # to it without being named here.
        idl = json.loads(IDL.read_text())
        slots = [s for f in idl["functions"]
                 for s in [f["returnType"]] + f.get("params", [])]
        slots += [fl for st in idl.get("structs", []) for fl in st.get("fields", [])]
        bad = sorted({(_base(s.get("c") or s.get("cType")), _base(s.get("canonical")))
                      for s in slots
                      if _base(s.get("c") or s.get("cType")) not in _C_INTEGERS
                      and _base(s.get("canonical")) in _C_INTEGERS})
        self.assertEqual(bad, [], f"typedefs stated as a platform integer: {bad}")

    def test_postgres_and_standard_types_keep_their_definition(self):
        def canon(name, pname):
            p = next(p for p in self.by_name[name]["params"] if p["name"] == pname)
            return p["canonical"]
        # TimeADT is PostgreSQL's `time`, as DateADT is its `date`.
        self.assertEqual(canon("pg_time_out", "time"), "TimeADT")
        self.assertEqual(canon("date_to_timestamp", "date"), "DateADT")
        self.assertEqual(self.by_name["pg_time_in"]["returnType"]["canonical"], "TimeADT")
        # a width name reaches the C standard type PostgreSQL 18 defines it as
        self.assertEqual(canon("pg_time_in", "typmod"), "int32_t")
        self.assertEqual(canon("set_as_wkb", "variant"), "uint8_t")


# C's own integer names: what a typedef must never be stated as.
_C_INTEGERS = {
    "char", "signed char", "unsigned char", "short", "signed short", "unsigned short",
    "short int", "int", "signed", "signed int", "unsigned", "unsigned int", "long",
    "signed long", "unsigned long", "long int", "unsigned long int", "long long",
    "unsigned long long", "long long int", "unsigned long long int",
}


def _base(t):
    return " ".join(t.replace("const", " ").replace("struct", " ").replace("*", " ")
                    .split()) if t else ""


class ScalarSpellingTests(unittest.TestCase):
    """#scalar_spelling and #postgres_scalar_names of parser/typerecover.py, over
    typedef chains as the parser records them, one step each."""

    TYPEDEFS = {
        "int32": "int32_t", "int32_t": "__int32_t", "__int32_t": "int",
        "int64": "int64_t", "int64_t": "__int64_t", "__int64_t": "long",
        "uint64": "uint64_t", "uint64_t": "__uint64_t", "__uint64_t": "unsigned long",
        "Quadbin": "uint64", "H3Index": "uint64_t",
        "TimeADT": "int64", "DateADT": "int32", "Oid": "unsigned int",
        "Datum": "uintptr_t", "uintptr_t": "unsigned long",
        "float8": "double", "raw16": "signed short", "int16": "signed short",
        "MeosType": "enum MeosType", "Loop": "Loop",
    }
    # every scalar typedef pgtypes declares, its base types included
    PG = frozenset({"TimeADT", "DateADT", "Oid", "Datum", "int32", "int64", "uint64",
                    "float8", "int16"})

    def spell(self, name):
        from parser.typerecover import scalar_spelling
        return scalar_spelling(name, self.TYPEDEFS, self.PG)

    def test_a_width_name_reaches_its_standard_type(self):
        self.assertEqual(self.spell("int32"), "int32_t")
        self.assertEqual(self.spell("int64"), "int64_t")

    def test_a_cell_reaches_uint64_t_through_uint64(self):
        self.assertEqual(self.spell("Quadbin"), "uint64_t")
        self.assertEqual(self.spell("H3Index"), "uint64_t")

    def test_a_postgres_type_keeps_its_name(self):
        # as #test_typedef_canonical_not_platform_resolved holds TimestampTz
        self.assertEqual(self.spell("TimeADT"), "TimeADT")
        self.assertEqual(self.spell("DateADT"), "DateADT")
        self.assertEqual(self.spell("Oid"), "Oid")
        # a C standard type below it does not make Datum a width name
        self.assertEqual(self.spell("Datum"), "Datum")
        # `typedef signed short int16` names no <stdint.h> type, so the chain stops at
        # PostgreSQL's name rather than at the platform's short
        self.assertEqual(self.spell("int16"), "int16")

    def test_a_chain_ending_at_a_builtin_states_the_builtin(self):
        self.assertEqual(self.spell("float8"), "double")
        self.assertEqual(self.spell("raw16"), "signed short")

    def test_no_scalar_no_spelling(self):
        self.assertIsNone(self.spell("MeosType"))
        self.assertIsNone(self.spell("Loop"))
        self.assertIsNone(self.spell("int"))

    def test_the_postgres_names_are_every_scalar_typedef_wherever_it_sits(self):
        import tempfile
        from parser.typerecover import postgres_scalar_names
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            (root / "pg_basetypes.h").write_text(
                "typedef int32_t int32;\ntypedef double float8;\n"
                "#ifndef DATE_H\ntypedef int32 DateADT;\n#endif\n")
            (root / "datatype").mkdir()
            (root / "datatype" / "timestamp.h").write_text(
                "typedef int64 TimestampTz;\n/* typedef int64 Commented; */\n"
                "typedef struct varlena bytea;\ntypedef char *Pointer;\n")
            self.assertEqual(postgres_scalar_names(root),
                             {"int32", "float8", "DateADT", "TimestampTz"})

    def test_normalize_states_a_slot_by_its_chain(self):
        from parser.typerecover import normalize_canonical
        idl = {"_typedefs": dict(self.TYPEDEFS), "functions": [
            {"name": "pg_time_in", "returnType": {"c": "TimeADT", "canonical": "long"},
             "params": [{"name": "typmod", "cType": "int32", "canonical": "int"},
                        {"name": "cells", "cType": "const Quadbin *",
                         "canonical": "const unsigned long *"}]}]}
        idl, fixed = normalize_canonical(idl, self.PG)
        f = idl["functions"][0]
        self.assertEqual(f["returnType"]["canonical"], "TimeADT")
        self.assertEqual([p["canonical"] for p in f["params"]],
                         ["int32_t", "const uint64_t *"])
        self.assertEqual(fixed, 3)
        self.assertNotIn("_typedefs", idl)


if __name__ == "__main__":
    unittest.main()
