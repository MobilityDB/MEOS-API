"""The codec each class states: its readers and writers per SQL type, its hex-WKB writer
with the variant its type binds, and every trailing input by name with its value.

The synthetic catalog follows the shape #ExposabilityTests of tests/test_enrich.py builds
(``fn`` with ``c`` / ``cType`` / ``canonical``), carrying the SQL signatures and bound
literals the pass reads; the contract tests read the generated catalog, as
#TypeRecoverTests of tests/test_typerecover.py does. Plain unittest, no pytest dependency.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser.codecs import state_type_encodings

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"
SET, RAST = "const Set *", "const Raster *"


def fn(name, ret, params, sigs=(), api="public", shape=None, typedef=None):
    f = {"name": name, "api": api,
         "returnType": {"c": ret, "canonical": ret},
         "params": [{"name": n, "cType": t, "canonical": t,
                     **({"typedef": typedef} if typedef and t == "uint64_t" else {})}
                    for t, n in params],
         "sqlSignatures": list(sigs)}
    if typedef and ret == "uint64_t":
        f["returnType"]["typedef"] = typedef
    if shape:
        f["shape"] = shape
    return f


def sig(name, args, ret, **kw):
    return {"sqlName": name, "args": args, "ret": ret, **kw}


FUNCTIONS = [
    # one reader and one writer per set type, keyed by the SQL type each serves
    fn("intset_in", "Set *", [("const char *", "str")],
       [sig("intset_in", ["cstring"], "intset")]),
    fn("floatset_in", "Set *", [("const char *", "str")],
       [sig("floatset_in", ["cstring"], "floatset")]),
    fn("intset_out", "char *", [(SET, "s")],
       [sig("intset_out", ["intset"], "cstring"), sig("asText", ["intset"], "text")]),
    fn("floatset_out", "char *", [(SET, "s"), ("int", "maxdd")],
       [sig("floatset_out", ["floatset"], "cstring")]),
    # a generic writer serving several types loses to each type's own
    fn("spatialset_out", "char *", [(SET, "s"), ("int", "maxdd")],
       [sig("geomset_out", ["geomset"], "cstring"), sig("intset_out", ["intset"], "cstring")]),
    # the generic reader of one internal programmer surface is no codec
    fn("set_in", "Set *", [("const char *", "str")],
       [sig("intset_in", ["cstring"], "intset")], api="internal"),
    # the hex-WKB pair, its size an out-parameter, its variant what send binds
    fn("set_from_hexwkb", "Set *", [("const char *", "hexwkb")],
       [sig("intsetFromHexWKB", ["text"], "intset"),
        sig("floatsetFromHexWKB", ["text"], "floatset")]),
    fn("set_as_hexwkb", "char *",
       [(SET, "s"), ("uint8_t", "variant"), ("size_t *", "size_out")],
       [sig("asHexWKB", ["intset", "text"], "text")], shape={"outParams": ["size_out"]}),
    fn("set_as_wkb", "uint8_t *",
       [(SET, "s"), ("uint8_t", "variant"), ("size_t *", "size_out")],
       [sig("intset_send", ["intset"], "bytea", boundArgs={"variant": "WKB_EXTENDED"})],
       shape={"outParams": ["size_out"]}),
    # a type with no send of its own: its SQL hex writer leaves the byte order empty
    fn("raster_from_hexwkb", "Raster *", [("const char *", "hexwkb")],
       [sig("rasterFromHexWKB", ["text"], "raster")]),
    fn("raster_as_hexwkb", "char *",
       [(RAST, "rast"), ("uint8_t", "variant"), ("size_t *", "size_out")],
       [sig("asHexWKB", ["raster", "text"], "text", argDefaults=[None, "''"])],
       shape={"outParams": ["size_out"]}),
    # a cell: a value of its own over uint64_t, read and written by its own functions
    fn("h3index_in", "uint64_t", [("const char *", "str")], typedef="H3Index"),
    fn("h3index_out", "char *", [("uint64_t", "cell")], typedef="H3Index"),
    # geometry and geography share one class: a HexEWKB reader per type, one writer for both
    fn("geom_from_hexewkb", "GSERIALIZED *", [("const char *", "hexwkb")],
       [sig(None, ["text"], "geometry")]),
    fn("geog_from_hexewkb", "GSERIALIZED *", [("const char *", "hexwkb")],
       [sig(None, ["text"], "geography")]),
    fn("geo_as_hexewkb", "char *", [("const GSERIALIZED *", "gs"), ("const char *", "endian")],
       [sig(None, ["geometry", "text"], "text"), sig(None, ["geography", "text"], "text")]),
]


def _idl(functions=FUNCTIONS):
    return {"functions": json.loads(json.dumps(functions)),
            "macros": [{"name": "WKB_EXTENDED", "value": 4}],
            "structs": [{"name": "Set", "fields": []}, {"name": "Raster", "fields": []}],
            "typeEncodings": {"Set": {}, "Raster": {}, "GSERIALIZED": {}}}


class CodecTests(unittest.TestCase):
    """#state_type_encodings of parser/codecs.py over a synthetic catalog."""

    def setUp(self):
        idl, self.errors = state_type_encodings(_idl())
        self.te = idl["typeEncodings"]
        self.structs = {s["name"]: s for s in idl["structs"]}

    def test_each_type_is_read_and_written_by_its_own_function(self):
        s = self.te["Set"]
        self.assertEqual(s["readers"]["text"], {"intset": "intset_in",
                                                "floatset": "floatset_in"})
        self.assertEqual(s["writers"]["text"], {"intset": "intset_out",
                                                "floatset": "floatset_out",
                                                "geomset": "spatialset_out"})
        self.assertNotIn("text", s["decoders"])
        self.assertNotIn("text", s["encoders"])
        self.assertEqual(s["writerAux"]["text"]["floatset"],
                         [{"name": "maxdd", "kind": "integer", "default": 15}])
        self.assertEqual(self.errors, [])

    def test_one_function_for_every_type_is_the_class_codec(self):
        s = self.te["Set"]
        self.assertEqual(s["decoders"]["wkb"], "set_from_hexwkb")
        self.assertEqual(s["in"], "set_from_hexwkb")
        self.assertEqual(self.structs["Set"]["serialization"]["in"], "set_from_hexwkb")

    def test_the_hex_writer_takes_the_variant_its_send_binds(self):
        s = self.te["Set"]
        self.assertEqual(s["encoders"]["wkb"], "set_as_hexwkb")
        self.assertEqual(s["encoderAux"]["wkb"],
                         [{"name": "variant", "kind": "integer", "default": 4}])

    def test_without_a_send_the_variant_is_the_sql_writers_default(self):
        r = self.te["Raster"]
        self.assertEqual(r["encoders"], {"wkb": "raster_as_hexwkb"})
        self.assertEqual(r["out"], "raster_as_hexwkb")
        self.assertEqual(r["encoderAux"]["wkb"][0]["default"], 0)

    def test_a_hexewkb_reader_per_type_and_one_writer_for_both(self):
        g = self.te["GSERIALIZED"]
        self.assertEqual(g["readers"]["wkb"], {"geometry": "geom_from_hexewkb",
                                               "geography": "geog_from_hexewkb"})
        self.assertNotIn("wkb", g["decoders"])
        self.assertEqual(g["encoders"]["wkb"], "geo_as_hexewkb")
        self.assertEqual(g["encoderAux"]["wkb"],
                         [{"name": "endian", "kind": "string", "default": None}])

    def test_the_plain_hex_writer_and_reader_rank_before_the_e_ones(self):
        """A class having both the plain and the E hex-WKB functions keeps the plain ones,
        as #test_a_hexewkb_reader_per_type_and_one_writer_for_both keeps the E ones of a
        class having no other."""
        cb = "const Cbuffer *"
        both = FUNCTIONS + [
            fn("cbuffer_from_hexwkb", "Cbuffer *", [("const char *", "hexwkb")],
               [sig("cbufferFromHexWKB", ["text"], "cbuffer")]),
            fn("cbuffer_from_hexewkb", "Cbuffer *", [("const char *", "hexwkb")],
               [sig("cbufferFromHexEWKB", ["text"], "cbuffer")]),
            fn("cbuffer_as_hexwkb", "char *",
               [(cb, "cb"), ("uint8_t", "variant"), ("size_t *", "size_out")],
               [sig("asHexWKB", ["cbuffer", "text"], "text")],
               shape={"outParams": ["size_out"]}),
            fn("cbuffer_as_hexewkb", "char *",
               [(cb, "cb"), ("uint8_t", "variant"), ("size_t *", "size_out")],
               [sig("asHexEWKB", ["cbuffer", "text"], "text")],
               shape={"outParams": ["size_out"]})]
        idl = _idl(both)
        idl["typeEncodings"]["Cbuffer"] = {}
        idl, errors = state_type_encodings(idl)
        c = idl["typeEncodings"]["Cbuffer"]
        self.assertEqual((c["decoders"]["wkb"], c["encoders"]["wkb"]),
                         ("cbuffer_from_hexwkb", "cbuffer_as_hexwkb"))
        self.assertEqual(errors, [])

    def test_a_cell_is_a_class_of_its_own(self):
        h = self.te["H3Index"]
        self.assertEqual((h["in"], h["out"]), ("h3index_in", "h3index_out"))

    def test_two_functions_alike_for_one_type_stop_the_catalog(self):
        twin = fn("intset_in2", "Set *", [("const char *", "str")],
                  [sig("intset_in", ["cstring"], "intset")])
        twin["name"] = "intsetx_in"
        _, errors = state_type_encodings(_idl(FUNCTIONS + [twin]))
        self.assertEqual(errors, ["Set: intset read by intset_in and intsetx_in"])


class CodecContractTests(unittest.TestCase):
    """Over the generated catalog: every trailing input a codec states names a parameter
    of its function, carries the value a binding passes, and is never an out-parameter."""

    def setUp(self):
        if not IDL.exists():
            self.skipTest(f"{IDL} not generated; run `python run.py` first")
        self.idl = json.loads(IDL.read_text())
        self.fns = {f["name"]: f for f in self.idl["functions"]}

    def _stated(self):
        for cls, e in self.idl["typeEncodings"].items():
            for side, names in (("decoderAux", e.get("decoders") or {}),
                                ("encoderAux", e.get("encoders") or {})):
                for enc, name in names.items():
                    yield cls, name, (e.get(side) or {}).get(enc, [])
            for side, aux in (("readers", "readerAux"), ("writers", "writerAux")):
                for enc, by_type in (e.get(side) or {}).items():
                    for t, name in by_type.items():
                        yield cls, name, e[aux][enc][t]
            if e.get("bytes", {}).get("encoderAux") is not None:
                yield cls, e["bytes"]["encoder"], e["bytes"]["encoderAux"]

    def test_every_trailing_input_is_named_valued_and_no_out_parameter(self):
        bad = []
        for cls, name, aux in self._stated():
            f = self.fns[name]
            params = {p["name"] for p in f["params"][1:]}
            out = set((f.get("shape") or {}).get("outParams") or ())
            for a in aux:
                if a["name"] not in params or "default" not in a or a["name"] in out:
                    bad.append((cls, name, a))
        self.assertEqual(bad, [])

    def test_the_classes_a_binding_reads(self):
        te = self.idl["typeEncodings"]
        self.assertEqual(te["Set"]["readers"]["text"]["intset"], "intset_in")
        self.assertEqual(te["Temporal"]["writers"]["text"]["tfloat"], "tfloat_out")
        self.assertEqual(te["Raster"]["out"], "raster_as_hexwkb")
        self.assertEqual(te["GSERIALIZED"]["readers"]["wkb"],
                         {"geometry": "geom_from_hexewkb", "geography": "geog_from_hexewkb"})
        self.assertEqual(te["GSERIALIZED"]["encoders"]["wkb"], "geo_as_hexewkb")
        for cell in ("H3Index", "Quadbin", "S2CellId"):
            self.assertIn(cell, te)


if __name__ == "__main__":
    unittest.main()
