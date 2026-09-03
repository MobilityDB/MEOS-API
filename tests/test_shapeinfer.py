"""Regression tests for parser/shapeinfer.py.

The inferer derives array-output shape from the C signatures, replacing the
hand-maintained meta stub.  The discriminator is the *count* parameter's form:

* a written-back out-array pairs with a by-pointer ``int *count`` (the callee
  fills the length) -> ``outputArrays`` + ``arrayReturn.lengthFrom``
* a read-only in-array pairs with a by-value ``int count`` -> ``inputArrays``

Plain unittest, no pytest dependency; fully synthetic IDL, no build artifacts.
"""
import unittest

from parser.shapeinfer import infer_shapes


def _fn(name, ret, params):
    return {"name": name,
            "returnType": {"c": ret, "canonical": ret},
            "params": [{"name": n, "cType": t, "canonical": t} for n, t in params]}


class ShapeInferTests(unittest.TestCase):
    def test_output_array_with_pointer_count(self):
        # temporal_time_split-style: non-const ** out-array + by-pointer count
        idl = {"functions": [_fn(
            "temporal_time_split", "Temporal **",
            [("temp", "const Temporal *"), ("duration", "const Interval *"),
             ("torigin", "TimestampTz"), ("time_bins", "TimestampTz **"),
             ("count", "int *")])]}
        idl, stats = infer_shapes(idl)
        sh = idl["functions"][0]["shape"]
        self.assertEqual(sh["outputArrays"], [{"param": "time_bins"}])
        self.assertEqual(sh["arrayReturn"]["lengthFrom"],
                         {"kind": "param", "name": "count"})
        # element = return with one pointer level stripped (array of pointers)
        self.assertEqual(sh["arrayReturn"]["element"],
                         {"c": "Temporal *", "canonical": "Temporal *"})
        self.assertEqual(stats["outputArrays"], 1)

    def test_scalar_array_return_element_is_by_value(self):
        # floatset_values-style: ``double *`` return + by-pointer count.  The
        # element strips one pointer level to the by-value scalar ``double`` so a
        # binding composes native-list-of-double with zero return-string parsing.
        idl = {"functions": [_fn(
            "floatset_values", "double *",
            [("s", "const Set *"), ("count", "int *")])]}
        idl, stats = infer_shapes(idl)
        ar = idl["functions"][0]["shape"]["arrayReturn"]
        self.assertEqual(ar["lengthFrom"], {"kind": "param", "name": "count"})
        self.assertEqual(ar["element"], {"c": "double", "canonical": "double"})
        self.assertEqual(stats["arrayReturn"], 1)

    def test_two_parallel_output_arrays(self):
        idl = {"functions": [_fn(
            "tfloat_value_time_split", "Temporal **",
            [("temp", "const Temporal *"), ("vsize", "double"),
             ("value_bins", "double **"), ("time_bins", "TimestampTz **"),
             ("count", "int *")])]}
        idl, _ = infer_shapes(idl)
        self.assertEqual(idl["functions"][0]["shape"]["outputArrays"],
                         [{"param": "value_bins"}, {"param": "time_bins"}])

    def test_index_pair_return_records_its_group_size(self):
        # edwithin_tgeoarr_tgeoarr-style: the NxN kernels answer "which element of
        # one array relates to which of the other", so `count` is a number of index
        # PAIRS and the `int *` return holds 2 * count ints, [i0, j0, i1, j1, ...].
        # Without the group size a consumer reads `count` ints and gets half.
        idl = {"functions": [_fn(
            "edwithin_tgeoarr_tgeoarr", "int *",
            [("arr1", "const Temporal **"), ("count1", "int"),
             ("arr2", "const Temporal **"), ("count2", "int"),
             ("dist", "double"), ("count", "int *")])]}
        idl, _ = infer_shapes(idl)
        ar = idl["functions"][0]["shape"]["arrayReturn"]
        self.assertEqual(ar["lengthFrom"], {"kind": "param", "name": "count"})
        self.assertEqual(ar["element"], {"c": "int", "canonical": "int"})
        self.assertEqual(ar["groupSize"], 2)

    def test_ordinary_array_return_records_no_group_size(self):
        # A scalar array return counts its own elements, so it carries no group
        # size and a binding reads `lengthFrom` elements unchanged.
        idl = {"functions": [_fn(
            "floatset_values", "double *",
            [("s", "const Set *"), ("count", "int *")])]}
        idl, _ = infer_shapes(idl)
        self.assertNotIn("groupSize", idl["functions"][0]["shape"]["arrayReturn"])

    def test_index_pair_rule_needs_an_array_argument(self):
        # The factor comes from the NxN shape, not from returning `int *`: a
        # function with no (TYPE **, int) argument pair counts its own ints.
        idl = {"functions": [_fn(
            "some_int_array_fn", "int *",
            [("temp", "const Temporal *"), ("count", "int *")])]}
        idl, _ = infer_shapes(idl)
        self.assertNotIn("groupSize", idl["functions"][0]["shape"]["arrayReturn"])

    def test_input_array_with_value_count_is_read_not_written(self):
        # tsequence_make-style: ** input array carries its length BY VALUE
        idl = {"functions": [_fn(
            "tsequence_make", "TSequence *",
            [("instants", "const TInstant **"), ("count", "int"),
             ("lower_inc", "bool")])]}
        idl, stats = infer_shapes(idl)
        shape = idl["functions"][0]["shape"]
        self.assertEqual(stats["outputArrays"], 0)
        self.assertNotIn("outputArrays", shape)
        self.assertEqual(shape["inputArrays"], [{
            "param": "instants",
            "lengthFrom": {"kind": "param", "name": "count"},
            "element": {"c": "TInstant *", "canonical": "TInstant *"}}])

    def test_nonconst_input_array_with_value_count_is_read_not_written(self):
        # tsequenceset_make_gaps-style: non-const ** but BY-VALUE count => input
        idl = {"functions": [_fn(
            "tsequenceset_make_gaps", "TSequenceSet *",
            [("instants", "TInstant **"), ("count", "int"),
             ("maxt", "const Interval *")])]}
        idl, stats = infer_shapes(idl)
        self.assertEqual(stats["outputArrays"], 0)
        self.assertEqual(
            idl["functions"][0]["shape"]["inputArrays"][0]["param"], "instants")

    def test_a_length_is_read_by_position_not_by_its_name(self):
        # The names disagree across the surface — `ngeoms`, `keys_len`, `size`
        # — and each is the length of the array before it.
        idl = {"functions": [
            _fn("geo_cluster_kmeans", "int *",
                [("geoms", "const GSERIALIZED **"), ("ngeoms", "uint32_t"),
                 ("k", "uint32_t"), ("count", "int *")]),
            _fn("jsonb_delete_array", "Jsonb *",
                [("jb", "const Jsonb *"), ("keys_elems", "text **"),
                 ("keys_len", "int")]),
            _fn("set_from_wkb", "Set *",
                [("wkb", "const uint8_t *"), ("size", "size_t")]),
        ]}
        idl, stats = infer_shapes(idl)
        lengths = {f["name"]: f["shape"]["inputArrays"][0]["lengthFrom"]["name"]
                   for f in idl["functions"]}
        self.assertEqual(lengths, {"geo_cluster_kmeans": "ngeoms",
                                   "jsonb_delete_array": "keys_len",
                                   "set_from_wkb": "size"})
        self.assertEqual(stats["inputArrays"], 3)
        # The byte buffer's element is the scalar itself, not a pointer to one.
        self.assertEqual(
            idl["functions"][2]["shape"]["inputArrays"][0]["element"]["c"],
            "uint8_t")

    def test_a_value_beside_a_number_is_not_an_array(self):
        # `text_left(text *txt, int n)` takes ONE text and a character count;
        # a pointer to a MEOS value type beside an integer says nothing about
        # an array, and only a pointer to a C scalar does.
        idl = {"functions": [
            _fn("text_left", "text *", [("txt", "text *"), ("n", "int")]),
            _fn("jsonb_hash_extended", "uint64_t",
                [("jb", "const Jsonb *"), ("seed", "uint64_t")]),
            _fn("interval_in", "Interval *",
                [("str", "const char *"), ("typmod", "int32")]),
        ]}
        idl, stats = infer_shapes(idl)
        self.assertEqual(stats["inputArrays"], 0)
        for f in idl["functions"]:
            self.assertNotIn("inputArrays", f.get("shape", {}))

    def test_an_out_array_is_not_read_as_an_input_one(self):
        # `jsonb_each(jb, Jsonb **values, int *count)` writes `values` back, and
        # its length being BY POINTER is what says so.
        idl = {"functions": [_fn(
            "jsonb_each", "text **",
            [("jb", "const Jsonb *"), ("values", "Jsonb **"),
             ("count", "int *")])]}
        idl, stats = infer_shapes(idl)
        shape = idl["functions"][0]["shape"]
        self.assertEqual(stats["inputArrays"], 0)
        self.assertNotIn("inputArrays", shape)
        self.assertEqual(shape["outputArrays"], [{"param": "values"}])


if __name__ == "__main__":
    unittest.main()
