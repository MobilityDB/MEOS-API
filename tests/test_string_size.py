"""The size of every string a public MEOS function returns.

Every public function returning a `char *`, a string the caller frees, takes a `size_t *`
through which it writes the size of that string, its terminating null character included,
and the catalog lists that parameter in `shape.outParams`. A binding then copies the
string with that size, as it copies a WKB buffer, rather than scanning it for its
terminator. A `const char *` result is not the caller's to free and states no size.

The synthetic catalog is built by #fn, the builder of tests/test_codecs.py; the contract
test reads the generated catalog as #CodecContractTests of tests/test_codecs.py does,
which reads `shape.outParams` the same way. Plain unittest, no pytest dependency.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"


def _ctype(t):
    """A C type without its spaces, so `char *` and `char*` read alike."""
    return (t or "").replace(" ", "")


def unsized_strings(functions):
    """The public functions returning a `char *` whose `shape.outParams` lists no
    `size_t *` parameter, by name; `shape.outParams` is read as
    #test_every_trailing_input_is_named_valued_and_no_out_parameter of
    tests/test_codecs.py reads it."""
    bad = []
    for f in functions:
        if f.get("api") != "public" or _ctype(f["returnType"].get("canonical")) != "char*":
            continue
        out = set((f.get("shape") or {}).get("outParams") or ())
        if not any(p["name"] in out and _ctype(p.get("canonical")) == "size_t*"
                   for p in f["params"]):
            bad.append(f["name"])
    return sorted(bad)


def fn(name, ret, params, api="public", out=()):
    """A catalog function, the shape #fn of tests/test_codecs.py builds."""
    f = {"name": name, "api": api,
         "returnType": {"c": ret, "canonical": ret},
         "params": [{"name": n, "cType": t, "canonical": t} for t, n in params]}
    if out:
        f["shape"] = {"outParams": list(out)}
    return f


TEMP = "const Temporal *"


class UnsizedStringTests(unittest.TestCase):
    def test_a_string_with_its_size_out_parameter(self):
        self.assertEqual(unsized_strings([
            fn("tfloat_out", "char *",
               [(TEMP, "temp"), ("int", "maxdd"), ("size_t *", "size_out")],
               out=["size_out"])]), [])

    def test_a_string_without_a_size(self):
        self.assertEqual(unsized_strings([
            fn("tfloat_out", "char *", [(TEMP, "temp"), ("int", "maxdd")])]),
            ["tfloat_out"])

    def test_a_size_parameter_the_catalog_does_not_list_as_out(self):
        """A `size_t *` the catalog does not list in `shape.outParams` is an input to a
        binding, which then still scans the string."""
        self.assertEqual(unsized_strings([
            fn("tfloat_out", "char *",
               [(TEMP, "temp"), ("int", "maxdd"), ("size_t *", "size_out")])]),
            ["tfloat_out"])

    def test_an_out_parameter_that_is_no_size(self):
        self.assertEqual(unsized_strings([
            fn("temporal_dump", "char *",
               [(TEMP, "temp"), ("int *", "count")], out=["count"])]),
            ["temporal_dump"])

    def test_a_string_the_caller_does_not_free(self):
        self.assertEqual(unsized_strings([
            fn("meos_errno_name", "const char *", [("int", "err")])]), [])

    def test_an_internal_string(self):
        self.assertEqual(unsized_strings([
            fn("basetype_out", "char *", [("Datum", "value")], api="internal")]), [])


class StringSizeContractTests(unittest.TestCase):
    """Over the generated catalog, read as #CodecContractTests of tests/test_codecs.py
    reads it: every public function returning a string the caller frees lists a
    `size_t *` in `shape.outParams`."""

    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        self.functions = json.loads(IDL.read_text())["functions"]

    def test_every_public_string_states_its_size(self):
        self.assertEqual(unsized_strings(self.functions), [])

    def test_the_writers_a_binding_reads(self):
        """The check of #test_every_public_string_states_its_size runs over the writers
        a binding calls, never over an empty set."""
        names = {f["name"] for f in self.functions
                 if f.get("api") == "public"
                 and _ctype(f["returnType"].get("canonical")) == "char*"}
        for name in ("temporal_out", "temporal_as_mfjson", "geo_as_text", "span_out",
                     "temporal_as_hexwkb"):
            self.assertIn(name, names)


if __name__ == "__main__":
    unittest.main()
