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
    # the hex-WKB pair serving every set type, its size an out-parameter, its variant
    # what send binds
    fn("set_from_hexwkb", "Set *", [("const char *", "hexwkb")],
       [sig("intsetFromHexWKB", ["text"], "intset"),
        sig("floatsetFromHexWKB", ["text"], "floatset"),
        sig("geomsetFromHexWKB", ["text"], "geomset")]),
    fn("set_as_hexwkb", "char *",
       [(SET, "s"), ("uint8_t", "variant"), ("size_t *", "size_out")],
       [sig("asHexWKB", [t, "text"], "text") for t in ("intset", "floatset", "geomset")],
       shape={"outParams": ["size_out"]}),
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
        self.assertEqual(s["decoders"]["hexwkb"], "set_from_hexwkb")
        self.assertEqual(s["in"], "set_from_hexwkb")
        self.assertEqual(self.structs["Set"]["serialization"]["in"], "set_from_hexwkb")

    def test_the_hex_writer_writes_plain_wkb_whatever_send_binds(self):
        """``asHexWKB`` leaving its byte order to the default '' passes 0, plain WKB,
        while ``set_send`` binds ``WKB_EXTENDED`` (#_portable_variant)."""
        fns = [dict(f, sqlSignatures=[dict(s, argDefaults=[None, "''"])
                                      for s in f["sqlSignatures"]])
               if f["name"] == "set_as_hexwkb" else f for f in FUNCTIONS]
        fns = [dict(f, params=[dict(p, _callLiteral="4") if p["name"] == "variant" else p
                               for p in f["params"]])
               if f["name"] == "set_as_hexwkb" else f for f in fns]
        idl, _ = state_type_encodings(_idl(fns))
        s = idl["typeEncodings"]["Set"]
        self.assertEqual(s["encoders"]["hexwkb"], "set_as_hexwkb")
        self.assertEqual(s["encoderAux"]["hexwkb"],
                         [{"name": "variant", "kind": "integer", "default": 0}])
        # the service wire writes as send does, the SRID included
        self.assertEqual(s["out"], "set_as_hexwkb")
        self.assertEqual(s["out_aux"], [{"name": "variant", "kind": "integer", "default": 4}])

    def test_without_a_send_the_variant_is_the_sql_writers_default(self):
        r = self.te["Raster"]
        self.assertEqual(r["encoders"], {"hexwkb": "raster_as_hexwkb"})
        self.assertEqual(r["out"], "raster_as_hexwkb")
        self.assertEqual(r["encoderAux"]["hexwkb"][0]["default"], 0)

    def test_a_hexewkb_reader_per_type_and_one_writer_for_both(self):
        g = self.te["GSERIALIZED"]
        self.assertEqual(g["readers"]["hexewkb"], {"geometry": "geom_from_hexewkb",
                                                   "geography": "geog_from_hexewkb"})
        self.assertNotIn("hexewkb", g["decoders"])
        self.assertEqual(g["encoders"]["hexewkb"], "geo_as_hexewkb")
        self.assertEqual(g["encoderAux"]["hexewkb"],
                         [{"name": "endian", "kind": "string", "default": None}])

    def test_the_plain_and_the_e_hex_wkb_are_encodings_of_their_own(self):
        """A class having both the plain and the E hex-WKB functions states each under
        its own encoding, the reader both SQL names reach under both."""
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
        self.assertEqual((c["decoders"]["hexwkb"], c["encoders"]["hexwkb"]),
                         ("cbuffer_from_hexwkb", "cbuffer_as_hexwkb"))
        self.assertEqual((c["decoders"]["hexewkb"], c["encoders"]["hexewkb"]),
                         ("cbuffer_from_hexewkb", "cbuffer_as_hexewkb"))
        self.assertEqual(errors, [])

    def _cbuffer(self, extra):
        """The typeEncodings of ``Cbuffer`` and the errors over the catalog of
        #test_the_plain_and_the_e_hex_wkb_are_encodings_of_their_own with ``extra``."""
        idl = _idl(FUNCTIONS + extra)
        idl["typeEncodings"]["Cbuffer"] = {}
        idl, errors = state_type_encodings(idl)
        return idl["typeEncodings"]["Cbuffer"], errors

    CB = "const Cbuffer *"

    def test_the_text_writer_is_the_one_as_text_reaches(self):
        """``cbuffer_out`` writes the point as hex WKB; ``asText(cbuffer)`` reaches
        ``cbuffer_as_text``, which writes it as WKT, so it is the text writer; the E
        twin is the writer of an encoding of its own."""
        c, errors = self._cbuffer([
            fn("cbuffer_in", "Cbuffer *", [("const char *", "str")],
               [sig("cbuffer_in", ["cstring"], "cbuffer"),
                sig("cbufferFromText", ["text"], "cbuffer"),
                sig("cbufferFromEWKT", ["text"], "cbuffer")]),
            fn("cbuffer_out", "char *", [(self.CB, "cb"), ("int", "maxdd")],
               [sig("cbuffer_out", ["cbuffer"], "cstring")]),
            fn("cbuffer_as_text", "char *", [(self.CB, "cb"), ("int", "maxdd")],
               [sig("asText", ["cbuffer", "integer"], "text")]),
            fn("cbuffer_as_ewkt", "char *", [(self.CB, "cb"), ("int", "maxdd")],
               [sig("asEWKT", ["cbuffer", "integer"], "text")])])
        self.assertEqual(c["encoders"], {"text": "cbuffer_as_text",
                                         "ewkt": "cbuffer_as_ewkt"})
        self.assertEqual(c["decoders"], {"text": "cbuffer_in", "ewkt": "cbuffer_in"})
        self.assertEqual((c["in"], c["out"]), ("cbuffer_in", "cbuffer_as_ewkt"))
        self.assertEqual(errors, [])

    def test_the_wkb_bytes_reader_states_their_length(self):
        c, errors = self._cbuffer([
            fn("cbuffer_from_wkb", "Cbuffer *",
               [("const uint8_t *", "wkb"), ("size_t", "size")],
               [sig("cbufferFromBinary", ["bytea"], "cbuffer"),
                sig("cbufferFromEWKB", ["bytea"], "cbuffer")]),
            fn("cbuffer_as_wkb", "uint8_t *",
               [(self.CB, "cb"), ("uint8_t", "variant"), ("size_t *", "size_out")],
               [sig("asBinary", ["cbuffer", "text"], "bytea")],
               shape={"outParams": ["size_out"]})])
        self.assertEqual(c["decoders"], {"wkb": "cbuffer_from_wkb",
                                         "ewkb": "cbuffer_from_wkb"})
        self.assertEqual(c["decoderAux"]["wkb"],
                         [{"name": "size", "kind": "length", "of": "wkb"}])
        self.assertEqual(c["encoders"], {"wkb": "cbuffer_as_wkb"})
        self.assertEqual((c["in"], c["out"]), (None, None))
        self.assertEqual(errors, [])

    def test_a_writer_of_some_types_of_a_class_is_no_codec_of_the_class(self):
        """A function serving some of the SQL types of a class is stated for those
        alone, as ``tspatial_as_ewkt`` writes no ``tint``."""
        idl, errors = state_type_encodings(_idl(FUNCTIONS + [
            fn("spatialset_as_ewkt", "char *", [(SET, "s"), ("int", "maxdd")],
               [sig("asEWKT", ["geomset", "integer"], "text")])]))
        s = idl["typeEncodings"]["Set"]
        self.assertEqual(s["writers"]["ewkt"], {"geomset": "spatialset_as_ewkt"})
        self.assertNotIn("ewkt", s["encoders"])
        self.assertEqual(errors, [])

    def test_in_and_out_are_one_encoding(self):
        """A class read per SQL type in MF-JSON and written by one function in it reads
        and writes a value in hex WKB, the encoding it has one reader and one writer of
        (the class codec of #test_one_function_for_every_type_is_the_class_codec), so a
        service reads back what it writes."""
        tp = "const Temporal *"
        idl = _idl(FUNCTIONS + [
            fn("tint_from_mfjson", "Temporal *", [("const char *", "mfjson")],
               [sig("tintFromMFJSON", ["text"], "tint")]),
            fn("tfloat_from_mfjson", "Temporal *", [("const char *", "mfjson")],
               [sig("tfloatFromMFJSON", ["text"], "tfloat")]),
            fn("temporal_as_mfjson", "char *", [(tp, "temp"), ("int", "precision")],
               [sig("asMFJSON", [t, "integer"], "text") for t in ("tint", "tfloat")]),
            fn("temporal_from_hexwkb", "Temporal *", [("const char *", "hexwkb")],
               [sig(t + "FromHexWKB", ["text"], t) for t in ("tint", "tfloat")]),
            fn("temporal_as_hexwkb", "char *",
               [(tp, "temp"), ("uint8_t", "variant"), ("size_t *", "size_out")],
               [sig("asHexWKB", [t, "text"], "text") for t in ("tint", "tfloat")],
               shape={"outParams": ["size_out"]})])
        idl["typeEncodings"]["Temporal"] = {}
        idl, errors = state_type_encodings(idl)
        t = idl["typeEncodings"]["Temporal"]
        self.assertEqual(t["encoders"]["mfjson"], "temporal_as_mfjson")
        self.assertEqual((t["in"], t["out"]), ("temporal_from_hexwkb", "temporal_as_hexwkb"))
        self.assertEqual(errors, [])

    MODEL = {"lattice": {"Temporal": {"kind": "root", "parent": None},
                         "TInt": {"kind": "leaf", "parent": "Temporal"}},
             "axes": {"subtype": {"values": [
                 {"name": "ANYTEMPSUBTYPE", "class": None},
                 {"name": "TINSTANT", "class": "TInstant"}]}}}

    def test_a_subtype_class_reads_through_its_parent(self):
        """``TInstant``, whose readers and writers are internal, states no codec of its
        own and reads and writes through ``Temporal``, the root of the lattice the subtype
        axis of the object model concretizes, as #test_an_internal_copy_is_no_image
        keeps an internal function out of the image."""
        inst = "const TInstant *"
        extra = [fn("tintinst_in", "TInstant *", [("const char *", "str")], api="internal"),
                 fn("tinstant_out", "char *", [(inst, "inst"), ("int", "maxdd")],
                    api="internal")]
        idl = _idl(FUNCTIONS + extra)
        idl["typeEncodings"]["TInstant"] = {"encodings": ["text"],
                                            "decoders": {"text": "tintinst_in"},
                                            "encoders": {"text": "tinstant_out"}}
        idl, errors = state_type_encodings(idl, self.MODEL)
        t = idl["typeEncodings"]["TInstant"]
        self.assertEqual((t["decoders"], t["encoders"], t["encodings"]), ({}, {}, []))
        self.assertEqual((t["in"], t["out"], t["through"]), (None, None, "Temporal"))
        self.assertNotIn("through", idl["typeEncodings"]["Set"])
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


class ImageTests(unittest.TestCase):
    """The memory image #_image of parser/codecs.py states for a class over the synthetic
    catalog of #CodecTests, with the copy and size functions each case adds."""

    SPAN = {"name": "Span", "sizeBytes": 24,
            "fields": [{"name": "spantype", "cType": "uint8", "offset_bits": 0}]}
    TEXT = {"name": "text", "sizeBytes": 4,
            "fields": [{"name": "vl_len_", "cType": "char[4]", "offset_bits": 0},
                       {"name": "vl_dat", "cType": "char[]", "offset_bits": 32}]}

    def _image(self, extra, structs=(), classes=()):
        idl = _idl(FUNCTIONS + extra)
        idl["structs"] += [dict(s) for s in structs]
        for cls in classes:
            idl["typeEncodings"][cls] = {}
        idl, errors = state_type_encodings(idl)
        return idl["typeEncodings"], errors

    def test_a_variable_length_value_states_its_copy_and_its_size_function(self):
        te, errors = self._image([
            fn("set_copy", "Set *", [(SET, "s")]),
            fn("set_mem_size", "int", [(SET, "s")]),
            fn("raster_copy", "Raster *", [(RAST, "rast")]),
            fn("raster_mem_size", "int", [(RAST, "rast")])])
        self.assertEqual(te["Set"]["image"], {"copy": "set_copy", "size": "set_mem_size"})
        self.assertEqual(te["Raster"]["image"],
                         {"copy": "raster_copy", "size": "raster_mem_size"})
        self.assertEqual(errors, [])

    def test_a_fixed_size_value_states_its_copy_and_its_structure_size(self):
        te, errors = self._image([fn("span_copy", "Span *", [("const Span *", "s")])],
                                 [self.SPAN], ["Span"])
        self.assertEqual(te["Span"]["image"], {"copy": "span_copy", "sizeBytes": 24})
        self.assertEqual(errors, [])

    def test_a_flexible_array_structure_without_a_size_function_states_no_image(self):
        te, _ = self._image([fn("text_copy", "text *", [("const text *", "txt")])],
                            [self.TEXT], ["text"])
        self.assertNotIn("image", te["text"])

    def test_no_copy_no_image(self):
        te, _ = self._image([fn("set_mem_size", "int", [(SET, "s")])])
        self.assertNotIn("image", te["Set"])
        self.assertNotIn("image", te["H3Index"])

    def test_an_internal_copy_is_no_image(self):
        te, _ = self._image([fn("set_copy", "Set *", [(SET, "s")], api="internal"),
                             fn("set_mem_size", "int", [(SET, "s")])])
        self.assertNotIn("image", te["Set"])

    def test_two_copies_of_one_class_stop_the_catalog(self):
        _, errors = self._image([fn("set_copy", "Set *", [(SET, "s")]),
                                 fn("intset_copy", "Set *", [(SET, "s")])])
        self.assertEqual(errors, ["Set: copied by intset_copy, set_copy"])


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
        """Every input #_stated yields carries the value a binding passes, or, for the
        size a WKB reader takes, names the bytes whose length it is."""
        bad = []
        for cls, name, aux in self._stated():
            f = self.fns[name]
            params = {p["name"] for p in f["params"][1:]}
            out = set((f.get("shape") or {}).get("outParams") or ())
            for a in aux:
                valued = ("default" in a if a.get("kind") != "length"
                          else a.get("of") == f["params"][0]["name"])
                if a["name"] not in params or not valued or a["name"] in out:
                    bad.append((cls, name, a))
        self.assertEqual(bad, [])

    def test_the_classes_a_binding_reads(self):
        te = self.idl["typeEncodings"]
        self.assertEqual(te["Set"]["readers"]["text"]["intset"], "intset_in")
        self.assertEqual(te["Temporal"]["writers"]["text"]["tfloat"], "tfloat_out")
        self.assertEqual(te["Raster"]["out"], "raster_as_hexwkb")
        self.assertEqual(te["GSERIALIZED"]["readers"]["hexewkb"],
                         {"geometry": "geom_from_hexewkb", "geography": "geog_from_hexewkb"})
        self.assertEqual(te["GSERIALIZED"]["encoders"]["hexewkb"], "geo_as_hexewkb")
        for cell in ("H3Index", "Quadbin", "S2CellId"):
            self.assertIn(cell, te)

    def test_a_spatial_value_is_written_as_text_through_as_text(self):
        """The text writer of a type, read as #test_the_classes_a_binding_reads reads a
        class, is the function its SQL ``asText`` reaches: WKT through ``*_as_text`` for
        a spatial type, ``*_out`` for the others."""
        te = self.idl["typeEncodings"]
        for cls in ("Cbuffer", "Npoint", "Pose", "PoseChain"):
            self.assertEqual(te[cls]["encoders"]["text"], cls.lower() + "_as_text")
            self.assertEqual(te[cls]["encoders"]["ewkt"], cls.lower() + "_as_ewkt")
        sets = te["Set"]["writers"]["text"]
        for t in ("geomset", "geogset", "cbufferset", "npointset", "poseset",
                  "posechainset"):
            self.assertEqual(sets[t], "spatialset_as_text")
        self.assertEqual(sets["intset"], "intset_out")
        temps = te["Temporal"]["writers"]["text"]
        for t in ("tgeompoint", "tgeogpoint", "tgeometry", "tgeography", "tcbuffer",
                  "tnpoint", "tpose", "tposechain"):
            self.assertEqual(temps[t], "tspatial_as_text")
        self.assertEqual(temps["trgeometry"], "trgeometry_as_text")
        self.assertEqual(temps["tint"], "tint_out")

    def test_every_format_is_an_encoding_of_its_own(self):
        """The seven formats, read as #test_the_classes_a_binding_reads reads a class."""
        te = self.idl["typeEncodings"]
        self.assertEqual(te["Temporal"]["encodings"],
                         ["ewkb", "ewkt", "hexewkb", "hexwkb", "mfjson", "text", "wkb"])
        self.assertEqual(te["Cbuffer"]["decoders"]["ewkt"], "cbuffer_in")
        self.assertEqual(te["Cbuffer"]["decoders"]["wkb"], "cbuffer_from_wkb")
        self.assertEqual(te["Cbuffer"]["encoders"]["ewkb"], "cbuffer_as_ewkb")
        self.assertNotIn("tint", te["Temporal"]["writers"]["ewkt"])

    def test_every_codec_is_public_and_a_subtype_reads_through_temporal(self):
        """Every function #_stated yields is public, and ``TInstant``, ``TSequence`` and
        ``TSequenceSet`` read and write through ``Temporal``: ``tintinst_make`` answers
        a value ``temporal_as_hexwkb`` writes."""
        bad = [(cls, name) for cls, name, _ in self._stated()
               if self.fns[name].get("api") != "public"]
        self.assertEqual(bad, [])
        te = self.idl["typeEncodings"]
        for cls in ("TInstant", "TSequence", "TSequenceSet"):
            self.assertEqual((te[cls]["decoders"], te[cls]["encoders"]), ({}, {}))
            self.assertEqual(te[cls]["through"], "Temporal")
        self.assertEqual(self.fns["tintinst_make"]["wire"]["result"]["encode"],
                         "temporal_as_hexwkb")

    def test_plain_wkb_writers_pass_variant_0(self):
        """Every WKB writer #_stated yields passes the variant 0 its SQL function passes
        for an empty byte order, plain WKB for ``wkb`` and ``hexwkb``; the byte codec
        keeps the variant ``send`` binds."""
        te = self.idl["typeEncodings"]
        bad = []
        for cls, e in te.items():
            auxes = [((e.get("encoderAux") or {}).get(enc) or []) for enc in
                     ("wkb", "hexwkb", "ewkb", "hexewkb") if enc in (e.get("encoders") or {})]
            for enc, by_type in (e.get("writerAux") or {}).items():
                if enc in ("wkb", "hexwkb", "ewkb", "hexewkb"):
                    auxes += list(by_type.values())
            for aux in auxes:
                for a in aux:
                    if a["name"] == "variant" and a["default"] != 0:
                        bad.append((cls, a))
        self.assertEqual(bad, [])
        self.assertEqual(te["Temporal"]["encoderAux"]["hexwkb"][0]["default"], 0)
        self.assertEqual(te["Temporal"]["out"], "temporal_as_hexwkb")
        self.assertEqual(te["Temporal"]["out_aux"][0]["default"], 4)
        self.assertEqual(te["Set"]["bytes"]["encoderAux"][0]["default"], 4)

    def test_a_service_reads_a_value_in_the_format_it_writes_it(self):
        """``in`` and ``out``, read as #test_the_classes_a_binding_reads reads a class,
        are the reader and the writer of one encoding wherever the class has one of
        each in a string encoding."""
        te = self.idl["typeEncodings"]
        self.assertEqual((te["Temporal"]["in"], te["Temporal"]["out"]),
                         ("temporal_from_hexwkb", "temporal_as_hexwkb"))
        bad = []
        for cls, e in te.items():
            pairs = [x for x in e["decoders"] if x in e["encoders"]
                     and x not in ("wkb", "ewkb")]
            if pairs and not any((e["in"], e["out"]) == (e["decoders"][x], e["encoders"][x])
                                 for x in pairs):
                bad.append(cls)
        self.assertEqual(bad, [])

    def test_the_images_a_binding_reads(self):
        """The image of #ImageTests over the generated catalog: a value sized by a
        function, and one whose image is its structure, at the size the structure
        states."""
        te = self.idl["typeEncodings"]
        sizes = {s["name"]: s.get("sizeBytes") for s in self.idl["structs"]}
        self.assertEqual(te["Temporal"]["image"],
                         {"copy": "temporal_copy", "size": "temporal_mem_size"})
        self.assertEqual(te["Raster"]["image"],
                         {"copy": "raster_copy", "size": "raster_mem_size"})
        for cls, copy in (("Span", "span_copy"), ("STBox", "stbox_copy"),
                          ("Npoint", "npoint_copy"), ("Nsegment", "nsegment_copy")):
            self.assertEqual(te[cls]["image"], {"copy": copy, "sizeBytes": sizes[cls]})

    def test_every_image_names_public_functions_of_its_class(self):
        bad = []
        for cls, e in self.idl["typeEncodings"].items():
            image = e.get("image")
            if not image:
                continue
            for key in ("copy", "size"):
                f = self.fns.get(image.get(key)) if key in image else None
                if key in image and (f is None or f.get("api") != "public"
                                     or len(f["params"]) != 1):
                    bad.append((cls, key, image[key]))
            if ("size" in image) == ("sizeBytes" in image):
                bad.append((cls, "size xor sizeBytes", image))
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
