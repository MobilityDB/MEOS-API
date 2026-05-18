"""Unit tests for the runtime server.

Runs without libclang, libmeos or pytest:  python3 tests/test_server.py

A real threaded HTTP server is exercised over a socket with a FakeEngine,
so routing / validation / dispatch / error mapping are end-to-end tested.
CtypesEngine marshalling is tested against a fake shared library.
"""

import json
import sys
import threading
import unittest
from http.client import HTTPConnection
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from server.app import make_server, build_routes
from server.engine import Engine, MeosError, CtypesEngine

TEMP = "const struct Temporal *"


def serialized(name, decode="temporal_in"):
    return {"name": name, "kind": "serialized", "cType": TEMP,
            "decode": decode, "encodings": ["text"]}


CATALOG = {
    "functions": [
        {"name": "temporal_eq", "category": "predicate",
         "network": {"exposable": True},
         "wire": {"params": [serialized("temp1"), serialized("temp2")],
                  "result": {"kind": "json", "json": "integer"}}},
        {"name": "temporal_set_interp", "category": "transformation",
         "network": {"exposable": True},
         "wire": {"params": [serialized("temp"),
                             {"name": "interp", "kind": "json",
                              "json": "string", "enum": "interpType"}],
                  "result": {"kind": "serialized",
                             "cType": "struct Temporal *",
                             "encode": "temporal_out",
                             "encode_aux": [{"name": "maxdd",
                                             "kind": "integer",
                                             "default": 15}]}}},
        {"name": "temporal_round", "category": "transformation",
         "network": {"exposable": True},
         "wire": {"params": [serialized("temp"),
                             {"name": "maxdd", "kind": "json",
                              "json": "integer"}],
                  "result": {"kind": "serialized",
                             "cType": "struct Temporal *",
                             "encode": "temporal_out"}}},
        {"name": "noop_op", "category": "transformation",
         "network": {"exposable": True},
         "wire": {"params": [], "result": {"kind": "void"}}},
        {"name": "tsequence_make", "category": "constructor",
         "network": {"exposable": False},
         "wire": {"params": [], "result": {"kind": "unsupported"}}},
        {"name": "floatset_value_n", "category": "accessor",
         "network": {"exposable": True},
         "wire": {"params": [serialized("s", "set_in"),
                             {"name": "n", "kind": "json",
                              "json": "integer"}],
                  "result": {"kind": "json", "json": "number",
                             "from_outparam": "result",
                             "out_ctype": "double *",
                             "presence_return": True}}},
        {"name": "geoset_value_n", "category": "accessor",
         "network": {"exposable": True},
         "wire": {"params": [serialized("s", "geoset_in"),
                             {"name": "n", "kind": "json",
                              "json": "integer"}],
                  "result": {"kind": "serialized", "cType": "GSERIALIZED **",
                             "encode": "geo_as_ewkt", "encode_aux": [],
                             "from_outparam": "result",
                             "out_ctype": "GSERIALIZED **",
                             "presence_return": True}}},
        {"name": "temporal_merge_array", "category": "transformation",
         "network": {"exposable": True},
         "wire": {"params": [
             {"name": "temparr", "kind": "array", "count_param": "count",
              "element": {"kind": "serialized",
                          "cType": "struct Temporal *",
                          "decode": "temporal_in", "decode_aux": [],
                          "encodings": ["text"]}}],
                  "result": {"kind": "serialized",
                             "cType": "struct Temporal *",
                             "encode": "temporal_out", "encode_aux": []}}},
        {"name": "temporal_sequences", "category": "accessor",
         "network": {"exposable": True},
         "wire": {"params": [serialized("temp", "temporal_in")],
                  "result": {"kind": "array", "count_outparam": "count",
                             "element": {"kind": "serialized",
                                         "cType": "struct TSequence *",
                                         "encode": "tsequence_out",
                                         "encode_aux": [],
                                         "encodings": ["text"]}}}},
    ],
    "enums": [{"name": "interpType",
               "values": [{"name": "STEP", "value": 0},
                          {"name": "LINEAR", "value": 1}]}],
}


class FakeEngine(Engine):
    name = "fake"

    def __init__(self):
        self.calls = []
        self.fail = False

    def decode(self, fn, value, aux=()):
        self.calls.append(("decode", fn, value, list(aux)))
        return ("H", fn, value)

    def invoke(self, fn, args, rt):
        self.calls.append(("invoke", fn, args, rt))
        if self.fail:
            raise MeosError("boom", 7)
        return {"void": None, "ptr": ("R", fn), "int": 42,
                "bool": 1, "double": 1.5, "str": "s"}[rt]

    def encode(self, fn, handle, aux=()):
        self.calls.append(("encode", fn, handle, list(aux)))
        return f"ENC({fn})"

    def invoke_outparam(self, fn, args, out_ctype, presence):
        self.calls.append(("invoke_outparam", fn, args, out_ctype, presence))
        if self.fail:
            raise MeosError("boom", 7)
        return (self.present, 99)

    def invoke_array(self, fn, args):
        self.calls.append(("invoke_array", fn, args))
        if self.fail:
            raise MeosError("boom", 7)
        return [101, 102]

    present = True


class ServerTests(unittest.TestCase):
    def setUp(self):
        self.engine = FakeEngine()
        self.srv = make_server(CATALOG, self.engine, "127.0.0.1", 0)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def req(self, method, path, body=None):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = None if body is None else json.dumps(body)
        c.request(method, path, data,
                  {"Content-Type": "application/json"} if data else {})
        r = c.getresponse()
        raw = r.read()
        c.close()
        return r.status, (json.loads(raw) if raw else None)

    # --- routing / health ---
    def test_routes_exclude_non_exposable(self):
        routes = build_routes(CATALOG)
        self.assertIn("/temporal_eq", routes)
        self.assertNotIn("/tsequence_make", routes)
        self.assertEqual(len(routes), 8)

    def test_health(self):
        st, body = self.req("GET", "/healthz")
        self.assertEqual(st, 200)
        self.assertEqual(body["engine"], "fake")
        self.assertEqual(body["operations"], 8)

    def test_unknown_route(self):
        self.assertEqual(self.req("POST", "/nope", {})[0], 404)
        self.assertEqual(self.req("GET", "/nope")[0], 404)

    # --- happy paths: full decode/invoke/encode pipeline ---
    def test_predicate_pipeline(self):
        st, body = self.req("POST", "/temporal_eq",
                            {"temp1": "Point(1 1)@...",
                             "temp2": "Point(2 2)@..."})
        self.assertEqual(st, 200)
        self.assertEqual(body, {"result": 42})
        kinds = [c[0] for c in self.engine.calls]
        self.assertEqual(kinds, ["decode", "decode", "invoke"])
        inv = self.engine.calls[-1]
        self.assertEqual(inv[1], "temporal_eq")
        self.assertEqual([t for t, _ in inv[2]], ["ptr", "ptr"])
        self.assertEqual(inv[3], "int")

    def test_enum_and_serialized_result(self):
        st, body = self.req("POST", "/temporal_set_interp",
                            {"temp": "x", "interp": "LINEAR"})
        self.assertEqual(st, 200)
        self.assertEqual(body, {"result": "ENC(temporal_out)"})
        inv = next(c for c in self.engine.calls if c[0] == "invoke")
        self.assertEqual(inv[2], [("ptr", ("H", "temporal_in", "x")),
                                  ("enum", 1)])           # LINEAR -> 1
        self.assertEqual(inv[3], "ptr")
        # encode_aux (maxdd=15) is passed through to engine.encode
        enc = next(c for c in self.engine.calls if c[0] == "encode")
        self.assertEqual(enc[3], [("int", 15)])

    def test_void(self):
        st, body = self.req("POST", "/noop_op", {})
        self.assertEqual(st, 204)
        self.assertIsNone(body)

    def test_scalar_outparam(self):
        st, body = self.req("POST", "/floatset_value_n", {"s": "x", "n": 0})
        self.assertEqual(st, 200)
        self.assertEqual(body, {"result": 99})       # from the out-param slot
        c = next(c for c in self.engine.calls if c[0] == "invoke_outparam")
        self.assertEqual(c[1], "floatset_value_n")
        self.assertEqual(c[2], [("ptr", ("H", "set_in", "x")), ("int", 0)])
        self.assertEqual(c[3], "double *")
        self.assertTrue(c[4])                        # presence_return
        # absent value -> no result -> 204
        self.engine.present = False
        st2, body2 = self.req("POST", "/floatset_value_n", {"s": "y", "n": 9})
        self.assertEqual(st2, 204)
        self.assertIsNone(body2)
        self.engine.present = True

    def test_opaque_outparam(self):
        st, body = self.req("POST", "/geoset_value_n", {"s": "x", "n": 1})
        self.assertEqual(st, 200)
        # opaque out-param pointer is encoded via the type's encoder
        self.assertEqual(body, {"result": "ENC(geo_as_ewkt)"})
        c = next(c for c in self.engine.calls if c[0] == "invoke_outparam")
        self.assertEqual(c[3], "GSERIALIZED **")
        enc = next(c for c in self.engine.calls if c[0] == "encode")
        self.assertEqual(enc[1], "geo_as_ewkt")
        self.assertEqual(enc[2], 99)                  # the out-param pointer
        self.engine.present = False
        st2, body2 = self.req("POST", "/geoset_value_n", {"s": "y", "n": 9})
        self.assertEqual(st2, 204)
        self.engine.present = True

    def test_input_array_builder(self):
        st, body = self.req("POST", "/temporal_merge_array",
                            {"temparr": ["a", "b", "c"]})
        self.assertEqual(st, 200)
        self.assertEqual(body, {"result": "ENC(temporal_out)"})
        decs = [c for c in self.engine.calls if c[0] == "decode"]
        self.assertEqual([c[1] for c in decs],
                         ["temporal_in"] * 3)          # each element decoded
        inv = next(c for c in self.engine.calls if c[0] == "invoke")
        tags = [t for t, _ in inv[2]]
        self.assertEqual(tags, ["ptrarray", "int"])    # array then count
        self.assertEqual(inv[2][1], ("int", 3))        # implicit count = len
        # validation: must be a list of strings
        self.assertEqual(self.req("POST", "/temporal_merge_array",
                                  {"temparr": "notlist"})[0], 400)
        self.assertEqual(self.req("POST", "/temporal_merge_array",
                                  {"temparr": [1, 2]})[0], 400)

    def test_array_return(self):
        st, body = self.req("POST", "/temporal_sequences", {"temp": "x"})
        self.assertEqual(st, 200)
        # each element pointer is encoded -> a JSON array
        self.assertEqual(body, {"result": ["ENC(tsequence_out)",
                                           "ENC(tsequence_out)"]})
        ia = next(c for c in self.engine.calls if c[0] == "invoke_array")
        self.assertEqual(ia[1], "temporal_sequences")
        self.assertEqual(ia[2], [("ptr", ("H", "temporal_in", "x"))])
        encs = [c for c in self.engine.calls if c[0] == "encode"]
        self.assertEqual([c[1] for c in encs], ["tsequence_out"] * 2)

    # --- validation ---
    def test_missing_field(self):
        st, body = self.req("POST", "/temporal_eq", {"temp1": "a"})
        self.assertEqual(st, 400)
        self.assertIn("missing required field: temp2", body["error"])

    def test_unexpected_field(self):
        st, body = self.req("POST", "/noop_op", {"x": 1})
        self.assertEqual(st, 400)
        self.assertIn("unexpected field", body["error"])

    def test_type_and_bool_rejection(self):
        st, b = self.req("POST", "/temporal_round",
                         {"temp": "x", "maxdd": "3"})
        self.assertEqual(st, 400)
        self.assertIn("maxdd must be of type integer", b["error"])
        st, _ = self.req("POST", "/temporal_round",
                         {"temp": "x", "maxdd": True})   # bool != integer
        self.assertEqual(st, 400)

    def test_bad_enum_value(self):
        st, b = self.req("POST", "/temporal_set_interp",
                         {"temp": "x", "interp": "NOPE"})
        self.assertEqual(st, 400)
        self.assertIn("must be one of", b["error"])

    def test_invalid_json(self):
        c = HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("POST", "/noop_op", "{not json",
                  {"Content-Type": "application/json"})
        r = c.getresponse()
        self.assertEqual(r.status, 400)
        c.close()

    def test_meos_error_envelope(self):
        self.engine.fail = True
        st, body = self.req("POST", "/temporal_eq",
                            {"temp1": "a", "temp2": "b"})
        self.assertEqual(st, 400)
        self.assertEqual(body, {"error": "boom", "code": 7})


class FakeFunc:
    def __init__(self, ret):
        self._ret = ret
        self.argtypes = None
        self.restype = "unset"
        self.called_with = None

    def __call__(self, *a):
        self.called_with = a
        return self._ret


class FakeLib:
    def __init__(self):
        self.funcs = {"d": FakeFunc(0xABCD), "f": FakeFunc(0x1234),
                      "e": FakeFunc(b"WKT-OUT")}

    def __getattr__(self, name):
        funcs = self.__dict__.get("funcs", {})
        if name not in funcs:
            raise AttributeError(name)
        return funcs[name]


class FakeCtypes:
    c_void_p = "void_p"
    c_char_p = "char_p"
    c_long = "long"
    c_double = "double"
    c_int = "int"


class CtypesEngineTests(unittest.TestCase):
    def _engine(self):
        e = object.__new__(CtypesEngine)
        e._ct = FakeCtypes
        e.lib = FakeLib()
        e._argmap = {"ptr": "void_p", "str": "char_p", "int": "long",
                     "double": "double", "bool": "int", "enum": "int"}
        e._retmap = {"ptr": "void_p", "str": "char_p", "int": "long",
                     "double": "double", "bool": "int"}
        e._last_error = None
        return e

    def test_decode_sets_types_and_encodes(self):
        e = self._engine()
        h = e.decode("d", "WKT")
        f = e.lib.funcs["d"]
        self.assertEqual(f.argtypes, ["char_p"])
        self.assertEqual(f.restype, "void_p")
        self.assertEqual(f.called_with, (b"WKT",))
        self.assertEqual(h, 0xABCD)

    def test_invoke_maps_arg_and_result_tags(self):
        e = self._engine()
        out = e.invoke("f", [("ptr", 0xABCD), ("int", 5)], "ptr")
        f = e.lib.funcs["f"]
        self.assertEqual(f.argtypes, ["void_p", "long"])
        self.assertEqual(f.restype, "void_p")
        self.assertEqual(f.called_with, (0xABCD, 5))
        self.assertEqual(out, 0x1234)

    def test_invoke_void_restype_none(self):
        e = self._engine()
        e.invoke("f", [], "void")
        self.assertIsNone(e.lib.funcs["f"].restype)

    def test_encode_decodes_bytes(self):
        e = self._engine()
        self.assertEqual(e.encode("e", 0xABCD), "WKT-OUT")

    def test_aux_args_appended_to_signature(self):
        # decode(str, +aux) and encode(handle, +aux): the trailing
        # formatting scalars (e.g. maxdd) extend argtypes and the call.
        e = self._engine()
        e.decode("d", "WKT", aux=[("int", 15)])
        d = e.lib.funcs["d"]
        self.assertEqual(d.argtypes, ["char_p", "long"])
        self.assertEqual(d.called_with, (b"WKT", 15))
        e.encode("e", 0xABCD, aux=[("int", 15)])
        f = e.lib.funcs["e"]
        self.assertEqual(f.argtypes, ["void_p", "long"])
        self.assertEqual(f.called_with, (0xABCD, 15))

    def test_unknown_symbol_raises_meos_error(self):
        e = self._engine()
        with self.assertRaises(MeosError):
            e.decode("missing", "x")


if __name__ == "__main__":
    unittest.main(verbosity=2)
