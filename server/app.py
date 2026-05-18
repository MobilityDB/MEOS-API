"""Contract-driven runtime HTTP server.

Builds its entire routing + request-validation + dispatch table from the
**enriched catalog** (`network` / `wire`), the same single source the
OpenAPI and MCP generators consume — the server is just another projection,
the one that *executes*.

For every stateless-exposable function it exposes ``POST /{function}``:

1. validate the JSON body against the `wire` parameter model;
2. ``engine.decode`` each serialized string into an opaque handle;
3. ``engine.invoke`` the function with the (scalars + handles);
4. ``engine.encode`` an opaque result back to a string;
5. reply ``{"result": …}`` (``204`` for void, ``400 {"error","code"}`` on a
   MEOS/validation error, ``404`` unknown route).

Stdlib only (`http.server`) — a reference/embeddable server, not a tuned
production stack. The MEOS work is entirely behind the ``Engine`` seam.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from server.engine import Engine, MeosError

_JSON_PYTYPE = {
    "integer": int, "number": (int, float), "boolean": bool, "string": str,
}
_RESULT_TAG = {"integer": "int", "number": "double",
               "boolean": "bool", "string": "str"}


def build_enum_map(catalog: dict) -> dict:
    return {
        e["name"]: {v["name"]: v["value"] for v in e.get("values", [])}
        for e in catalog.get("enums", [])
    }


def build_routes(catalog: dict) -> dict:
    """`/{fn}` → route descriptor, for every exposable function."""
    routes = {}
    for fn in catalog.get("functions", []):
        if not fn.get("network", {}).get("exposable"):
            continue
        w = fn["wire"]
        params = []
        for p in w["params"]:
            if p["kind"] == "array":
                params.append({
                    "name": p["name"], "kind": "array",
                    "count_param": p["count_param"],
                    "element": p["element"],
                })
            else:
                params.append({
                    "name": p["name"], "kind": p["kind"],
                    "json": p.get("json"), "enum": p.get("enum"),
                    "decode": p.get("decode"),
                    "decode_aux": p.get("decode_aux", []),
                    "cType": p.get("cType"),
                })
        r = w["result"]
        routes["/" + fn["name"]] = {
            "name": fn["name"],
            "category": fn.get("category"),
            "params": params,
            "result": {"kind": r["kind"], "json": r.get("json"),
                       "encode": r.get("encode"),
                       "encode_aux": r.get("encode_aux", []),
                       "from_outparam": r.get("from_outparam"),
                       "out_ctype": r.get("out_ctype"),
                       "presence_return": r.get("presence_return", False),
                       "element": r.get("element"),
                       "count_outparam": r.get("count_outparam")},
        }
    return routes


def _validate(body: dict, route: dict, enums: dict) -> None:
    if not isinstance(body, dict):
        raise ValueError("request body must be a JSON object")
    names = {p["name"] for p in route["params"]}
    extra = set(body) - names
    if extra:
        raise ValueError(f"unexpected field(s): {', '.join(sorted(extra))}")
    for p in route["params"]:
        if p["name"] not in body:
            raise ValueError(f"missing required field: {p['name']}")
        v = body[p["name"]]
        if p["kind"] == "array":
            if not isinstance(v, list) or not all(
                    isinstance(x, str) for x in v):
                raise ValueError(
                    f"{p['name']} must be an array of strings")
        elif p["kind"] == "serialized":
            if not isinstance(v, str):
                raise ValueError(f"{p['name']} must be a string")
        elif p["enum"]:
            if v not in enums.get(p["enum"], {}):
                raise ValueError(
                    f"{p['name']} must be one of "
                    f"{sorted(enums.get(p['enum'], {}))}")
        else:
            ok = _JSON_PYTYPE.get(p["json"], object)
            # bool is an int subclass — reject it for numeric fields.
            if (p["json"] in ("integer", "number") and isinstance(v, bool)) \
                    or not isinstance(v, ok):
                raise ValueError(
                    f"{p['name']} must be of type {p['json']}")


_KIND_TAG = {"integer": "int", "number": "double",
             "boolean": "bool", "string": "str"}


def _aux_args(specs: list) -> list:
    """Catalog aux specs -> engine ``(tag, value)`` pairs (server defaults)."""
    out = []
    for a in specs:
        tag = _KIND_TAG.get(a["kind"], "str")
        val = a["default"]
        if tag == "bool":
            val = 1 if val else 0
        out.append((tag, val))
    return out


def _dispatch(body: dict, route: dict, engine: Engine, enums: dict):
    args = []
    for p in route["params"]:
        v = body[p["name"]]
        if p["kind"] == "array":
            el = p["element"]
            handles = [engine.decode(el["decode"], s,
                                     _aux_args(el.get("decode_aux", [])))
                       for s in v]
            args.append(("ptrarray", handles))   # Elem **arr
            args.append(("int", len(handles)))   # the implicit count
            continue
        if p["kind"] == "serialized":
            args.append(("ptr", engine.decode(
                p["decode"], v, _aux_args(p.get("decode_aux", [])))))
        elif p["enum"]:
            args.append(("enum", enums[p["enum"]][v]))
        elif p["json"] == "integer":
            args.append(("int", int(v)))
        elif p["json"] == "number":
            args.append(("double", float(v)))
        elif p["json"] == "boolean":
            args.append(("bool", 1 if v else 0))
        else:
            args.append(("str", v))

    res = route["result"]
    if res.get("from_outparam"):
        # bool f(.., T *result): the value comes back through the trailing
        # out-parameter; the C return is a presence flag (void = always).
        present, val = engine.invoke_outparam(
            route["name"], args, res["out_ctype"], res["presence_return"])
        if not present:
            return None
        if res["kind"] == "serialized":         # opaque out-param -> encode
            return {"result": engine.encode(
                res["encode"], val, _aux_args(res.get("encode_aux", [])))}
        return {"result": bool(val) if res.get("json") == "boolean" else val}

    if res["kind"] == "array":
        # Elem **f(.., int *count): MEOS returns a fresh array + byref count.
        el = res["element"]
        ptrs = engine.invoke_array(route["name"], args)
        return {"result": [engine.encode(el["encode"], p,
                                         _aux_args(el.get("encode_aux", [])))
                           for p in ptrs]}

    rtag = "void" if res["kind"] == "void" else (
        "ptr" if res["kind"] == "serialized"
        else _RESULT_TAG.get(res["json"], "str"))

    out = engine.invoke(route["name"], args, rtag)
    if res["kind"] == "void":
        return None
    if res["kind"] == "serialized":
        return {"result": engine.encode(
            res["encode"], out, _aux_args(res.get("encode_aux", [])))}
    if res["kind"] == "json" and res["json"] == "boolean":
        return {"result": bool(out)}
    return {"result": out}


def make_handler(routes: dict, engine: Engine, enums: dict):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):  # silence default stderr spam
            pass

        def _send(self, code: int, payload):
            data = b"" if payload is None else json.dumps(payload).encode()
            self.send_response(code)
            if data:
                self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            if data:
                self.wfile.write(data)

        def do_GET(self):
            if self.path in ("/", "/healthz"):
                return self._send(200, {
                    "service": "meos", "status": "ok",
                    "engine": getattr(engine, "name", "engine"),
                    "operations": len(routes),
                })
            self._send(404, {"error": f"no such resource: {self.path}"})

        def do_POST(self):
            route = routes.get(self.path)
            if route is None:
                return self._send(
                    404, {"error": f"no such operation: {self.path}"})
            try:
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b"{}"
                body = json.loads(raw or b"{}")
            except (ValueError, json.JSONDecodeError):
                return self._send(400, {"error": "invalid JSON body"})
            try:
                _validate(body, route, enums)
                result = _dispatch(body, route, engine, enums)
            except MeosError as e:
                return self._send(400, {"error": str(e), "code": e.code})
            except (ValueError, KeyError) as e:
                return self._send(400, {"error": str(e)})
            if result is None:
                return self._send(204, None)
            self._send(200, result)

    return Handler


def make_server(catalog: dict, engine: Engine, host="127.0.0.1", port=8080):
    routes = build_routes(catalog)
    enums = build_enum_map(catalog)
    handler = make_handler(routes, engine, enums)
    return ThreadingHTTPServer((host, port), handler)
