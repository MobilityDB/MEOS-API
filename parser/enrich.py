"""Service-projection enrichment.

Derives, for every function and type in the parsed catalog, the metadata a
service generator (OpenAPI, MCP, gRPC, ...) needs but that *cannot* be read
from C headers:

- ``category``     — a coarse semantic class (constructor, predicate, io, ...).
- ``typeEncodings``— for each opaque C type, how it round-trips to the wire
                      (text / MF-JSON / WKB) and the function names that do it.
- ``network``      — whether the function can be projected onto a *stateless*
                      endpoint, and if not, why.
- ``wire``         — per-parameter and return value, the concrete request /
                      response representation a generator should emit.

Everything here is a heuristic *default*. The pass runs before the manual
merge step, so any field can be overridden per function/type from
``meta/meos-meta.json`` (the merger applies on top). This module is
deliberately free of any libclang dependency: it operates purely on the
parsed ``idl`` dict and is therefore unit-testable on its own.

Note: libclang emits *canonical* C spellings — ``struct Temporal *`` (not
``Temporal *``), ``unsigned char`` (not ``uint8_t``), ``long`` (not
``int64_t``), and MEOS uses ``int`` for booleans. The heuristics below match
those canonical spellings. See ``docs/enrichment.md`` for the full contract.
"""

import re

# Ordered category vocabulary. The first matching rule wins.
CATEGORIES = (
    "lifecycle",       # library/process setup, configuration, teardown
    "index",           # in-memory index objects (RTree, ...)
    "io",              # parse/serialize between a value and a wire encoding
    "aggregate",       # aggregate transition/combine/final functions
    "predicate",       # boolean question about value(s)
    "constructor",     # build a value (_make, _copy)
    "setop",           # set algebra (union/intersection/minus/...)
    "conversion",      # convert one value into another representation
    "accessor",        # read a component/property of a value
    "transformation",  # value -> value of the same family
    "other",           # anything not classified above
)

# Canonical scalar spellings as emitted by libclang.
_INT_BASES = {
    "char", "signed char", "unsigned char",
    "short", "unsigned short", "int", "unsigned int",
    "long", "unsigned long", "long long", "unsigned long long",
}
_FLOAT_BASES = {"float", "double", "long double"}
_BOOL_BASES = {"bool", "_Bool"}
# char-like pointers carry text or bytes (WKB) — represented as a JSON string.
_STRING_PTR_BASES = {"char", "signed char", "unsigned char"}

# name suffix -> wire encoding produced (encoder) / consumed (decoder)
_ENCODERS = [
    (re.compile(r"_out$"), "text"),
    (re.compile(r"_as_text$"), "text"),
    (re.compile(r"_as_e?wkt$"), "text"),
    (re.compile(r"_as_mfjson$"), "mfjson"),
    (re.compile(r"_as_geojson$"), "mfjson"),
    (re.compile(r"_as_hex_?wkb$"), "wkb"),
    (re.compile(r"_as_e?wkb$"), "wkb"),
]
_DECODERS = [
    (re.compile(r"_in$"), "text"),
    (re.compile(r"_from_e?wkt$"), "text"),
    (re.compile(r"_from_text$"), "text"),
    (re.compile(r"_from_mfjson$"), "mfjson"),
    (re.compile(r"_from_geojson$"), "mfjson"),
    (re.compile(r"_from_hex_?wkb$"), "wkb"),
    (re.compile(r"_from_e?wkb$"), "wkb"),
]
_IO_RE = [rx for rx, _ in _DECODERS + _ENCODERS]

_LIFECYCLE_RE = re.compile(r"^meos_")
_INDEX_RE = re.compile(r"^rtree_")
_AGG_RE = re.compile(r"(_transfn|_combinefn|_finalfn)$|_tagg|_collect$")
_CONSTRUCTOR_RE = re.compile(r"(_make|_copy)$")
_SETOP_RE = re.compile(r"^(union|intersection|minus|difference)_")
_CONVERSION_RE = re.compile(r"_to_|_from_base|_from_|_as_")
_ACCESSOR_RE = re.compile(
    r"(_values?|_start_value|_end_value|_min_value|_max_value|_srid|_timespan|"
    r"_duration|_length|_num_[a-z]+|_n|_lower|_upper|_start_[a-z]+|_end_[a-z]+|"
    r"_get_[a-z]+|_value_n)$"
)
# MEOS predicates return `int`, so they must be recognised by name.
_PREDICATE_RE = re.compile(
    r"^(ever|always)_|(_eq|_ne|_lt|_le|_gt|_ge|_cmp)$|"
    r"^(contains|contained|overlaps|overbefore|overafter|overleft|overright|"
    r"overbelow|overabove|overfront|overback|left|right|below|above|front|"
    r"back|before|after|adjacent|same|intersects|disjoint|touches|dwithin|"
    r"covers|coveredby|equals|crosses|within|relate)_|"
    r"^t(eq|ne|lt|le|gt|ge|contains|intersects|disjoint|touches|dwithin)_"
)

_QUAL_RE = re.compile(r"\b(const|volatile|struct|union|enum)\b")


def _base(c_type: str) -> str:
    """Bare type token: qualifiers, ``struct``/``union``/``enum`` and ``*``
    stripped (so ``const struct Temporal *`` -> ``Temporal``)."""
    return " ".join(_QUAL_RE.sub(" ", c_type).replace("*", " ").split())


def _ptr_depth(c_type: str) -> int:
    return c_type.count("*")


def _scalar_wire(c_type: str, enums: set):
    """Wire descriptor for a non-opaque scalar/string/enum, else ``None``."""
    base, depth = _base(c_type), _ptr_depth(c_type)
    if base == "void" and depth == 0:
        return {"kind": "void"}
    # char-likes and PostgreSQL `text` are just strings on the wire.
    if depth == 1 and (base in _STRING_PTR_BASES or base == "text"):
        return {"kind": "json", "json": "string"}
    if depth == 0:
        if base in _BOOL_BASES:
            return {"kind": "json", "json": "boolean"}
        if base in _FLOAT_BASES:
            return {"kind": "json", "json": "number"}
        if base in _INT_BASES:
            return {"kind": "json", "json": "integer"}
        if base in enums:
            return {"kind": "json", "json": "string", "enum": base}
    return None


def _is_scalar_pointer(c_type: str, enums: set) -> bool:
    """``int *`` / ``double *`` / ``interpType *`` etc. — an array/out-param,
    as opposed to a pointer to an opaque value (``struct Temporal *``)."""
    base, depth = _base(c_type), _ptr_depth(c_type)
    if depth >= 2:
        return True
    if depth == 1 and base not in _STRING_PTR_BASES:
        return (base in _INT_BASES or base in _FLOAT_BASES
                or base in _BOOL_BASES or base in enums)
    return False


def _aux_specs(params: list):
    """Defaults for the trailing args of an in/out helper.

    Real MEOS in/out helpers are not pure ``(str)->T`` / ``(T)->str``: they
    take trailing *formatting* scalars — ``temporal_out(temp, int maxdd)``,
    ``*_as_mfjson(temp, with_bbox, flags, precision, srs)``. Those are safe
    to default, so the helper still satisfies the stateless contract.

    Returns the aux spec list (one ``{name, kind, default}`` per trailing
    param), or ``None`` if any trailing parameter is *not* a defaultable
    formatting scalar — a semantic ``*type`` tag (``temporal_in``'s
    ``temptype``), a pointer/array (``*_as_wkb``'s ``size_out``), etc. —
    which disqualifies the helper entirely.
    """
    specs = []
    for p in params:
        sc = _scalar_wire(p["canonical"], set())
        if sc is None or sc.get("kind") != "json":
            return None                      # pointer / array / opaque aux
        nm = p["name"].lower()
        if "type" in nm:                     # temptype/basetype/settype tag
            return None
        j = sc["json"]
        if j == "integer":
            default = (15 if any(k in nm for k in
                                 ("maxdd", "decimal", "digit", "precision"))
                       else 0)
        elif j == "number":
            default = 0.0
        elif j == "boolean":
            default = False
        else:                                # string (e.g. srs) -> NULL
            default = None
        specs.append({"name": p["name"], "kind": j, "default": default})
    return specs


def build_type_encodings(functions: list, structs: set) -> dict:
    """Scan the catalog for the in/out functions of every opaque struct.

    A *decoder* turns a wire string into an object (returns ``struct T *``,
    first arg a char-like string). An *encoder* turns an object into a wire
    string (returns ``char *``, first arg ``const struct T *``). Trailing
    *formatting* scalars are allowed and defaulted (see ``_aux_specs``); a
    non-defaultable trailing arg disqualifies the helper. Only declared
    structs qualify, so primitives never register by accident.
    """
    enc: dict[str, dict] = {}

    def slot(b: str) -> dict:
        return enc.setdefault(b, {"encodings": set(), "encoders": {},
                                  "decoders": {}})

    for fn in functions:
        name = fn["name"]
        ret = fn["returnType"]["canonical"]
        params = fn.get("params", [])
        if not params:
            continue
        p0 = params[0]["canonical"]
        rb, rd = _base(ret), _ptr_depth(ret)
        pb, pd = _base(p0), _ptr_depth(p0)
        aux = _aux_specs(params[1:])     # None => non-defaultable trailing arg

        # Decoder: const char* (+ defaultable scalar aux) -> opaque struct
        if (aux is not None and rd >= 1 and rb in structs
                and pd == 1 and pb in _STRING_PTR_BASES):
            for rx, encoding in _DECODERS:
                if rx.search(name):
                    s = slot(rb)
                    s["encodings"].add(encoding)
                    s["decoders"].setdefault(encoding, {})[name] = aux
                    break

        # Encoder: const struct T* (+ defaultable scalar aux) -> char*
        if (aux is not None and rd == 1 and rb in _STRING_PTR_BASES
                and pd >= 1 and pb in structs):
            for rx, encoding in _ENCODERS:
                if rx.search(name):
                    s = slot(pb)
                    s["encodings"].add(encoding)
                    s["encoders"].setdefault(encoding, {})[name] = aux
                    break

    # Several typed functions can serve one encoding (e.g. tbool_in,
    # tint_in, ... all decode a `Temporal *`). Prefer the *generic root*
    # (`<type>_in` / `_out`) so the chosen in/out works for every subtype
    # (and `temporal_out` correctly serialises any subtype); fall back to a
    # deterministic alphabetical pick otherwise.
    order = ("text", "mfjson", "wkb")
    dec_suffix = {"text": "_in", "mfjson": "_from_mfjson",
                  "wkb": "_from_hexwkb"}
    enc_suffix = {"text": "_out", "mfjson": "_as_mfjson",
                  "wkb": "_as_hexwkb"}

    def choose(cands: dict, base: str, suffix: str) -> str:
        generic = base.lower() + suffix
        return generic if generic in cands else sorted(cands)[0]

    out: dict[str, dict] = {}
    for base, s in enc.items():
        dec = {e: choose(c, base, dec_suffix[e])
               for e, c in s["decoders"].items()}
        encd = {e: choose(c, base, enc_suffix[e])
                for e, c in s["encoders"].items()}
        in_e = next((e for e in order if e in dec), None)
        out_e = next((e for e in order if e in encd), None)
        out[base] = {
            "encodings": sorted(s["encodings"]),
            "decoders": dec,
            "encoders": encd,
            "in": dec.get(in_e) if in_e else None,
            "out": encd.get(out_e) if out_e else None,
            "in_aux": s["decoders"][in_e][dec[in_e]] if in_e else [],
            "out_aux": s["encoders"][out_e][encd[out_e]] if out_e else [],
        }
    return out


def classify_category(fn: dict) -> str:
    name = fn["name"]
    ret = fn["returnType"]["canonical"]

    if _LIFECYCLE_RE.match(name):
        return "lifecycle"
    if _INDEX_RE.match(name):
        return "index"
    if any(rx.search(name) for rx in _IO_RE):
        return "io"
    if _AGG_RE.search(name):
        return "aggregate"
    if _PREDICATE_RE.search(name) or _base(ret) in _BOOL_BASES:
        return "predicate"
    if _CONSTRUCTOR_RE.search(name):
        return "constructor"
    if _SETOP_RE.match(name):
        return "setop"
    if _CONVERSION_RE.search(name):
        return "conversion"
    if _ACCESSOR_RE.search(name):
        return "accessor"
    return "transformation"


# MEOS splits its API into a public *user* surface and an *internal* programmer
# surface. The single authored signal for that split is the doxygen `@ingroup`:
# a function is public iff it carries a group that is not `meos_internal_*`; a
# function with no group, or a `meos_internal_*` group, is internal. This ties
# the binding/network surface to the reference manual — the two derive from one
# human-authored tag and cannot drift.


def _outparam(fn: dict, enums: set, type_encodings: dict):
    """A MEOS accessor of the form ``bool f(.., T *result)`` returns its
    value through a trailing out-parameter, with the ``bool``/``int`` return
    (or ``void``) as a presence flag. Two safe shapes:

    - ``scalar``: ``T *result`` where ``T`` is a JSON scalar.
    - ``opaque``: ``T **result`` where ``T`` has an encoder (serialised).

    Returns ``(leading_params, outparam, mode)`` or ``(None, None, None)``.
    """
    ps = fn.get("params", [])
    ret = fn["returnType"]["canonical"]
    if (not ps or _ptr_depth(ret) != 0
            or _base(ret) not in ("bool", "_Bool", "int", "void")):
        return None, None, None
    last = ps[-1]
    if last["name"] not in ("result", "value"):
        return None, None, None
    c = last["canonical"]
    if _ptr_depth(c) == 1:
        pointee = c.replace("const", "").replace("*", "").strip()
        sw = _scalar_wire(pointee, enums)
        if sw is not None and sw.get("kind") == "json":
            return ps[:-1], last, "scalar"
    elif _ptr_depth(c) == 2:
        te = type_encodings.get(_base(c))
        if te and te.get("out"):
            return ps[:-1], last, "opaque"
    return None, None, None


def _array_param(params: list, type_encodings: dict):
    """A MEOS builder takes an element array as a ``(Elem **arr, int count)``
    pair. Detect the first such pair whose element type is decodable, so the
    array can be projected as a JSON list (the ``count`` is then implicit).
    Returns ``(arr_index, count_index)`` or ``(None, None)``.
    """
    for i, p in enumerate(params):
        c = p["canonical"]
        if (_ptr_depth(c) == 2 and p["name"] not in ("result", "value")
                and i + 1 < len(params)):
            te = type_encodings.get(_base(c))
            nxt = params[i + 1]["canonical"]
            if (te and te.get("in") and _base(nxt) == "int"
                    and _ptr_depth(nxt) == 0):
                return i, i + 1
    return None, None


def _array_return(fn: dict, type_encodings: dict):
    """A MEOS accessor that returns a freshly-allocated element array as
    ``Elem **f(.., int *count)`` (e.g. ``temporal_sequences``). The element
    type must be encodable. Returns ``(count_param_name, elem_base)`` or
    ``(None, None)``.
    """
    ret = fn["returnType"]["canonical"]
    if _ptr_depth(ret) != 2:
        return None, None
    rb = _base(ret)
    te = type_encodings.get(rb)
    if not (te and te.get("out")):
        return None, None
    cnt = [p for p in fn.get("params", [])
           if _ptr_depth(p["canonical"]) == 1
           and _base(p["canonical"]) in ("int", "long")
           and p["name"] in ("count", "n", "nvalues", "size", "npoints")]
    if len(cnt) != 1:
        return None, None
    return cnt[0]["name"], rb


def assess(fn: dict, type_encodings: dict, enums: set) -> tuple:
    """Return ``(network, wire)`` for one function.

    Exposable over a stateless endpoint iff every parameter can be decoded
    from the request and the return can be encoded into the response.
    Pointer-to-scalar parameters (arrays / out-params) and opaque types
    lacking an in/out function make it non-exposable; the reason is recorded.
    """
    reasons: list[str] = []
    wire_params = []

    out_lead, out_p, out_mode = _outparam(fn, enums, type_encodings)
    eff = out_lead if out_p is not None else fn.get("params", [])
    arr_i, count_i = _array_param(eff, type_encodings)
    ret_count_name, ret_elem = _array_return(fn, type_encodings)
    ret_count_i = (next((i for i, p in enumerate(eff)
                         if p["name"] == ret_count_name), None)
                   if ret_elem is not None else None)

    for idx, p in enumerate(eff):
        if idx == count_i or idx == ret_count_i:
            continue                       # array length is implicit
        c = p["canonical"]
        if idx == arr_i:
            elem = type_encodings[_base(c)]
            wire_params.append({
                "name": p["name"], "kind": "array",
                "count_param": eff[count_i]["name"],
                "element": {
                    "kind": "serialized",
                    "cType": " ".join(c.replace("*", " ").split()) + " *",
                    "decode": elem["in"],
                    "decode_aux": elem.get("in_aux", []),
                    "encodings": elem["encodings"],
                },
            })
            continue
        scalar = _scalar_wire(c, enums)
        if scalar is not None:
            wire_params.append({"name": p["name"], **scalar})
            continue
        if _is_scalar_pointer(c, enums):
            reasons.append(f"array-or-out-param:{p['name']}")
            wire_params.append({"name": p["name"], "kind": "unsupported"})
            continue
        base = _base(c)
        te = type_encodings.get(base)
        if te and te["in"]:
            wire_params.append({
                "name": p["name"], "kind": "serialized", "cType": c,
                "decode": te["in"], "decode_aux": te.get("in_aux", []),
                "encodings": te["encodings"],
            })
        else:
            reasons.append(f"no-decoder:{base}")
            wire_params.append({"name": p["name"], "kind": "unsupported"})

    ret = fn["returnType"]["canonical"]
    if out_p is not None:
        # a bool/int C return is a presence flag; void = always present
        presence = _base(ret) in ("bool", "_Bool", "int")
        if out_mode == "scalar":
            pointee = out_p["canonical"].replace("const", "").replace(
                "*", "").strip()
            wire_result = {
                **_scalar_wire(pointee, enums),     # kind:"json", json:…
                "from_outparam": out_p["name"],
                "out_ctype": out_p["canonical"],
                "presence_return": presence,
            }
        else:                                       # opaque T **result
            te = type_encodings[_base(out_p["canonical"])]
            wire_result = {
                "kind": "serialized",
                "cType": out_p["canonical"],
                "encode": te["out"], "encode_aux": te.get("out_aux", []),
                "encodings": te["encodings"],
                "from_outparam": out_p["name"],
                "out_ctype": out_p["canonical"],
                "presence_return": presence,
            }
        scalar = "handled"
    elif ret_elem is not None:
        te = type_encodings[ret_elem]
        wire_result = {
            "kind": "array",
            "element": {
                "kind": "serialized",
                "cType": " ".join(ret.replace("*", " ").split()) + " *",
                "encode": te["out"], "encode_aux": te.get("out_aux", []),
                "encodings": te["encodings"],
            },
            "count_outparam": ret_count_name,
        }
        scalar = "handled"
    else:
        scalar = _scalar_wire(ret, enums)
    if scalar == "handled":
        pass
    elif scalar is not None:
        wire_result = scalar
    elif _is_scalar_pointer(ret, enums):
        reasons.append(f"unsupported-return:{ret}")
        wire_result = {"kind": "unsupported"}
    else:
        base, depth = _base(ret), _ptr_depth(ret)
        te = type_encodings.get(base)
        if depth == 1 and te and te["out"]:
            wire_result = {"kind": "serialized", "cType": ret,
                           "encode": te["out"],
                           "encode_aux": te.get("out_aux", []),
                           "encodings": te["encodings"]}
        else:
            reasons.append(
                f"no-encoder:{base}" if depth == 1
                else f"unsupported-return:{ret}"
            )
            wire_result = {"kind": "unsupported"}

    if fn["category"] in ("lifecycle", "index"):
        reasons.insert(0, fn["category"])
    if fn.get("api") == "internal":
        reasons.insert(0, "internal")

    exposable = not reasons
    network = {
        "exposable": exposable,
        "method": "POST" if exposable else None,
        "reason": None if exposable else "; ".join(dict.fromkeys(reasons)),
    }
    return network, {"params": wire_params, "result": wire_result}


def enrich_idl(idl: dict) -> dict:
    """Augment ``idl`` in place with service-projection metadata."""
    functions = idl.get("functions", [])
    struct_names = {s["name"] for s in idl.get("structs", [])}
    enum_names = {e["name"] for e in idl.get("enums", [])}

    # An opaque type is any *named* pointer type that is not a scalar / enum
    # / string. Beyond parsed structs this also covers reconciled
    # PostgreSQL/PostGIS types (`Interval`, `GBOX`, ...) so their own in/out
    # wrappers can register a codec instead of being dead `no-decoder`s.
    _scalarish = (_INT_BASES | _FLOAT_BASES | _BOOL_BASES | _STRING_PTR_BASES
                  | enum_names | {"void", "text"})
    opaque_names = set(struct_names)
    for fn in functions:
        for c in ([fn["returnType"]["canonical"]]
                  + [p["canonical"] for p in fn.get("params", [])]):
            b = _base(c)
            if _ptr_depth(c) >= 1 and b and b not in _scalarish:
                opaque_names.add(b)

    type_encodings = build_type_encodings(functions, opaque_names)

    for fn in functions:
        group = fn.get("group")
        fn["api"] = ("internal"
                     if (not group or group.startswith("meos_internal_"))
                     else "public")
        fn["category"] = classify_category(fn)
        network, wire = assess(fn, type_encodings, enum_names)
        fn["network"] = network
        fn["wire"] = wire

    for struct in idl.get("structs", []):
        te = type_encodings.get(struct["name"])
        if te:
            struct["serialization"] = {
                "encodings": te["encodings"], "in": te["in"], "out": te["out"],
            }
    idl["typeEncodings"] = type_encodings

    counts: dict[str, int] = {}
    for fn in functions:
        counts[fn["category"]] = counts.get(fn["category"], 0) + 1
    public = [fn for fn in functions if fn["api"] == "public"]
    idl["enrichment"] = {
        "categoryCounts": counts,
        "publicFunctions": len(public),
        "internalFunctions": len(functions) - len(public),
        # Internal functions are policy-excluded, so this equals the
        # public-exposable count — the meaningful parity numerator.
        "exposableFunctions": sum(
            1 for fn in functions if fn["network"]["exposable"]
        ),
    }
    return idl
