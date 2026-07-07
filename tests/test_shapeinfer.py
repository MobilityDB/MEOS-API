"""Regression tests for parser/shapeinfer.py.

The inferer derives array-output shape from the C signatures, replacing the
hand-maintained meta stub.  The discriminator is the *count* parameter's form:

* a written-back out-array pairs with a by-pointer ``int *count`` (the callee
  fills the length) -> ``outputArrays`` + ``arrayReturn.lengthFrom``
* a read-only in-array pairs with a by-value ``int count`` -> left untouched

Plain unittest, no pytest dependency; fully synthetic IDL (no generated file).
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
        self.assertEqual(stats["outputArrays"], 1)

    def test_two_parallel_output_arrays(self):
        idl = {"functions": [_fn(
            "tfloat_value_time_split", "Temporal **",
            [("temp", "const Temporal *"), ("vsize", "double"),
             ("value_bins", "double **"), ("time_bins", "TimestampTz **"),
             ("count", "int *")])]}
        idl, _ = infer_shapes(idl)
        self.assertEqual(idl["functions"][0]["shape"]["outputArrays"],
                         [{"param": "value_bins"}, {"param": "time_bins"}])

    def test_input_array_with_value_count_untouched(self):
        # tsequence_make-style: ** input array carries its length BY VALUE
        idl = {"functions": [_fn(
            "tsequence_make", "TSequence *",
            [("instants", "const TInstant **"), ("count", "int"),
             ("lower_inc", "bool")])]}
        idl, stats = infer_shapes(idl)
        self.assertNotIn("shape", idl["functions"][0])
        self.assertEqual(stats["outputArrays"], 0)

    def test_nonconst_input_array_with_value_count_untouched(self):
        # tsequenceset_make_gaps-style: non-const ** but BY-VALUE count => input
        idl = {"functions": [_fn(
            "tsequenceset_make_gaps", "TSequenceSet *",
            [("instants", "TInstant **"), ("count", "int"),
             ("maxt", "const Interval *")])]}
        idl, stats = infer_shapes(idl)
        self.assertEqual(stats["outputArrays"], 0)
        self.assertNotIn("shape", idl["functions"][0])


if __name__ == "__main__":
    unittest.main()
