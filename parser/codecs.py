"""State the codec of every class a binding carries, from the functions MEOS declares and
the SQL surface that registers them.

#build_type_encodings of parser/enrich.py reads a class's readers and writers from the
shape of the C functions alone, before any SQL fact is in the catalog, and keeps one of
each per encoding. This pass completes each class once the SQL signatures and the bound
literals are attached:

- A class that several SQL types share reads each type's text or MF-JSON form through that
  type's own public reader, so it states them in ``readers`` keyed by the SQL type each
  reader returns (``Set: {text: {intset: intset_in, bigintset: bigintset_in, ...}}``) and
  states no single decoder, nor ``in``, for that encoding; it writes each type through
  that type's own public writer, stated in ``writers`` keyed by the SQL type each writer
  takes (``Set: {text: {intset: intset_out, ...}}``), with no single encoder, nor
  ``out``, for that encoding.
- The hex-WKB writer is the class's ``wkb`` encoder. Its size is an out-parameter, stated
  in the function's ``shape.outParams`` as for any other function, and its ``variant`` is
  the value the type's own ``send`` binds (``WKB_EXTENDED``), else the value its SQL hex
  writer passes when the call leaves the byte order out (``asHexWKB(raster, endian
  DEFAULT '')`` passes 0, as ``wkb_variant_from_endian`` reads an empty order).
- Every decoder and encoder states its trailing inputs by name with the value a binding
  passes, in ``decoderAux`` and ``encoderAux`` keyed by encoding, every reader and writer
  of a SQL type in ``readerAux`` and ``writerAux`` keyed as ``readers`` and ``writers``
  are, and the byte codec its own in ``bytes.encoderAux``: a binding fills each parameter
  by name and refuses one the catalog does not fill.
- A value whose slot carries a ``typedef`` (``H3Index``, ``Quadbin``, ``S2CellId``) is a
  class of its own, keyed by that name.
"""
import re

from parser.enrich import _DECODERS, _ENCODERS, _STRING_PTR_BASES, _aux_specs, _base

_ORDER = ("text", "mfjson", "wkb")


def _cls(slot):
    """The class of a slot: the type of its own it names, else its base type."""
    return slot.get("typedef") or _base(slot.get("canonical") or slot.get("c")
                                        or slot.get("cType") or "")


def _encoding(name, table):
    for rx, encoding in table:
        if rx.search(name):
            return encoding
    return None


def _literal(value, macros):
    """The number a bound literal names: a macro's value or an integer literal."""
    if value in macros:
        return macros[value]
    return int(value) if re.fullmatch(r"-?\d+", str(value)) else None


def _variant(cls, fns, macros, errors):
    """The WKB variant a binding passes for ``cls``: the one the type's own ``send``
    binds, else the one its SQL hex writer passes when the byte order is left out."""
    bound = set()
    for f in fns:
        ps = f.get("params") or []
        if (f.get("api") == "public" and f["name"].endswith("_as_wkb") and ps
                and _cls(ps[0]) == cls):
            for s in f.get("sqlSignatures") or ():
                v = (s.get("boundArgs") or {}).get("variant")
                if v is not None and (s.get("sqlName") or "").endswith("_send"):
                    bound.add(_literal(v, macros))
    if len(bound) > 1:
        errors.append(f"{cls}: its send functions bind variants {sorted(bound)}")
    if len(bound) == 1:
        return bound.pop()
    for f in fns:
        ps = f.get("params") or []
        if (f.get("api") == "public" and f["name"].endswith("_as_hexwkb") and ps
                and _cls(ps[0]) == cls):
            for s in f.get("sqlSignatures") or ():
                dflts = s.get("argDefaults") or []
                if len(dflts) > 1 and dflts[1] == "''":
                    return 0
    return None


def _trailing(fn, skip_first=True):
    """The aux of ``fn``'s trailing inputs, its out-parameters set aside, or None when
    one is not defaultable (#_aux_specs of parser/enrich.py)."""
    out = set(((fn.get("shape") or {}).get("outParams")) or ())
    params = [p for p in (fn.get("params") or [])[1 if skip_first else 0:]
              if p["name"] not in out]
    return _aux_specs(params)


def state_type_encodings(idl):
    """(idl, errors): ``idl["typeEncodings"]`` and each struct's ``serialization`` stated
    as this module's docstring describes, and the classes whose codec contradicts
    itself."""
    fns = [f for f in idl.get("functions", []) if f.get("api") == "public"]
    macros = {m["name"]: m.get("value") for m in idl.get("macros", [])}
    encs = idl.setdefault("typeEncodings", {})
    errors = []

    decoders, encoders = {}, {}
    for f in fns:
        ps = f.get("params") or []
        if not ps:
            continue
        ret, p0 = f["returnType"], ps[0]
        # a reader: one string in, a value of the class out
        enc = _encoding(f["name"], _DECODERS)
        if (enc and "*" in (p0.get("cType") or "") and _base(p0["cType"]) in _STRING_PTR_BASES
                and (("*" in (ret.get("c") or "")) or ret.get("typedef"))):
            aux = _trailing(f)
            if aux is not None:
                decoders.setdefault(_cls(ret), {}).setdefault(enc, []).append((f, aux))
        # a writer: a value of the class in, a string out
        enc = _encoding(f["name"], _ENCODERS)
        if (enc and _base(ret.get("c") or "") in _STRING_PTR_BASES and "*" in (ret.get("c") or "")
                and ("*" in (p0.get("cType") or "") or p0.get("typedef"))):
            aux = _trailing(f)
            if aux is not None:
                encoders.setdefault(_cls(p0), {}).setdefault(enc, []).append((f, aux))

    def keyed(cls, cands, sql_type, side, table):
        """(one function and its aux, or None; {SQL type: function name}) for the readers
        or writers ``cands`` of ``cls`` in one encoding, replacing the one pick
        #build_type_encodings of parser/enrich.py makes per encoding. Each is keyed by
        the SQL type its signatures read or write (``sql_type``); several functions keyed
        so make the map, one makes the class's codec. A function no signature keys stands
        for the class only when it is the encoding's one candidate (``interval_in``). Two
        functions of one encoding for one SQL type rank by ``table``, whose first match
        names the encoding (``cbuffer_out`` before ``cbuffer_as_text`` and
        ``cbuffer_as_ewkt``, ``cbuffer_as_hexwkb`` before ``cbuffer_as_hexewkb``), then by
        how many SQL types each serves, the narrower first,
        as PostgreSQL resolves an overload to its most specific candidate
        (``cbufferset_out`` before ``spatialset_out`` for a ``cbufferset``). Two that tie
        on both contradict each other."""
        def types_of(f):
            """The SQL types ``f``'s signatures serve; #build_type_encodings of
            parser/enrich.py reads none, having no SQL signature yet."""
            return {sql_type(s) for s in f.get("sqlSignatures") or ()} - {None}

        def rank(f):
            """(the index of ``f``'s pattern in ``table``, the encoding table
            #build_type_encodings of parser/enrich.py classifies with; how many SQL types
            ``f`` serves)."""
            return (next(i for i, (rx, _) in enumerate(table) if rx.search(f["name"])),
                    len(types_of(f)))
        by_type, unkeyed = {}, []
        for f, aux in cands:
            types = types_of(f)
            for t in types:
                held = by_type.get(t)
                if held and held[0] is not f:
                    if rank(held[0]) == rank(f):
                        errors.append(f"{cls}: {t} {side} by {held[0]['name']} "
                                      f"and {f['name']}")
                    if rank(held[0]) <= rank(f):
                        continue
                by_type[t] = (f, aux)
            if not types:
                unkeyed.append((f, aux))
        named = {f["name"]: (f, aux) for f, aux in by_type.values()}
        if len(named) > 1:
            return None, {t: (f["name"], aux) for t, (f, aux) in sorted(by_type.items())}
        if named:
            return next(iter(named.values())), {}
        return None, {}

    def resolve(cls, enc, cands, prior, sql_type, side, table):
        """(function and aux, or None; {SQL type: (name, aux)}) of one encoding: what the
        SQL signatures key (#keyed), else the function #build_type_encodings of
        parser/enrich.py states for the encoding, else the encoding's one public
        candidate. An encoding whose readers and writers carry no SQL signature
        (the text and MF-JSON forms of ``GSERIALIZED``, whose geometry and geography are
        PostGIS's types) keeps what enrich states."""
        pick, by_type = keyed(cls, cands, sql_type, side, table) if cands else (None, {})
        if pick or by_type:
            return pick, by_type
        if prior in every:
            aux = _trailing(every[prior])
            return ((every[prior], aux) if aux is not None else None), {}
        return (cands[0] if len(cands) == 1 else None), {}

    every = {f["name"]: f for f in idl.get("functions", [])}
    for cls in sorted(set(encs) | {c for c in decoders if c in encs or _is_identity(c, fns)}):
        e = encs.setdefault(cls, {"encodings": [], "decoders": {}, "encoders": {}})
        prior_dec, prior_enc = dict(e.get("decoders") or {}), dict(e.get("encoders") or {})
        dec, decaux, readers, readaux = {}, {}, {}, {}
        for enc in sorted(set(decoders.get(cls) or {}) | set(prior_dec)):
            pick, by_type = resolve(cls, enc, (decoders.get(cls) or {}).get(enc, []),
                                    prior_dec.get(enc), lambda s: s.get("ret"), "read",
                                    _DECODERS)
            if by_type:
                readers[enc] = {t: name for t, (name, _) in by_type.items()}
                readaux[enc] = {t: aux for t, (_, aux) in by_type.items()}
            elif pick:
                dec[enc], decaux[enc] = pick[0]["name"], pick[1]
        encd, encaux, writers, writeaux = {}, {}, {}, {}
        for enc in sorted(set(encoders.get(cls) or {}) | set(prior_enc)):
            pick, by_type = resolve(cls, enc, (encoders.get(cls) or {}).get(enc, []),
                                    prior_enc.get(enc),
                                    lambda s: (s.get("args") or [None])[0], "written",
                                    _ENCODERS)
            if by_type:
                writers[enc] = {t: name for t, (name, _) in by_type.items()}
                writeaux[enc] = {t: aux for t, (_, aux) in by_type.items()}
                continue
            if not pick:
                continue
            f, aux = pick
            if enc == "wkb":
                v = _variant(cls, fns, macros, errors)
                aux = [dict(a, default=v) if a["name"] == "variant" else a for a in aux]
            encd[enc], encaux[enc] = f["name"], aux
        e["decoders"], e["decoderAux"], e["encoders"], e["encoderAux"] = dec, decaux, encd, encaux
        for key, val in (("readers", readers), ("readerAux", readaux),
                         ("writers", writers), ("writerAux", writeaux)):
            if val:
                e[key] = val
            else:
                e.pop(key, None)
        if "bytes" not in e and _is_identity(cls, fns):
            bytes_ = _byte_codec(cls, fns)
            if bytes_:
                e["bytes"] = bytes_
        e["encodings"] = sorted(set(dec) | set(encd) | set(readers) | set(writers))
        in_e = next((x for x in _ORDER if x in dec), None)
        out_e = next((x for x in _ORDER if x in encd), None)
        e["in"], e["in_aux"] = (dec[in_e], decaux[in_e]) if in_e else (None, [])
        e["out"], e["out_aux"] = (encd[out_e], encaux[out_e]) if out_e else (None, [])
        if "bytes" in e:
            v = _variant(cls, fns, macros, errors)
            e["bytes"]["encoderAux"] = [{"name": "variant", "kind": "integer", "default": v}]

    for struct in idl.get("structs", []):
        te = encs.get(struct["name"])
        if te:
            struct["serialization"] = {"encodings": te["encodings"], "in": te["in"],
                                       "out": te["out"]}
    return idl, errors


def _byte_codec(cls, fns):
    """{decoder, encoder} of a type of its own (``H3Index``): the reader ``T f(const
    uint8_t *, size_t)`` and the writer ``uint8_t *f(T, uint8_t, size_t *)``, the shapes
    #build_type_encodings of parser/enrich.py reads for a structure."""
    dec = [f["name"] for f in fns if f["returnType"].get("typedef") == cls
           and [_base(p["cType"]) for p in f.get("params") or []] == ["uint8_t", "size_t"]]
    enc = [f["name"] for f in fns if (f.get("params") or [{}])[0].get("typedef") == cls
           and _base(f["returnType"].get("c")) == "uint8_t"
           and len(f.get("params") or []) == 3]
    if len(dec) == 1 and len(enc) == 1:
        return {"decoder": dec[0], "encoder": enc[0]}
    return None


def _is_identity(cls, fns):
    """Whether ``cls`` is a type of its own some slot names (``H3Index``)."""
    return any(p.get("typedef") == cls for f in fns
               for p in [f["returnType"]] + (f.get("params") or []))
