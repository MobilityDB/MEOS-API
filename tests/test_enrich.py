"""Unit tests for parser/enrich.py.

Runs without libclang or pytest:  python3 tests/test_enrich.py

The fixture uses the *canonical* C spellings libclang actually emits
(``struct Temporal *``, ``unsigned char``, ``int`` for booleans, enum
parameters), so the assertions double as a specification.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser.enrich import enrich_idl, classify_category, build_type_encodings


def fn(name, ret, *params):
    return {
        "name": name,
        "file": "meos.h",
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
    # An otherwise-exposable function living in the internal header: it must
    # be policy-excluded (api=internal), like the programmer Datum API.
    dict(fn("internal_op", "struct Temporal *", (T, "temp")),
         file="meos_internal.h"),
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
