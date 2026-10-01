"""End-to-end integration test against a *built* libmeos.

Skipped unless ``MEOS_LIBRARY_PATH`` points at a loadable MEOS shared
library — so CI without a MEOS build still passes. Run it with:

    MEOS_LIBRARY_PATH=/usr/local/lib/libmeos.so python3 tests/test_engine_integration.py

It drives the exact path the server uses: a temporal value is read and
written through the reader and writer the catalog states for its SQL type,
each called with the trailing inputs the catalog states by name
(``tfloat_out(temp, maxdd=15)``), and bad input raises ``MeosError`` instead of
terminating the process (MEOS's default handler calls ``exit()``).
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

# A temporal value of each SQL type, read through the reader the catalog states
# for that type: `Temporal` serves twenty types and has no generic public reader.
_LITERAL_BY_TYPE = {
    "tbool": _TBOOL,
    "tint": "{1@2000-01-01, 2@2000-01-03, 1@2000-01-05}",
    "tfloat": "{1.5@2000-01-01, 3.5@2000-01-03, 1.5@2000-01-05}",
    "ttext": "{AA@2000-01-01, BB@2000-01-03, AA@2000-01-05}",
}

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
        cls.readers = (t.get("readers") or {}).get("text", {})
        cls.reader_aux = (t.get("readerAux") or {}).get("text", {})
        cls.writers = (t.get("writers") or {}).get("text", {})
        cls.writer_aux = (t.get("writerAux") or {}).get("text", {})

    def read(self, sqltype, literal):
        return self.eng.decode(self.readers[sqltype], literal,
                               _aux(self.reader_aux[sqltype]))

    def write(self, sqltype, handle):
        return self.eng.encode(self.writers[sqltype], handle,
                               _aux(self.writer_aux[sqltype]))

    def test_each_type_states_its_own_reader_and_writer(self):
        for sqltype in _LITERAL_BY_TYPE:
            self.assertEqual(self.readers[sqltype], sqltype + "_in")
            self.assertEqual(self.writers[sqltype], sqltype + "_out")
        self.assertEqual(_aux(self.writer_aux["tfloat"]), [("int", 15)])

    def test_decode_invoke_scalar(self):
        for sqltype, literal in _LITERAL_BY_TYPE.items():
            h = self.read(sqltype, literal)
            self.assertTrue(h, sqltype)
            n = self.eng.invoke("temporal_num_instants", [("ptr", h)], "int")
            self.assertEqual(n, 3, sqltype)

    def test_each_type_round_trips_through_its_own_writer(self):
        ob = self.write("tbool", self.read("tbool", _TBOOL))
        self.assertIn("@", ob)
        of = self.write("tfloat", self.read("tfloat", _TFLOAT))
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
        # geo_as_ewkt(const GSERIALIZED *, int maxdd) takes TWO arguments, so
        # maxdd must be passed: encode() builds argtypes from the aux it is
        # given, and calling a two-argument function with one argument leaves
        # maxdd reading whatever the register held. MEOS rejects it whenever
        # that junk is negative — measured failing 4 runs in 6, with a
        # different value each time, and passing 6 in 6 once maxdd is supplied.
        self.assertIn("POINT",
                      self.eng.encode("geo_as_ewkt", ptr, [("int", 15)]).upper())

    def test_input_array_builder_round_trip(self):
        # Temporal *temporal_merge_array(Temporal **temparr, int count):
        # a JSON list -> decoded element handles -> C array.
        h1 = self.eng.decode("tbool_in", "t@2000-01-01")
        h2 = self.eng.decode("tbool_in", "f@2000-01-03")
        merged = self.eng.invoke(
            "temporal_merge_array",
            [("ptrarray", [h1, h2]), ("int", 2)], "ptr")
        self.assertTrue(merged)
        out = self.write("tbool", merged)
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
