"""How a binding carries each SQL type (parser/sqltypes.py #attach_sql_types): the C type it
stands for, the codec it crosses an engine in and the memory image it keeps.

The synthetic catalog follows the shape #fn of tests/test_codecs.py builds; the contract tests
read the generated catalog, as #TypeRecoverTests of tests/test_typerecover.py does. Plain
unittest, no pytest dependency.
"""
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from parser.sqltypes import attach_sql_types, _codec

IDL = Path(__file__).resolve().parents[1] / "output" / "meos-idl.json"


def fn(name, ret, params, sigs=(), api="public", sqlfn="f", typedef=None):
    f = {"name": name, "api": api, "sqlfn": sqlfn,
         "returnType": {"c": ret, "canonical": ret},
         "params": [{"name": n, "cType": t, "canonical": t,
                     **({"typedef": typedef} if typedef and t == "uint64_t" else {})}
                    for t, n in params],
         "sqlSignatures": [{"args": a, "ret": r} for a, r in sigs]}
    if typedef and ret == "uint64_t":
        f["returnType"]["typedef"] = typedef
    return f


GEO_ENC = {
    "readers": {"hexwkb": {"geometry": "geom_from_hexewkb"},
                "hexewkb": {"geometry": "geom_from_hexewkb"}},
    "readerAux": {"hexwkb": {"geometry": []}, "hexewkb": {"geometry": []}},
    "encoders": {"hexwkb": "geo_as_hexwkb", "hexewkb": "geo_as_hexewkb"},
    "encoderAux": {"hexwkb": [{"name": "endian"}], "hexewkb": [{"name": "endian"}]},
}


class CodecTests(unittest.TestCase):

    def test_hex_ewkb_keeps_the_srid(self):
        """A C type stating both hex WKB writers crosses in the extended one, which keeps the
        SRID: geometry is written by geo_as_hexewkb, never geo_as_hexwkb."""
        c = _codec("geometry", "GSERIALIZED", GEO_ENC,
                   {"geom_from_hexewkb", "geo_as_hexwkb", "geo_as_hexewkb"}, {}, True)
        self.assertEqual((c["kind"], c["decoder"], c["encoder"]),
                         ("wkb", "geom_from_hexewkb", "geo_as_hexewkb"))

    def test_hex_wkb_where_no_extended_form(self):
        enc = {k: {e: v for e, v in d.items() if e == "hexwkb"} for k, d in GEO_ENC.items()}
        c = _codec("geometry", "GSERIALIZED", enc,
                   {"geom_from_hexewkb", "geo_as_hexwkb"}, {}, True)
        self.assertEqual(c["encoder"], "geo_as_hexwkb")

    def test_byte_codec_first(self):
        e = {"bytes": {"decoder": "span_from_wkb", "encoder": "span_as_wkb"},
             "decoders": {"text": "span_in"}, "encoders": {"text": "span_out"}}
        c = _codec("tstzspan", "Span", e, {"span_from_wkb", "span_as_wkb", "span_in",
                                           "span_out"}, {}, True)
        self.assertEqual((c["kind"], c["decoder"]), ("bytes", "span_from_wkb"))

    def test_text_only_for_a_c_type_of_one_sql_type(self):
        """A text decoder cannot tell apart the SQL types one C type stands for."""
        e = {"decoders": {"text": "x_in"}, "encoders": {"text": "x_out"}}
        names = {"x_in", "x_out"}
        self.assertEqual(_codec("x", "X", e, names, {}, False)["kind"], "text")
        self.assertIsNone(_codec("x", "X", e, names, {}, True))

    def test_internal_function_is_no_codec(self):
        e = {"bytes": {"decoder": "x_from_wkb", "encoder": "x_as_wkb"}}
        self.assertIsNone(_codec("x", "X", e, {"x_from_wkb"}, {}, False))


class AttachTests(unittest.TestCase):

    def catalog(self):
        return {
            "functions": [
                fn("tfloat_in", "Temporal *", [("const char *", "str")], [(["cstring"], "tfloat")]),
                fn("tfloat_start", "double", [("const Temporal *", "temp")], [(["tfloat"], "float")]),
                fn("temporal_from_wkb", "Temporal *", [("const uint8_t *", "wkb"), ("size_t", "size")]),
                fn("temporal_as_wkb", "uint8_t *", [("const Temporal *", "temp")]),
                fn("temporal_copy", "Temporal *", [("const Temporal *", "temp")]),
                fn("temporal_mem_size", "size_t", [("const Temporal *", "temp")]),
                fn("cell_parent", "uint64_t", [("uint64_t", "cell")], [(["h3index"], "h3index")],
                   typedef="H3Index"),
            ],
            "typeEncodings": {
                "Temporal": {"bytes": {"decoder": "temporal_from_wkb", "encoder": "temporal_as_wkb"},
                             "image": {"copy": "temporal_copy", "size": "temporal_mem_size"}},
                "H3Index": {"bytes": {"decoder": "temporal_from_wkb", "encoder": "temporal_as_wkb"}},
            },
        }

    def test_c_type_codec_and_image(self):
        st = attach_sql_types(self.catalog())["sqlTypes"]
        self.assertEqual(st["tfloat"]["cType"], "Temporal")
        self.assertEqual(st["tfloat"]["codec"]["kind"], "bytes")
        self.assertEqual(st["tfloat"]["image"], {"copy": "temporal_copy", "size": "temporal_mem_size"})

    def test_cell_by_value_keeps_no_image(self):
        st = attach_sql_types(self.catalog())["sqlTypes"]
        self.assertEqual((st["h3index"]["cType"], st["h3index"]["byValue"]), ("H3Index", True))
        self.assertNotIn("image", st["h3index"])

    def test_image_through_public_functions_alone(self):
        cat = self.catalog()
        next(f for f in cat["functions"] if f["name"] == "temporal_copy")["api"] = "internal"
        self.assertNotIn("image", attach_sql_types(cat)["sqlTypes"]["tfloat"])


class ElementTests(unittest.TestCase):
    """The element of an array of a SQL scalar, over a synthetic catalog as #AttachTests builds one."""

    def catalog(self):
        """An integer[] signature, the core io reader and writer of int32_t, and two decoys: an int
        writer of category transformation and an int reader of the JSON family (#AttachTests.catalog)."""
        def io(name, ret, params, family="CORE", category="io", shape=None):
            f = fn(name, ret, params, sqlfn=None)
            f.update({"family": family, "category": category})
            if shape:
                f["shape"] = shape
            return f
        arr = fn("intset_make", "Set *", [("const int *", "values"), ("int", "count")],
                 [(["integer[]"], "intset")])
        arr["shape"] = {"inputArrays": [{"param": "values", "lengthFrom": {"kind": "param", "name": "count"},
                                         "element": {"c": "int", "canonical": "int"}}]}
        return {"functions": [
            arr,
            io("int32_in", "int32_t", [("const char *", "str")]),
            io("int32_out", "char *", [("int32_t", "num"), ("size_t *", "size_out")],
               shape={"outParams": ["size_out"]}),
            io("geo_typename", "char *", [("int", "type")], category="transformation"),
            io("other_in", "int32_t", [("const char *", "str")], family="JSON"),
        ], "typeEncodings": {}}

    def test_element_of_an_integer_array(self):
        """integer[] is an int array, read and written by the core io functions of int32_t, as
        #AttachTests.test_c_type_codec_and_image reads a value type's codec."""
        e = attach_sql_types(self.catalog())["sqlTypes"]["integer"]["element"]
        self.assertEqual((e["cType"], e["byValue"]), ("int", True))
        self.assertEqual((e["codec"]["decoder"], e["codec"]["encoder"]), ("int32_in", "int32_out"))
        self.assertEqual(e["codec"]["encoderAux"], [])

    def test_no_element_without_one_reader_and_one_writer(self):
        """Without its writer, integer states no element, as #CodecTests.test_internal_function_is_no_codec
        states no codec."""
        cat = self.catalog()
        cat["functions"] = [f for f in cat["functions"] if f["name"] != "int32_out"]
        self.assertNotIn("integer", attach_sql_types(cat)["sqlTypes"])


@unittest.skipUnless(IDL.exists(), "output/meos-idl.json is generated by run.py")
class ContractTests(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.idl = json.loads(IDL.read_text())
        cls.st = cls.idl["sqlTypes"]

    def test_geometry_and_geography_keep_their_srid(self):
        for sql in ("geometry", "geography"):
            self.assertEqual(self.st[sql]["codec"]["encoder"], "geo_as_hexewkb", sql)

    def test_every_codec_function_is_public(self):
        public = {f["name"] for f in self.idl["functions"] if f.get("api") != "internal"}
        for sql, e in self.st.items():
            for side in ("decoder", "encoder"):
                if "codec" in e:
                    self.assertIn(e["codec"][side], public, f"{sql} {side}")

    def test_scalar_array_elements(self):
        """Each SQL scalar whose array a signature passes or answers states the C element of the
        array and the core reader and writer of that element, over the generated catalog as
        #ContractTests.test_geometry_and_geography_keep_their_srid reads it."""
        want = {"integer": ("int", "int32_in", "int32_out"), "bigint": ("int64_t", "int64_in", "int64_out"),
                "boolean": ("bool", "bool_in", "bool_out"), "float": ("double", "float8_in", "float8_out"),
                "date": ("DateADT", "date_in", "date_out"),
                "timestamptz": ("TimestampTz", "timestamptz_in", "timestamptz_out"),
                "text": ("text *", "text_in", "text_out")}
        for sql, (ct, dec, enc) in want.items():
            e = self.st[sql]["element"]
            self.assertEqual((e["cType"], e["codec"]["decoder"], e["codec"]["encoder"]), (ct, dec, enc), sql)

    def test_temporal_types_carry_their_image(self):
        for sql in ("tint", "tfloat", "tgeompoint", "tstzspan", "stbox"):
            self.assertIn("image", self.st[sql], sql)


if __name__ == "__main__":
    unittest.main()
