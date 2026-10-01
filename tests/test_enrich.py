"""Unit tests for parser/enrich.py.

Runs without libclang or pytest:  python3 tests/test_enrich.py

The fixture uses the *canonical* C spellings libclang actually emits
(``struct Temporal *``, ``unsigned char``, ``int`` for booleans, enum
parameters), so the assertions double as a specification.
"""

import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser.enrich import (_aux_specs, build_type_encodings, classify_category,
                           enrich_idl)


def fn(name, ret, *params):
    return {
        "name": name,
        "file": "meos.h",
        # A public doxygen group: api derives from @ingroup, so a fixture
        # function is public unless given a meos_internal_* group (or none).
        "group": "meos_temporal",
        "returnType": {"c": ret, "canonical": ret},
        "params": [{"name": n, "cType": t, "canonical": t} for t, n in params],
    }


T = "const struct Temporal *"
FUNCTIONS = [
    fn("temporal_in", "struct Temporal *", ("const char *", "str")),
    fn("temporal_out", "char *", (T, "temp")),
    fn("temporal_from_mfjson", "struct Temporal *", ("const char *", "str")),
    fn("temporal_as_hexwkb", "char *",
       (T, "temp"), ("unsigned char", "variant"), ("int *", "size_out")),
    fn("bigintset_in", "struct Set *", ("const char *", "str")),
    fn("bigintset_out", "char *", ("const struct Set *", "set")),
    fn("temporal_eq", "int", (T, "temp1"), (T, "temp2")),
    fn("tpoint_speed", "struct Temporal *", (T, "temp")),
    fn("tjsonb_to_ttext", "struct Temporal *", (T, "temp")),
    fn("union_set_set", "struct Set *",
       ("const struct Set *", "s1"), ("const struct Set *", "s2")),
    fn("temporal_num_instants", "int", (T, "temp")),
    fn("tsequence_make", "struct TSequence *",
       ("struct TInstant **", "instants"), ("int", "count"),
       ("interpType", "interp")),
    fn("tjsonb_value_at_timestamptz", "int",
       (T, "temp"), ("long", "t"), ("int", "strict"),
       ("struct Jsonb **", "value")),
    fn("temporal_set_interp", "struct Temporal *",
       (T, "temp"), ("interpType", "interp")),
    fn("meos_initialize", "void"),
    fn("rtree_insert", "void",
       ("struct RTree *", "rtree"), ("void *", "box"), ("long", "id")),
    fn("temporal_timestamps", "int *", (T, "temp"), ("int *", "count")),
    # aux args: a defaultable formatting scalar (maxdd) is allowed and
    # defaulted; a semantic *type tag disqualifies the helper entirely.
    fn("box_in", "struct Box *", ("const char *", "str")),
    fn("box_out", "char *",
       ("const struct Box *", "box"), ("int", "maxdd")),
    fn("weird_in", "struct Weird *",
       ("const char *", "str"), ("int", "basetype")),
    # The byte codec: a reader of (bytes, length) and a writer of (value,
    # variant, *size_out). Box has a writer and no reader, so no byte codec.
    fn("temporal_from_wkb", "struct Temporal *",
       ("const uint8_t *", "wkb"), ("size_t", "size")),
    fn("temporal_as_wkb", "uint8_t *",
       (T, "temp"), ("uint8_t", "variant"), ("size_t *", "size_out")),
    fn("box_as_wkb", "uint8_t *",
       ("const struct Box *", "box"), ("uint8_t", "variant"),
       ("size_t *", "size_out")),
    # An otherwise-exposable function carrying an internal doxygen group: it
    # must be policy-excluded (api=internal), like the programmer Datum API.
    dict(fn("internal_op", "struct Temporal *", (T, "temp")),
         file="meos_internal.h", group="meos_internal_temporal"),
    # Scalar out-parameter accessor: bool f(.., int *result) — the value is
    # returned through the trailing out-param, the bool is a presence flag.
    fn("setspan_value_n", "int",
       ("const struct Set *", "s"), ("int", "n"), ("int *", "result")),
    # Opaque out-parameter accessor: bool f(.., Box **result) — the value
    # comes back as an opaque pointer, serialised via the type's encoder.
    fn("boxset_value_n", "int",
       ("const struct Set *", "s"), ("int", "n"),
       ("struct Box **", "result")),
    # Input-array builder: f(Elem **arr, int count) — the (array,count) pair
    # becomes one JSON-array wire param; the count is implicit.
    fn("temporal_merge_array", "struct Temporal *",
       ("struct Temporal **", "temparr"), ("int", "count")),
    # Array return: Elem **f(.., int *count) — a freshly-allocated element
    # array; the count out-param is implicit, result is a JSON array.
    fn("temporal_components", "struct Temporal **",
       (T, "temp"), ("int *", "count")),
]

STRUCTS = [{"name": n, "fields": []} for n in
           ("Temporal", "TSequence", "Set", "RTree", "Jsonb", "TInstant",
            "Box", "Weird")]
ENUMS = [{"name": "interpType", "values": []}]


def make_idl():
    return enrich_idl({
        "functions": [dict(f, returnType=dict(f["returnType"]),
                           params=[dict(p) for p in f["params"]])
                      for f in FUNCTIONS],
        "structs": [dict(s) for s in STRUCTS],
        "enums": [dict(e) for e in ENUMS],
    })


def by_name(idl):
    return {f["name"]: f for f in idl["functions"]}


class CategoryTests(unittest.TestCase):
    def test_categories(self):
        c = {f["name"]: classify_category(f) for f in FUNCTIONS}
        self.assertEqual(c["temporal_in"], "io")
        self.assertEqual(c["temporal_from_mfjson"], "io")
        self.assertEqual(c["temporal_as_hexwkb"], "io")
        self.assertEqual(c["temporal_eq"], "predicate")     # returns int
        self.assertEqual(c["tpoint_speed"], "transformation")
        self.assertEqual(c["tjsonb_to_ttext"], "conversion")
        self.assertEqual(c["union_set_set"], "setop")
        self.assertEqual(c["temporal_num_instants"], "accessor")
        self.assertEqual(c["tsequence_make"], "constructor")
        self.assertEqual(c["meos_initialize"], "lifecycle")
        self.assertEqual(c["rtree_insert"], "index")


class TypeEncodingTests(unittest.TestCase):
    def setUp(self):
        self.te = build_type_encodings(
            FUNCTIONS, {s["name"] for s in STRUCTS})

    def test_struct_prefix_stripped_and_round_trip(self):
        self.assertIn("Temporal", self.te)              # not "struct Temporal"
        # temporal_as_hexwkb is still excluded — its `size_out` is a pointer
        # (out-param), not a defaultable scalar — so no wkb encoder here.
        self.assertEqual(self.te["Temporal"]["encodings"],
                         ["mfjson", "text"])
        self.assertEqual(self.te["Temporal"]["in"], "temporal_in")
        self.assertEqual(self.te["Temporal"]["out"], "temporal_out")
        self.assertEqual(self.te["Set"]["in"], "bigintset_in")
        self.assertEqual(self.te["Set"]["out"], "bigintset_out")

    def test_defaultable_aux_accepted_type_tag_rejected(self):
        # box_out(box, int maxdd) qualifies; maxdd defaults to 15.
        self.assertEqual(self.te["Box"]["out"], "box_out")
        self.assertEqual(self.te["Box"]["out_aux"],
                         [{"name": "maxdd", "kind": "integer",
                           "default": 15}])
        self.assertEqual(self.te["Box"]["in"], "box_in")
        self.assertEqual(self.te["Box"]["in_aux"], [])
        # weird_in(str, int basetype): the *type tag disqualifies it, so
        # Weird gets no decoder at all.
        self.assertNotIn("Weird", self.te)

    def test_byte_codec_beside_the_wire_encodings(self):
        # the byte-codec twin of #test_struct_prefix_stripped_and_round_trip
        self.assertEqual(self.te["Temporal"]["bytes"],
                         {"decoder": "temporal_from_wkb",
                          "encoder": "temporal_as_wkb"})
        # the bytes are no wire string: the encodings stay the string forms
        self.assertEqual(self.te["Temporal"]["encodings"], ["mfjson", "text"])
        # a writer without a reader states no codec
        self.assertNotIn("bytes", self.te["Box"])
        self.assertNotIn("bytes", self.te["Set"])

    def test_no_primitive_or_intermediate_false_positives(self):
        self.assertNotIn("int", self.te)        # was a real false positive
        self.assertNotIn("char", self.te)
        self.assertNotIn("TSequence", self.te)  # builder-only type
        for k in self.te:
            self.assertNotIn("struct ", k)

    def test_struct_serialization_folded(self):
        s = {x["name"]: x for x in make_idl()["structs"]}
        self.assertIn("serialization", s["Temporal"])
        self.assertNotIn("serialization", s["TSequence"])


class ExposabilityTests(unittest.TestCase):
    def setUp(self):
        self.fns = by_name(make_idl())

    def n(self, name):
        return self.fns[name]["network"]

    def test_int_returning_predicate_exposable(self):
        self.assertTrue(self.n("temporal_eq")["exposable"])
        self.assertEqual(self.fns["temporal_eq"]["wire"]["result"],
                         {"kind": "json", "json": "integer"})

    def test_serialized_round_trip(self):
        w = self.fns["tpoint_speed"]["wire"]
        self.assertTrue(self.n("tpoint_speed")["exposable"])
        self.assertEqual(w["params"][0]["kind"], "serialized")
        self.assertEqual(w["params"][0]["decode"], "temporal_in")
        self.assertEqual(w["result"]["encode"], "temporal_out")

    def test_enum_param_is_scalar_and_exposable(self):
        f = self.fns["temporal_set_interp"]
        self.assertTrue(f["network"]["exposable"])
        self.assertEqual(f["wire"]["params"][1],
                         {"name": "interp", "kind": "json",
                          "json": "string", "enum": "interpType"})

    def test_io_parse_serialize_exposable(self):
        for name in ("temporal_in", "temporal_out", "temporal_from_mfjson",
                     "bigintset_in", "bigintset_out"):
            self.assertTrue(self.n(name)["exposable"], name)

    def test_out_param_not_exposable(self):
        r = self.n("temporal_as_hexwkb")["reason"]
        self.assertFalse(self.n("temporal_as_hexwkb")["exposable"])
        self.assertIn("array-or-out-param:size_out", r)
        r2 = self.n("tjsonb_value_at_timestamptz")["reason"]
        self.assertIn("array-or-out-param:value", r2)

    def test_array_param_and_missing_encoder(self):
        r = self.n("tsequence_make")["reason"]
        self.assertFalse(self.n("tsequence_make")["exposable"])
        self.assertIn("array-or-out-param:instants", r)
        self.assertIn("no-encoder:TSequence", r)

    def test_array_return_not_exposable(self):
        r = self.n("temporal_timestamps")["reason"]
        self.assertFalse(self.n("temporal_timestamps")["exposable"])
        self.assertIn("unsupported-return:int *", r)

    def test_lifecycle_and_index_not_exposable(self):
        self.assertIn("lifecycle", self.n("meos_initialize")["reason"])
        self.assertIn("index", self.n("rtree_insert")["reason"])


class ValueTypeTests(unittest.TestCase):
    """A PostgreSQL type passed by value (`TimestampTz`, `DateADT`) registers a codec
    from its own in/out functions, as a pointer type does in #TypeEncodingTests, and a
    function taking or returning it is exposable."""

    def setUp(self):
        idl = {"functions": [
            fn("timestamptz_in", "TimestampTz", ("const char *", "str"),
               ("int32_t", "typmod")),
            fn("timestamptz_out", "char *", ("TimestampTz", "tstz")),
            fn("timetz_in", "TimeTzADT *", ("const char *", "str"),
               ("int32_t", "typmod")),
            fn("pg_timetz_in", "TimeTzADT *", ("const char *", "str"),
               ("int32_t", "typmod")),
            fn("timetz_out", "char *", ("const TimeTzADT *", "timetz")),
            fn("pg_timetz_out", "char *", ("const TimeTzADT *", "timetz")),
            fn("temporal_start_timestamptz", "TimestampTz", (T, "temp")),
            fn("tbool_at_timestamptz", "struct Temporal *", (T, "temp"),
               ("TimestampTz", "t")),
            fn("tbool_in", "struct Temporal *", ("const char *", "str")),
            fn("temporal_out", "char *", (T, "temp")),
            fn("datum_hash", "uint32_t", ("Datum", "d")),
        ], "structs": [{"name": "Temporal", "fields": []}], "enums": []}
        self.idl = enrich_idl(idl)
        self.te = self.idl["typeEncodings"]
        self.fns = by_name(self.idl)

    def test_a_value_type_reads_and_writes_through_its_functions(self):
        self.assertEqual((self.te["TimestampTz"]["in"], self.te["TimestampTz"]["out"]),
                         ("timestamptz_in", "timestamptz_out"))

    def test_a_value_parameter_and_result_are_on_the_wire(self):
        self.assertTrue(self.fns["tbool_at_timestamptz"]["network"]["exposable"])
        self.assertEqual(self.fns["tbool_at_timestamptz"]["wire"]["params"][1]["decode"],
                         "timestamptz_in")
        self.assertEqual(self.fns["temporal_start_timestamptz"]["wire"]["result"]["encode"],
                         "timestamptz_out")

    def test_the_postgresql_spelling_yields_to_the_meos_name(self):
        self.assertEqual((self.te["TimeTzADT"]["in"], self.te["TimeTzADT"]["out"]),
                         ("timetz_in", "timetz_out"))

    def test_a_value_type_without_a_codec_registers_nothing(self):
        self.assertNotIn("Datum", self.te)
        self.assertEqual(self.fns["datum_hash"]["network"]["reason"], "no-decoder:Datum")


class StandardIntegerTests(unittest.TestCase):
    """An integer the catalog states by its C standard name (``int64_t``, ``uint8_t``),
    as #normalize_canonical of parser/typerecover.py states every integer typedef,
    reads as an integer, as the builtin spellings in #ExposabilityTests do."""

    def setUp(self):
        self.fns = by_name(enrich_idl({
            "functions": [
                fn("bigint_to_set", "struct Set *", ("int64_t", "i")),
                fn("set_round", "struct Set *",
                   ("const struct Set *", "s"), ("int32_t", "maxdd")),
                fn("set_hash", "uint32_t", ("const struct Set *", "s")),
                fn("bigintset_in", "struct Set *", ("const char *", "str")),
                fn("bigintset_out", "char *", ("const struct Set *", "set")),
            ],
            "structs": [{"name": "Set", "fields": []}],
            "enums": [],
        }))

    def test_a_standard_integer_parameter_is_a_json_integer(self):
        f = self.fns["bigint_to_set"]
        self.assertEqual(f["wire"]["params"][0],
                         {"name": "i", "kind": "json", "json": "integer"})
        self.assertTrue(f["network"]["exposable"])
        self.assertEqual(self.fns["set_round"]["wire"]["params"][1]["json"], "integer")

    def test_a_standard_integer_result_is_a_json_integer(self):
        self.assertEqual(self.fns["set_hash"]["wire"]["result"],
                         {"kind": "json", "json": "integer"})


class CallLiteralDefaultTests(unittest.TestCase):
    """A trailing input whose name MEOS's calls pass one literal for, as
    #attach_call_literals of parser/boundargs.py reads it, defaults to that literal;
    a macro name keeps the default #TypeEncodingTests states."""

    def _aux(self, ctype, name, literal):
        p = {"name": name, "cType": ctype, "canonical": ctype, "_callLiteral": literal}
        return _aux_specs([p])[0]["default"]

    def test_the_literal_is_the_default(self):
        self.assertEqual(self._aux("int32_t", "typmod", "-1"), -1)
        self.assertIsNone(self._aux("const char *", "srs", "NULL"))
        self.assertIs(self._aux("bool", "with_bbox", "true"), True)

    def test_a_macro_or_a_literal_of_another_kind_keeps_the_default(self):
        self.assertEqual(self._aux("int", "maxdd", "OUT_DEFAULT_DECIMAL_DIGITS"), 15)
        self.assertEqual(self._aux("int", "option", "NULL"), 0)


class CallLiteralCatalogTests(unittest.TestCase):
    """Over the generated catalog, the PostgreSQL readers taking a type modifier read
    with -1, the modifier MEOS's own calls pass, as #CallLiteralDefaultTests states."""

    def test_a_type_modifier_reads_minus_one(self):
        idl_path = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"
        if not idl_path.exists():
            self.skipTest(f"{idl_path} not generated; run `python run.py` first")
        te = json.loads(idl_path.read_text())["typeEncodings"]
        for cls in ("Interval", "TimeTzADT", "NumericData"):
            self.assertEqual(te[cls]["in_aux"],
                             [{"name": "typmod", "kind": "integer", "default": -1}], cls)


class ValueTypeCatalogTests(unittest.TestCase):
    """Over the generated catalog, PostgreSQL's time types read and write through the
    MEOS functions #ValueTypeTests states, never their `pg_` spelling."""

    def test_the_postgresql_time_types_read_through_their_meos_functions(self):
        idl_path = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"
        if not idl_path.exists():
            self.skipTest(f"{idl_path} not generated; run `python run.py` first")
        te = json.loads(idl_path.read_text())["typeEncodings"]
        self.assertEqual({c: (te[c]["in"], te[c]["out"]) for c in
                          ("TimestampTz", "Timestamp", "TimeADT", "DateADT", "TimeTzADT")},
                         {"TimestampTz": ("timestamptz_in", "timestamptz_out"),
                          "Timestamp": ("timestamp_in", "timestamp_out"),
                          "TimeADT": ("time_in", "time_out"),
                          "DateADT": ("date_in", "date_out"),
                          "TimeTzADT": ("timetz_in", "timetz_out")})


class ApiClassificationTests(unittest.TestCase):
    def setUp(self):
        self.fns = by_name(make_idl())

    def test_internal_policy_excluded(self):
        f = self.fns["internal_op"]
        self.assertEqual(f["api"], "internal")
        self.assertFalse(f["network"]["exposable"])
        self.assertIn("internal", f["network"]["reason"])

    def test_public_default(self):
        self.assertEqual(self.fns["temporal_eq"]["api"], "public")
        self.assertTrue(self.fns["temporal_eq"]["network"]["exposable"])

    def test_no_group_is_internal(self):
        # A function with no doxygen @ingroup is internal (not documented,
        # not part of the public surface), even in a public header.
        idl = enrich_idl({
            "functions": [{k: v for k, v in fn("ungrouped_op", "int",
                                               (T, "temp")).items()
                           if k != "group"}],
            "structs": [dict(s) for s in STRUCTS], "enums": [],
        })
        self.assertEqual(idl["functions"][0]["api"], "internal")

    def test_scalar_outparam_projected_as_result(self):
        f = self.fns["setspan_value_n"]
        self.assertTrue(f["network"]["exposable"])
        pnames = [p["name"] for p in f["wire"]["params"]]
        self.assertEqual(pnames, ["s", "n"])          # 'result' not a param
        r = f["wire"]["result"]
        self.assertEqual(r["kind"], "json")
        self.assertEqual(r["json"], "integer")
        self.assertEqual(r["from_outparam"], "result")
        self.assertTrue(r["presence_return"])         # int return = presence

    def test_opaque_outparam_projected_as_serialized(self):
        f = self.fns["boxset_value_n"]
        self.assertTrue(f["network"]["exposable"])
        self.assertEqual([p["name"] for p in f["wire"]["params"]], ["s", "n"])
        r = f["wire"]["result"]
        self.assertEqual(r["kind"], "serialized")     # opaque -> encoded
        self.assertEqual(r["encode"], "box_out")
        self.assertEqual(r["from_outparam"], "result")
        self.assertTrue(r["presence_return"])

    def test_array_return(self):
        f = self.fns["temporal_components"]
        self.assertTrue(f["network"]["exposable"])
        self.assertEqual([p["name"] for p in f["wire"]["params"]], ["temp"])
        r = f["wire"]["result"]
        self.assertEqual(r["kind"], "array")
        self.assertEqual(r["count_outparam"], "count")
        self.assertEqual(r["element"]["kind"], "serialized")
        self.assertEqual(r["element"]["encode"], "temporal_out")

    def test_input_array_builder(self):
        f = self.fns["temporal_merge_array"]
        self.assertTrue(f["network"]["exposable"])
        params = f["wire"]["params"]
        self.assertEqual(len(params), 1)              # count is implicit
        a = params[0]
        self.assertEqual(a["name"], "temparr")
        self.assertEqual(a["kind"], "array")
        self.assertEqual(a["count_param"], "count")
        self.assertEqual(a["element"]["kind"], "serialized")
        self.assertEqual(a["element"]["decode"], "temporal_in")
        self.assertEqual(f["wire"]["result"]["kind"], "serialized")


class SummaryTests(unittest.TestCase):
    def test_enrichment_summary(self):
        e = make_idl()["enrichment"]
        self.assertEqual(sum(e["categoryCounts"].values()), len(FUNCTIONS))
        self.assertEqual(e["internalFunctions"], 1)        # internal_op
        self.assertEqual(e["publicFunctions"], len(FUNCTIONS) - 1)
        # 13 + setspan_value_n + boxset_value_n + temporal_merge_array
        # + temporal_components (array return); internal_op excluded.
        self.assertEqual(e["exposableFunctions"], 17)


if __name__ == "__main__":
    unittest.main(verbosity=2)
