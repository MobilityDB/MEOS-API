"""A SQL signature two public functions claim stays on the one whose C parameters it fits.

A wrapper serving a whole family is claimed by each typed MEOS function behind it, and a type
scope naming the box they all take keeps every signature on each. #attach_claims_fit of
parser/sqlfn.py lets a claimant whose parameters a signature does not fit give it up when
another claimant's parameters fit it, and changes nothing else.

The catalog is synthetic, its classes stated as #CompositionTests of tests/test_compositions.py
states them. Plain unittest, no pytest dependency.
"""
import unittest

from parser.sqlfn import attach_claims_fit

TB = "const TBox *"


def _fn(name, sqlfn, params, sigs, api="public", shape=None):
    f = {"name": name, "sqlfn": sqlfn, "api": api,
         "params": [{"name": n, "cType": c} for n, c in params],
         "sqlSignatures": [dict(s) for s in sigs]}
    if shape:
        f["shape"] = shape
    return f


# The three signatures Tbox_value_tiles deploys, each typed kernel claiming all three
TILES = [{"args": ["tbox", "integer", "integer", "boolean"], "ret": "tbox[]"},
         {"args": ["tbox", "bigint", "bigint", "boolean"], "ret": "tbox[]"},
         {"args": ["tbox", "float", "float", "boolean"], "ret": "tbox[]"}]


def _kernel(name, ctype):
    return _fn(name, "valueTiles", [("box", TB), ("vsize", ctype), ("vorigin", ctype),
                                    ("border_inc", "bool")], TILES)


def _idl(*functions):
    return {"functions": list(functions),
            "objectModel": {"classes": {"TBox": {"cType": "TBox *"}}}}


def _args(f):
    return [tuple(s["args"]) for s in f["sqlSignatures"]]


class ClaimsFitTests(unittest.TestCase):
    """Each claimant of a shared signature keeps the signatures its parameters fit."""

    def test_each_typed_kernel_keeps_its_own_signature(self):
        idl, n = attach_claims_fit(_idl(_kernel("tintbox_value_tiles", "int"),
                                        _kernel("tbigintbox_value_tiles", "int64"),
                                        _kernel("tfloatbox_value_tiles", "double")))
        fns = {f["name"]: f for f in idl["functions"]}
        self.assertEqual(_args(fns["tintbox_value_tiles"]),
                         [("tbox", "integer", "integer", "boolean")])
        self.assertEqual(_args(fns["tbigintbox_value_tiles"]),
                         [("tbox", "bigint", "bigint", "boolean")])
        self.assertEqual(_args(fns["tfloatbox_value_tiles"]),
                         [("tbox", "float", "float", "boolean")])
        self.assertEqual(n, 6)

    def test_a_bound_parameter_takes_no_argument(self):
        # shiftValue binds the width of the shift_scale kernel, as the catalog states
        sig = {"args": ["tbox", "integer"], "ret": "tbox",
               "boundArgs": {"width": "0", "hasshift": "true", "haswidth": "false"}}
        params = lambda t: [("box", TB), ("shift", t), ("width", t),  # noqa: E731
                            ("hasshift", "bool"), ("haswidth", "bool")]
        idl, n = attach_claims_fit(_idl(
            _fn("tintbox_shift_scale", "shiftValue", params("int"), [sig]),
            _fn("tfloatbox_shift_scale", "shiftValue", params("double"), [sig])))
        fns = {f["name"]: f for f in idl["functions"]}
        self.assertEqual(_args(fns["tintbox_shift_scale"]), [("tbox", "integer")])
        self.assertEqual(fns["tfloatbox_shift_scale"]["sqlSignatures"], [])
        self.assertEqual(n, 1)

    def test_a_signature_every_claimant_fits_stays_on_each(self):
        sig = [{"args": ["tbox", "integer", "integer", "boolean"], "ret": "tbox[]"}]
        params = [("box", TB), ("a", "int"), ("b", "int"), ("c", "bool")]
        idl, n = attach_claims_fit(_idl(_fn("one", "valueTiles", params, sig),
                                        _fn("two", "valueTiles", params, sig)))
        self.assertEqual(n, 0)
        self.assertTrue(all(f["sqlSignatures"] for f in idl["functions"]))

    def test_a_signature_no_claimant_fits_stays_on_each(self):
        sig = [{"args": ["tbox", "text"], "ret": "tbox"}]
        idl, n = attach_claims_fit(_idl(
            _fn("one", "f", [("box", TB), ("a", "int")], sig),
            _fn("two", "f", [("box", TB), ("a", "double")], sig)))
        self.assertEqual(n, 0)

    def test_an_array_length_takes_no_argument(self):
        # As #test_a_bound_parameter_takes_no_argument for a bound parameter: the count
        # shape.inputArrays names for an array takes no SQL argument, so Set_constructor's
        # set(float[]) stays on floatset_make and cbufferset_make gives it up
        def shape(element):
            return {"inputArrays": [{"param": "values",
                                     "lengthFrom": {"kind": "param", "name": "count"},
                                     "element": {"c": element, "canonical": element}}]}
        sig = [{"args": ["float[]"], "ret": "floatset"}]
        idl = _idl(
            _fn("floatset_make", "set", [("values", "const double *"), ("count", "int")],
                sig, shape=shape("double")),
            _fn("cbufferset_make", "set", [("values", "const Cbuffer *"), ("count", "int")],
                sig, shape=shape("Cbuffer")))
        idl["objectModel"]["classes"]["Cbuffer"] = {"cType": "Cbuffer *"}
        idl, n = attach_claims_fit(idl)
        fns = {f["name"]: f for f in idl["functions"]}
        self.assertEqual(_args(fns["floatset_make"]), [("float[]",)])
        self.assertEqual(fns["cbufferset_make"]["sqlSignatures"], [])
        self.assertEqual(n, 1)

    def test_an_internal_claimant_is_left_alone(self):
        idl, n = attach_claims_fit(_idl(_kernel("tintbox_value_tiles", "int"),
                                        _kernel("internal_tiles", "int64") | {"api": "internal"}))
        fns = {f["name"]: f for f in idl["functions"]}
        self.assertEqual(len(fns["internal_tiles"]["sqlSignatures"]), 3)
        self.assertEqual(len(fns["tintbox_value_tiles"]["sqlSignatures"]), 3)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
