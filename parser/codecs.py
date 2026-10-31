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
- Each format is an encoding of its own: ``text``, ``ewkt``, ``wkb``, ``ewkb``, ``hexwkb``,
  ``hexewkb`` and ``mfjson``. A function serves, for the SQL type of each signature, the
  encoding the signature's portable name states (``asText``, ``cbufferFromEWKT``,
  #_serves), so ``cbuffer_in`` reads both the text and the EWKT of a ``cbuffer``; and the
  text writer of a type is the one ``asText`` reaches (#_portable), WKT through
  ``*_as_text`` for a spatial type.
- The WKB writers' size is an out-parameter, stated in the function's ``shape.outParams``
  as for any other function, and the ``variant`` of each is the value the SQL function of
  its encoding passes when the call leaves the byte order out (#_portable_variant): 0, as
  ``wkb_variant_from_endian`` reads an empty order, so ``asBinary`` and ``asHexWKB``
  write plain WKB and ``asEWKB`` and ``asHexEWKB`` the SRID, as in PostgreSQL. The byte
  codec ``bytes``, which carries a value in process, takes the variant the type's own
  ``send`` binds (``WKB_EXTENDED``, #_variant).
- Every decoder and encoder states its trailing inputs by name with the value a binding
  passes, in ``decoderAux`` and ``encoderAux`` keyed by encoding, every reader and writer
  of a SQL type in ``readerAux`` and ``writerAux`` keyed as ``readers`` and ``writers``
  are, and the byte codec its own in ``bytes.encoderAux``: a binding fills each parameter
  by name and refuses one the catalog does not fill. The size a WKB reader takes is the
  length of the bytes it reads (``{"kind": "length", "of": "wkb"}``).
- ``in`` and ``out`` are the reader and the writer of one string encoding wherever the
  class has a single one of each (#wire_pair of parser/enrich.py), so a service reads a
  value in the format it writes it in; in hex WKB ``out_aux`` passes the variant the
  type's ``send`` binds, the SRID included, as PostgreSQL's wire writes a value.
- A value whose slot carries a ``typedef`` (``H3Index``, ``Quadbin``, ``S2CellId``) is a
  class of its own, keyed by that name.
- A class whose values MEOS copies states their memory image in ``image``: the copy
  function and either the function answering an image's length (``size``) or, for a
  fixed-size structure, the bytes it is laid out in (``sizeBytes``), so a binding holding
  values in process reads both from the catalog (#_image).
"""
import re

from parser.enrich import (_BYTE_ENCODINGS, _DECODERS, _ENCODERS, _INT_BASES,
                           _SQL_DECODERS, _SQL_ENCODERS, _STRING_PTR_BASES, _aux_specs,
                           _base, _ptr_depth, wire_pair)


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


def _portable_variant(f, enc, aux, macros):
    """``aux`` of the writer ``f`` of the encoding ``enc``, its ``variant`` the value the
    SQL function of the encoding's portable name passes, as #_variant reads the one a
    ``send`` binds: one the signature binds, else 0 where the byte order is left out
    (``asBinary(intset, endian DEFAULT '')`` and ``asHexWKB`` write plain WKB, as
    ``wkb_variant_from_endian`` reads an empty order 0; ``asEWKB`` and ``asHexEWKB``
    reach writers that add ``WKB_EXTENDED`` themselves). A writer no such signature
    names keeps ``aux``."""
    vs = set()
    for s in f.get("sqlSignatures") or ():
        if _sql_encoding(s.get("sqlName") or f.get("sqlfn"), "written") != enc:
            continue
        bound = (s.get("boundArgs") or {}).get("variant")
        dflts = s.get("argDefaults") or []
        if bound is not None:
            vs.add(_literal(bound, macros))
        elif len(dflts) > 1 and dflts[1] == "''":
            vs.add(0)
    if len(vs) != 1:
        return aux
    v = vs.pop()
    return [dict(a, default=v) if a["name"] == "variant" else a for a in aux]


def _variant(cls, fns, macros, errors):
    """The WKB variant of the byte codec of ``cls``, which carries a value in process:
    the one the type's own ``send`` binds, else the one its SQL hex writer passes when
    the byte order is left out."""
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


def _sql_encoding(name, side):
    """The encoding the SQL name ``name`` states for a reader (``side`` "read") or a
    writer (#_SQL_DECODERS and #_SQL_ENCODERS of parser/enrich.py), or None."""
    if not name:
        return None
    if side == "written":
        return _SQL_ENCODERS.get(name)
    return _encoding(name, _SQL_DECODERS)


def _serves(f, sql_type, side, table):
    """{encoding: {SQL type}}: the encodings ``f`` serves for each SQL type a signature
    reads or writes (``sql_type``). A signature named by a portable I/O function
    (``asText``, ``cbufferFromEWKT``) serves the encoding its name states; any other
    serves the encoding of ``f``'s C name (``table``), as ``cbuffer_in`` serves the text
    of its ``cbuffer_in`` signature and the EWKT of its ``cbufferFromEWKT`` one."""
    out = {}
    for s in f.get("sqlSignatures") or ():
        t = sql_type(s)
        if t is None:
            continue
        enc = (_sql_encoding(s.get("sqlName") or f.get("sqlfn"), side)
               or _encoding(f["name"], table))
        if enc:
            out.setdefault(enc, set()).add(t)
    return out


def _portable(f, t, enc, sql_type, side):
    """Whether a signature of ``f`` over the SQL type ``t`` carries the portable name of
    ``enc``: ``asText(cbuffer)`` is ``cbuffer_as_text``, which outranks ``cbuffer_out``,
    the ``output`` function of the type."""
    return any(sql_type(s) == t and _sql_encoding(s.get("sqlName") or f.get("sqlfn"),
                                                   side) == enc
               for s in f.get("sqlSignatures") or ())


def _byte_reader_aux(f):
    """The aux of a WKB-bytes reader ``T *f(const uint8_t *wkb, size_t size, ...)``: the
    size is the length of the bytes (``kind`` ``length``, ``of`` the bytes), the inputs
    after it as #_aux_specs of parser/enrich.py states them; None for another shape."""
    ps = f.get("params") or []
    if len(ps) < 2 or _base(ps[1].get("cType") or "") != "size_t" or "*" in ps[1]["cType"]:
        return None
    rest = _aux_specs(ps[2:])
    if rest is None:
        return None
    return [{"name": ps[1]["name"], "kind": "length", "of": ps[0]["name"]}] + rest


def _read_type(sig):
    """The SQL type a reader's signature reads: the one it returns."""
    return sig.get("ret")


def _written_type(sig):
    """The SQL type a writer's signature writes: its first argument."""
    return (sig.get("args") or [None])[0]


def _subtype_parents(model):
    """{class: parent} for the classes of the subtype axis of the object model
    (``meta/object-model.json``): ``TInstant``, ``TSequence`` and ``TSequenceSet``, each a
    subtype of the root of the lattice, ``Temporal``, which the axis concretizes."""
    if not model:
        return {}
    roots = [k for k, v in (model.get("lattice") or {}).items()
             if not k.startswith("_") and v.get("kind") == "root"]
    if len(roots) != 1:
        return {}
    return {v["class"]: roots[0]
            for v in ((model.get("axes") or {}).get("subtype") or {}).get("values") or ()
            if v.get("class")}


def state_type_encodings(idl, model=None):
    """(idl, errors): ``idl["typeEncodings"]`` and each struct's ``serialization`` stated
    as this module's docstring describes, and the classes whose codec contradicts
    itself. ``model`` is the object model the subtype classes are read from
    (#_subtype_parents)."""
    fns = [f for f in idl.get("functions", []) if f.get("api") == "public"]
    parents = _subtype_parents(model)
    macros = {m["name"]: m.get("value") for m in idl.get("macros", [])}
    encs = idl.setdefault("typeEncodings", {})
    errors = []

    decoders, encoders, sql_types = {}, {}, {}

    def add(table, cls, f, aux, enc, serves):
        """File ``f`` under each encoding it serves of its own kind, a string or bytes:
        ``enc``, the one of its C name, and those its SQL names state (#_serves)."""
        for e in set(serves) | {enc}:
            if (e in _BYTE_ENCODINGS) == (enc in _BYTE_ENCODINGS):
                table.setdefault(cls, {}).setdefault(e, []).append(
                    (f, aux, serves.get(e, set())))
        sql_types.setdefault(cls, set()).update(*serves.values())

    for f in fns:
        ps = f.get("params") or []
        if not ps:
            continue
        ret, p0 = f["returnType"], ps[0]
        p0c, retc = p0.get("cType") or "", ret.get("c") or ""
        # a reader: a string, or WKB bytes and their length, in; a value of the class out
        enc = _encoding(f["name"], _DECODERS)
        if enc and "*" in p0c and ("*" in retc or ret.get("typedef")):
            if enc in _BYTE_ENCODINGS:
                aux = _byte_reader_aux(f) if _base(p0c) == "uint8_t" else None
            else:
                aux = _trailing(f) if _base(p0c) in _STRING_PTR_BASES else None
            if aux is not None:
                add(decoders, _cls(ret), f, aux, enc,
                    _serves(f, _read_type, "read", _DECODERS))
        # a writer: a value of the class in; a string, or WKB bytes, out
        enc = _encoding(f["name"], _ENCODERS)
        if (enc and "*" in retc and ("*" in p0c or p0.get("typedef"))
                and _base(retc) in ({"uint8_t"} if enc in _BYTE_ENCODINGS
                                    else _STRING_PTR_BASES)):
            aux = _trailing(f)
            if aux is not None:
                add(encoders, _cls(p0), f, aux, enc,
                    _serves(f, _written_type, "written", _ENCODERS))

    def keyed(cls, enc, cands, sql_type, side, table):
        """(one function and its aux, or None; {SQL type: (name, aux)}) for the readers
        or writers ``cands`` of ``cls`` in the encoding ``enc``, replacing the one pick
        #build_type_encodings of parser/enrich.py makes per encoding. Each is keyed by
        the SQL types it serves in ``enc`` (#_serves). One function serving every SQL
        type of the class is the class's codec; otherwise the functions keyed so make
        the map, which leaves out a type the encoding does not serve (``tspatial_as_ewkt``
        writes no ``tint``). A function no signature keys stands for the class only when
        it is the encoding's one candidate (``interval_in``). Two functions of one
        encoding for one SQL type rank first by whether a signature over the type carries
        the portable name of the encoding (#_portable): the function ``asText`` reaches
        writes the text, ``cbuffer_as_text`` (WKT) before ``cbuffer_out`` (hex WKB) for a
        circular buffer, ``tint_out`` for a ``tint``. Then by ``table``, whose first match
        names the encoding (``cbuffer_as_hexwkb`` before ``cbuffer_as_hexewkb``), then by
        how many SQL types each serves, the narrower first, as PostgreSQL resolves an
        overload to its most specific candidate (``cbufferset_out`` before
        ``spatialset_out`` for a ``cbufferset``). Two that tie on all three contradict
        each other."""
        def rank(f, types, t):
            return (not _portable(f, t, enc, sql_type, side),
                    next(i for i, (rx, _) in enumerate(table) if rx.search(f["name"])),
                    len(types))
        by_type, served = {}, {}
        for f, aux, types in cands:
            served[f["name"]] = types
            for t in types:
                held = by_type.get(t)
                if held and held[0] is not f:
                    mine, theirs = rank(f, types, t), rank(held[0],
                                                           served[held[0]["name"]], t)
                    if theirs == mine:
                        errors.append(f"{cls}: {t} {side} by {held[0]['name']} "
                                      f"and {f['name']}")
                    if theirs <= mine:
                        continue
                by_type[t] = (f, aux)
        named = {f["name"]: (f, aux) for f, aux in by_type.values()}
        if len(named) == 1 and set(by_type) >= sql_types.get(cls, set()):
            return next(iter(named.values())), {}
        if named:
            return None, {t: (f["name"], aux) for t, (f, aux) in sorted(by_type.items())}
        return None, {}

    def resolve(cls, enc, cands, prior, sql_type, side, table):
        """(function and aux, or None; {SQL type: (name, aux)}) of one encoding: what the
        SQL signatures key (#keyed), else the function #build_type_encodings of
        parser/enrich.py states for the encoding, else the encoding's one public
        candidate. An encoding whose readers and writers carry no SQL signature
        (the text and MF-JSON forms of ``GSERIALIZED``, whose geometry and geography are
        PostGIS's types) keeps what enrich states."""
        pick, by_type = (keyed(cls, enc, cands, sql_type, side, table) if cands
                         else (None, {}))
        if pick or by_type:
            return pick, by_type
        if prior in every and every[prior].get("api") == "public":
            aux = _trailing(every[prior])
            return ((every[prior], aux) if aux is not None else None), {}
        return ((cands[0][0], cands[0][1]) if len(cands) == 1 else None), {}

    every = {f["name"]: f for f in idl.get("functions", [])}
    structs = {s["name"]: s for s in idl.get("structs", [])}
    for cls in sorted(set(encs) | {c for c in decoders if c in encs or _is_identity(c, fns)}):
        e = encs.setdefault(cls, {"encodings": [], "decoders": {}, "encoders": {}})
        prior_dec, prior_enc = dict(e.get("decoders") or {}), dict(e.get("encoders") or {})
        dec, decaux, readers, readaux = {}, {}, {}, {}
        for enc in sorted(set(decoders.get(cls) or {}) | set(prior_dec)):
            pick, by_type = resolve(cls, enc, (decoders.get(cls) or {}).get(enc, []),
                                    prior_dec.get(enc), _read_type, "read", _DECODERS)
            if by_type:
                readers[enc] = {t: name for t, (name, _) in by_type.items()}
                readaux[enc] = {t: aux for t, (_, aux) in by_type.items()}
            elif pick:
                dec[enc], decaux[enc] = pick[0]["name"], pick[1]
        encd, encaux, writers, writeaux = {}, {}, {}, {}
        for enc in sorted(set(encoders.get(cls) or {}) | set(prior_enc)):
            pick, by_type = resolve(cls, enc, (encoders.get(cls) or {}).get(enc, []),
                                    prior_enc.get(enc), _written_type, "written",
                                    _ENCODERS)
            if by_type:
                writers[enc] = {t: name for t, (name, _) in by_type.items()}
                writeaux[enc] = {t: _portable_variant(every[name], enc, aux, macros)
                                 for t, (name, aux) in by_type.items()}
                continue
            if not pick:
                continue
            f, aux = pick
            encd[enc], encaux[enc] = f["name"], _portable_variant(f, enc, aux, macros)
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
        # A subtype class no public function reads or writes (TInstant, whose readers
        # tintinst_in, ... are internal) reads and writes through its parent's public
        # codecs, keyed by the SQL type as SQL keys them, SQL having no subtype type.
        if not e["encodings"] and cls in parents:
            e["through"] = parents[cls]
        else:
            e.pop("through", None)
        in_e, out_e = wire_pair(dec, encd)
        e["in"], e["in_aux"] = (dec[in_e], decaux[in_e]) if in_e else (None, [])
        e["out"], e["out_aux"] = (encd[out_e], encaux[out_e]) if out_e else (None, [])
        # A service writes a value in hex WKB as PostgreSQL's wire does, through the
        # type's send: the variant #_variant reads (WKB_EXTENDED, the SRID included),
        # where the portable hexwkb writer, asHexWKB, writes plain WKB.
        if out_e == "hexwkb":
            v = _variant(cls, fns, macros, errors)
            e["out_aux"] = [dict(a, default=v) if a["name"] == "variant" else a
                            for a in e["out_aux"]]
        if "bytes" in e:
            v = _variant(cls, fns, macros, errors)
            e["bytes"]["encoderAux"] = [{"name": "variant", "kind": "integer", "default": v}]
        image = _image(cls, fns, structs, errors)
        if image:
            e["image"] = image
        else:
            e.pop("image", None)

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


def _image(cls, fns, structs, errors):
    """{copy, size} or {copy, sizeBytes}: the memory image of a value of ``cls``, which a
    binding holding values in process carries instead of a wire form. ``copy`` is the
    public ``T *f(const T *)`` named ``_copy`` that duplicates a value from its image;
    the image's length is what the public ``int f(const T *)`` named ``_mem_size``
    answers (``size``), or, for a type no such function sizes, the bytes clang lays its
    structure in (``sizeBytes`` of #extract_struct of parser/extractors.py). A structure
    ending in a flexible array member is variable-length, so its laid-out size is not
    its image and the type states none without a ``_mem_size``. Found by shape and name
    as #_byte_codec finds the byte codec."""
    def one(names, what):
        if len(names) > 1:
            errors.append(f"{cls}: {what} by {', '.join(sorted(names))}")
            return None
        return names[0] if names else None

    copy = one([f["name"] for f in fns if f["name"].endswith("_copy")
                and len(f.get("params") or []) == 1
                and _ptr_depth(f["returnType"].get("c") or "") == 1
                and _cls(f["returnType"]) == cls
                and _ptr_depth(f["params"][0].get("cType") or "") == 1
                and _cls(f["params"][0]) == cls], "copied")
    if not copy:
        return None
    size = one([f["name"] for f in fns if f["name"].endswith("_mem_size")
                and len(f.get("params") or []) == 1
                and _ptr_depth(f["returnType"].get("c") or "") == 0
                and _base(f["returnType"].get("canonical") or "") in _INT_BASES
                and _ptr_depth(f["params"][0].get("cType") or "") == 1
                and _cls(f["params"][0]) == cls], "sized")
    if size:
        return {"copy": copy, "size": size}
    struct = structs.get(cls) or {}
    if "sizeBytes" in struct and not any(fld["cType"].endswith("[]")
                                         for fld in struct.get("fields") or ()):
        return {"copy": copy, "sizeBytes": struct["sizeBytes"]}
    return None


def _is_identity(cls, fns):
    """Whether ``cls`` is a type of its own some slot names (``H3Index``)."""
    return any(p.get("typedef") == cls for f in fns
               for p in [f["returnType"]] + (f.get("params") or []))
