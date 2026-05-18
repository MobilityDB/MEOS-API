"""End-to-end integration test against a *built* libmeos.

Skipped unless ``MEOS_LIBRARY_PATH`` points at a loadable MEOS shared
library — so CI without a MEOS build still passes. Run it with:

    MEOS_LIBRARY_PATH=/usr/local/lib/libmeos.so python3 tests/test_engine_integration.py

It drives the exact path the server uses, including the catalog's
``in_aux``/``out_aux`` defaults (so the *generic* ``temporal_out(temp,
maxdd=15)`` is called correctly — proving it serialises any subtype), and
asserts that bad input raises ``MeosError`` instead of terminating the
process (MEOS's default handler calls ``exit()``).
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.engine import CtypesEngine, MeosError

_LIB = os.environ.get("MEOS_LIBRARY_PATH")
_HAVE = bool(_LIB) and Path(_LIB).exists()
_CATALOG = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"
_TBOOL = "{t@2000-01-01, f@2000-01-03, t@2000-01-05}"
_TFLOAT = "{1.5@2000-01-01, 3.5@2000-01-03}"

_KIND_TAG = {"integer": "int", "number": "double",
             "boolean": "bool", "string": "str"}


def _aux(specs):
    return [(_KIND_TAG.get(a["kind"], "str"), a["default"]) for a in specs]


@unittest.skipUnless(_HAVE, "set MEOS_LIBRARY_PATH to a built libmeos.so")
class CtypesIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.eng = CtypesEngine(_LIB)
        te = (json.loads(_CATALOG.read_text()).get("typeEncodings", {})
              if _CATALOG.exists() else {})
        t = te.get("Temporal", {})
        cls.tin = t.get("in", "tbool_in")
        cls.tout = t.get("out", "tbool_out")
        cls.in_aux = _aux(t.get("in_aux", []))
        cls.out_aux = _aux(t.get("out_aux", []))

    def test_catalog_selected_in_out(self):
        # Decoding stays a typed wrapper (subtype-narrow); encoding is the
        # generic temporal_out with a defaulted maxdd.
        self.assertEqual(self.tin, "tbool_in")
        self.assertEqual(self.tout, "temporal_out")
        self.assertEqual(self.out_aux, [("int", 15)])

    def test_decode_invoke_scalar(self):
        h = self.eng.decode(self.tin, _TBOOL, self.in_aux)
        self.assertTrue(h)
        n = self.eng.invoke("temporal_num_instants", [("ptr", h)], "int")
        self.assertEqual(n, 3)

    def test_generic_encoder_round_trips_any_subtype(self):
        # The whole point of the gap fix: temporal_out(+maxdd) serialises
        # a tbool AND a tfloat — a subtype-narrow tbool_out could not.
        hb = self.eng.decode("tbool_in", _TBOOL)
        ob = self.eng.encode(self.tout, hb, self.out_aux)
        self.assertIn("@", ob)
        hf = self.eng.decode("tfloat_in", _TFLOAT)
        of = self.eng.encode(self.tout, hf, self.out_aux)
        self.assertIn("@", of)
        self.assertIn("1.5", of)

    def test_scalar_outparam_round_trip(self):
        # bool floatset_value_n(const Set *, int n, double *result):
        # the value comes back through the byref out-parameter.
        h = self.eng.decode("floatset_in", "{1.0, 2.5, 3.0}")
        # MEOS *_value_n is 1-based: n=2 -> the second element.
        present, val = self.eng.invoke_outparam(
            "floatset_value_n", [("ptr", h), ("int", 2)], "double *", True)
        self.assertTrue(present)
        self.assertAlmostEqual(val, 2.5, places=6)
        # out-of-range index -> presence False, no value
        present2, _ = self.eng.invoke_outparam(
            "floatset_value_n", [("ptr", h), ("int", 99)], "double *", True)
        self.assertFalse(present2)

    def test_opaque_outparam_round_trip(self):
        # bool geoset_value_n(const Set *, int n, GSERIALIZED **result):
        # the opaque pointer comes back via byref and is then encoded.
        h = self.eng.decode("geomset_in", "{Point(1 1), Point(2 2)}")
        present, ptr = self.eng.invoke_outparam(
            "geoset_value_n", [("ptr", h), ("int", 1)], "GSERIALIZED **",
            True)
        self.assertTrue(present)
        self.assertTrue(ptr)
        self.assertIn("POINT", self.eng.encode("geo_as_ewkt", ptr).upper())

    def test_input_array_builder_round_trip(self):
        # Temporal *temporal_merge_array(Temporal **temparr, int count):
        # a JSON list -> decoded element handles -> C array.
        h1 = self.eng.decode("tbool_in", "t@2000-01-01")
        h2 = self.eng.decode("tbool_in", "f@2000-01-03")
        merged = self.eng.invoke(
            "temporal_merge_array",
            [("ptrarray", [h1, h2]), ("int", 2)], "ptr")
        self.assertTrue(merged)
        out = self.eng.encode(self.tout, merged, self.out_aux)
        self.assertIn("@", out)
        self.assertIn("2000-01-03", out)        # both instants merged in

    def test_array_return_round_trip(self):
        # TSequence **temporal_sequences(const Temporal *, int *count):
        # MEOS allocates the array; engine returns the element handles.
        h = self.eng.decode(
            "tbool_in", "{[t@2000-01-01, f@2000-01-03], [t@2000-01-05]}")
        ptrs = self.eng.invoke_array("temporal_sequences", [("ptr", h)])
        self.assertEqual(len(ptrs), 2)          # two composing sequences
        outs = [self.eng.encode("tsequence_out", p, [("int", 15)])
                for p in ptrs]
        self.assertTrue(all("@" in o for o in outs))

    def test_bad_input_raises_not_exits(self):
        with self.assertRaises(MeosError):
            self.eng.decode("tbool_in", "not a temporal value at all")


if __name__ == "__main__":
    unittest.main(verbosity=2)
