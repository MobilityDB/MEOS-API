"""The implicit MEOS object model, made explicit — codegen source of truth.

`meta/object-model.json` is the curated, authoritative lattice (the class
tree, its prefixes, the closed-algebra companion hierarchies, the error
contract). Folding it into the catalog means every binding/engine derives
the *identical* classes and methods from one mapping instead of
re-curating the implicit C convention by hand.

This is curated canonical data, not a heuristic: classes are preserved
verbatim and only *derived* lookups are added — children/depth/ancestors
of the tree, the assignment of each catalog function to the class it is a
method of (by the MEOS prefix convention, longest-match — equivalence by
construction, the method *is* the C function), and the reverse index. No
class is invented; a function with no prefix match is recorded honestly as
unclassified with a reason, never force-fitted.

The error contract (`raises`) is derived by a static scan of the
MobilityDB sources when available; if they are not, it degrades to an
honest `source-unavailable` signal rather than an empty-set claim — the
same philosophy as portable_parity.py.

Pure dict → dict plus an optional text scan; no libclang.
"""

import json
import os
import re
from pathlib import Path

from parser.typerelations import (MEOSTYPE_PARAM, locate_catalog, temptype_basetypes,
                                  type_names)


def find_mobilitydb_src(headers_dir: Path | None = None) -> Path | None:
    """Resolve the MobilityDB C source root for the error scan / drift gate.

    First existing of: $MOBILITYDB_SRC, the checkout $MDB_SRC_ROOT names, the
    sparse-checkout ``_mobilitydb/meos/src``, or the ``src`` sibling of the
    headers dir.  Returns None when no source tree is available — callers must
    degrade to an honest signal, never fabricate.

    ``MDB_SRC_ROOT`` is the checkout the provisioning hands the parse, and it is
    consulted because the directory name is the provisioner's to choose: the CI
    action checks MobilityDB out as ``_mobilitydb_src`` while the probe below
    names ``_mobilitydb``, so a resolver that knows only the literal name reports
    no source over a tree that is present, and every catalog it derives silently
    loses what the source carries.
    """
    candidates = []
    env = os.environ.get("MOBILITYDB_SRC")
    if env:
        candidates.append(Path(env))
    root = os.environ.get("MDB_SRC_ROOT")
    if root:
        candidates.append(Path(root) / "meos" / "src")
    candidates.append(Path("_mobilitydb") / "meos" / "src")
    if headers_dir is not None:
        candidates.append(Path(headers_dir).parent / "src")
    for c in candidates:
        if c.exists() and (c / "temporal" / "meos_catalog.c").exists():
            return c
    return None


_SUBTYPE_SUFFIX = [("seqset", "SeqSet", "TSequenceSet"),
                   ("seq",    "Seq",    "TSequence"),
                   ("inst",   "Inst",   "TInstant")]

# Extra real prefixes for concrete collection nodes whose C prefix is not the
# lower-cased node name (verified against the headers, not guessed).
_COMPANION_PREFIX_ALIASES: dict[str, list[str]] = {}


#: The roles whose first parameter is the value the method is called on. A
#: constructor builds one out of something else and an aggregate takes the
#: accumulator, so neither says what the class's instances are.
_RECEIVER_ROLES = frozenset({"accessor", "predicate", "conversion",
                             "restriction", "output"})

_QUALIFIER_RE = re.compile(r"\b(?:const|struct)\b")

#: What a class's instances are never a pointer to. A `char *` is a string in
#: every binding and a scalar pointer is a buffer or an out-parameter, so
#: neither says anything about the type a class stands for.
_NOT_A_CLASS_POINTEE = frozenset({
    "char", "void", "bool", "int", "int8", "int8_t", "uint8", "uint8_t",
    "short", "int16", "int16_t", "uint16", "uint16_t", "int32", "int32_t",
    "uint32", "uint32_t", "float", "Oid", "DateADT", "long", "int64",
    "int64_t", "uint64", "uint64_t", "double", "float8", "Datum", "Timestamp",
    "TimestampTz", "TimeADT", "TimeOffset", "size_t",
})


def _pointee(c_type: str) -> str | None:
    """The type a single-pointer C declaration points at, or None."""
    bare = _QUALIFIER_RE.sub("", c_type).strip()
    if bare.endswith("*") and bare.count("*") == 1:
        return bare[:-1].strip()
    return None


def _companion_families(model: dict) -> list:
    """The companion hierarchies the model carries, in file order.

    Read from the model rather than named here, so a hierarchy it gains is
    classified, trees derived and published with no edit in this file.
    """
    return [k for k in model["companions"] if not k.startswith("_")]

_MEOS_ERROR_RE = re.compile(r"\bmeos_error\s*\(\s*[^,]+,\s*([A-Z][A-Z0-9_]+)")
_ENSURE_CALL_RE = re.compile(r"\b(ensure_[a-z0-9_]+)\s*\(")
_FUNC_SIG_RE = re.compile(r"^([A-Za-z_][\w \t\*]*?\b)?([A-Za-z_]\w*)\s*\(")


def _tree(nodes: dict) -> dict:
    """Add children/depth/ancestors to a {name: {parent: ...}} node map."""
    children = {n: [] for n in nodes}
    for n, spec in nodes.items():
        p = spec.get("parent")
        if p:
            children[p].append(n)

    def ancestors(n):
        chain, p = [], nodes[n].get("parent")
        while p:
            chain.append(p)
            p = nodes[p].get("parent")
        return chain

    for n, spec in nodes.items():
        spec["children"] = sorted(children[n])
        anc = ancestors(n)
        spec["ancestors"] = anc
        spec["depth"] = len(anc)
    return nodes


def _candidates(model: dict) -> list:
    """All (prefix, target) pairs, longest prefix first.

    target = {"class", "scope", "axis"}.  Compound <leaf><subtype> prefixes
    map to the concrete leaf×subtype class (a constructor/accessor of it).
    """
    out = []
    lat = {k: v for k, v in model["lattice"].items() if not k.startswith("_")}
    for name, spec in lat.items():
        scope = {"root": "superclass", "abstract": "family",
                 "leaf": "exact"}[spec["kind"]]
        for pref in spec.get("prefixes", []):
            out.append((pref, {"class": name, "scope": scope,
                               "axis": "typeFamily"}))
            if spec["kind"] == "leaf":
                for tok, suf, _sub in _SUBTYPE_SUFFIX:
                    out.append((pref + tok,
                                {"class": name + suf, "scope": "constructor",
                                 "axis": "concrete", "concreteOf": name,
                                 "subtype": _sub}))
    for v in model["axes"]["subtype"]["values"]:
        if v["prefix"]:
            out.append((v["prefix"], {"class": v["class"], "scope": "subtype",
                                      "axis": "subtype"}))
    for fam in _companion_families(model):
        fnodes = {k: x for k, x in model["companions"][fam]["nodes"].items()
                  if not k.startswith("_")}
        for name, spec in fnodes.items():
            prefs = list(spec.get("prefixes", []))
            if spec["kind"] == "leaf":
                prefs += _COMPANION_PREFIX_ALIASES.get(name, [name.lower()])
            for pref in prefs:
                out.append((pref, {"class": name, "scope": "companion",
                                   "axis": fam.lower()}))
    # The bounding box of a leaf's values: a `bbox` relation of the algebra
    # (TNumber -> TBox) makes `<leaf prefix>box` a prefix of the box class for
    # each leaf below its source, so `tintbox_expand` is a TBox member, named
    # with the leaf's value type (#_value_box_suffix).
    for rel in model.get("algebra", {}).get("relations", []):
        if rel.get("relation") != "bbox":
            continue
        for name, spec in lat.items():
            if spec["kind"] != "leaf" or rel["from"] not in _lattice_ancestors(name, lat):
                continue
            for pref in spec.get("prefixes", []):
                out.append((pref + "box", {"class": rel["to"], "scope": "companion",
                                           "axis": "valueBox", "valueOf": name}))
    out.sort(key=lambda kv: len(kv[0]), reverse=True)
    return out


def _lattice_ancestors(name: str, lat: dict) -> list:
    """The parent chain of a lattice class, nearest first, as #_ancestors_of walks
    a class's parents."""
    chain, p = [], lat[name].get("parent")
    while p:
        chain.append(p)
        p = lat[p].get("parent")
    return chain


def _value_box_suffix(leaf: str) -> str:
    """The value type a member of a leaf's box names: the leaf's class without its
    `T` (TInt is Int), so tintbox_expand and tfloatbox_expand are TBox.expandInt
    and TBox.expandFloat, apart in a binding without overloading, as the member
    names #_ooname derives stay apart."""
    return leaf[1:] if leaf.startswith("T") else leaf


def _classify(fn_name: str, candidates: list):
    for pref, target in candidates:
        if fn_name == pref or fn_name.startswith(pref + "_"):
            return pref, target
    return None, None


def _operator_receiver(param: dict, cls: str, ctypes: dict) -> bool:
    """Whether a parameter is a value of the class: a single pointer to the C
    type the class's instances are (see #_class_ctypes)."""
    return bool(ctypes.get(cls)) and _pointee(param.get("cType", "")) == ctypes[cls]


def _classify_operator(fn: dict, candidates: list, ctypes: dict):
    """The class and entry of an operator the prefix rule does not reach.

    MEOS names an operator ``<op>_<left>_<right>`` (``ever_eq_tfloat_float``,
    ``left_stbox_stbox``, ``union_span_span``) or ``<op>_<operand>``
    (``tnot_tbool``), each type token the prefix of a class or a base type, so no
    class prefix starts the name and #_classify leaves it out. The receiver is the
    type of the FIRST parameter and the member is the leading verb, the name cut
    at its first type token (``ever_eq_tfloat_float`` -> ``everEq``), as #_ooname
    camelCases a member: the receiver-typed rule, so a class's operator is the
    value its first parameter is. The type tokens are read with the same
    candidates #_classify reads, and the parameter must really be a value of that
    class, so a conversion such as ``timestamptz_to_stbox``, whose first
    parameter is a timestamp, stays unclassified.

    Where the first operand is no class value (``sub_float_tfloat``, a float
    minus a temporal float) the call is the operation REFLECTED onto the right
    operand, and its member is ``r<op>`` (``rsub``), the spelling the MEOS.js and
    PyMEOS hand layers give it (``radd``, ``rsub``), so it never shares a name with
    ``sub_tfloat_float``, which computes the other difference.

    The entry states which operand the receiver is and the other operand's type
    token, so a binding without overloading can tell the members of one name
    apart by what they take.
    """
    name = fn["name"]
    params = fn.get("params") or []
    parts = name.split("_")
    for i in range(1, len(parts)):
        op, tail = "_".join(parts[:i]), "_".join(parts[i:])
        pref, tgt = _classify(tail, candidates)
        if tgt is None:
            continue
        other = tail[len(pref) + 1:]
        if other and len(params) < 2:
            continue
        if not other and len(params) != 1:
            continue
        if params and _operator_receiver(params[0], tgt["class"], ctypes):
            return tgt["class"], _operator_entry(fn, op, "left", other)
    # Reflected: the class token is the LAST type token and its value the
    # second parameter, the first being no class value.
    if len(params) == 2 and not any(
            _operator_receiver(params[0], c, ctypes) for c in ctypes):
        for j in range(len(parts) - 1, 1, -1):
            tail = "_".join(parts[j:])
            pref, tgt = _classify(tail, candidates)
            if tgt is None or pref != tail:
                continue
            if not _operator_receiver(params[1], tgt["class"], ctypes):
                continue
            op, other = "_".join(parts[:j - 1]), parts[j - 1]
            return tgt["class"], _operator_entry(fn, "r" + op, "right", other)
    return None


def _sql_type_classes(model: dict) -> dict:
    """``{SQL type: class}`` for every class that stands for ONE MEOS type: its
    SQL type is that type's name, the constant without its ``T_`` lower-cased, as
    ``meos_catalog.c`` writes it (``T_INTSET`` is ``intset``)."""
    out = {}
    lat = {k: v for k, v in model["lattice"].items() if not k.startswith("_")}
    for name, spec in lat.items():
        tts = spec.get("temptypes") or []
        if spec.get("kind") == "leaf" and len(tts) == 1:
            out[tts[0][2:].lower()] = name
    for fam in _companion_families(model):
        for name, spec in model["companions"][fam]["nodes"].items():
            if not name.startswith("_") and spec.get("temptype"):
                out[spec["temptype"][2:].lower()] = name
    return out


def _sql_receiver_class(fn: dict, entry: dict, sql_classes: dict,
                        parents: dict) -> str | None:
    """The nearest class common to every type the SQL signatures of an operator
    call it on, or None.

    The receiver-typed rule reads the receiver from the C parameter, and a C
    collection is generic: ``contains_set_int`` and ``contains_set_float`` both take
    a ``Set *`` and a number, so both land on Set and no argument tells them apart.
    The SQL signatures say which set each is for (``intset``, ``floatset``). The
    receiver is the parameter #_classify_operator named; ``sqlArgParams`` puts a
    signature's arguments in its own order (``distance_set_float`` is also
    ``distance(float, floatset)``). Every MEOS type has the same spelling in
    PostgreSQL, so the SQL type names the class once its type modifier is dropped
    (``geometry(Point)`` is ``geometry``).

    This is the template instantiated: Set, Span, SpanSet and Temporal stand for
    the types they are templates of, and the signatures name which of those an
    operator is for. Signatures naming several instantiations answer the nearest
    class common to them: ``contains_set_geo`` over ``geomset`` and ``geogset`` is
    GeoSet, and ``contains_span_span`` over every span type stays on Span."""
    found = _sql_classes_at(fn, 1 if entry.get("receiver") == "right" else 0, sql_classes)
    return _nearest_common(found, parents) if found else None


def _nearest_common(classes: set, parents: dict) -> str | None:
    """The nearest class every one of `classes` is or descends from."""
    chains = [[c, *_ancestors_of(c, parents)] for c in sorted(classes)]
    return next((c for c in chains[0] if all(c in ch for ch in chains[1:])), None)


def _sql_classes_at(fn: dict, at: int, sql_classes: dict) -> set | None:
    """The classes the SQL signatures of a function name for its parameter `at`,
    or None where a signature names none or a type no class stands for: the
    lookup #_sql_receiver_class makes for the receiver."""
    found = _sql_classes_by_signature(fn, at, sql_classes)
    return set(found) if found is not None else None


def _answers_receiver(fn: dict, at: int, sql_classes: dict) -> bool:
    """Whether every SQL signature of a function answers the type of its
    parameter `at`: ``expand(intspan, integer)`` answers an intspan and
    ``expand(floatspan, float)`` a floatspan, so span_expand answers the class
    of its receiver whichever span it is called on, where its C return type
    names the template (``Span *``). The receiver's class per signature is read
    as #_sql_classes_by_signature reads it, and the answer must be that same
    class in every signature."""
    receivers = _sql_classes_by_signature(fn, at, sql_classes)
    if not receivers:
        return False
    for sig, cls in zip(fn.get("sqlSignatures") or [], receivers):
        ret = re.sub(r"\s*\(.*\)$", "", (sig.get("ret") or "").strip())
        if sql_classes.get(ret) != cls:
            return False
    return True


def _sql_return_instantiations(fn: dict, at: int, sql_classes: dict) -> list | None:
    """The class each SQL signature of a member answers for the class it is
    called on, where its C return names a template: ``spanset_span`` returns a
    ``Span *`` in C, and ``span(intspanset)`` answers an intspan,
    ``span(floatspanset)`` a floatspan. One ``{receiverClass, returnClass}`` pair
    per distinct pair, sorted, read as #_answers_receiver reads the receiver
    (#_sql_classes_by_signature) and #_sql_return_class the answer; None where a
    signature answers a type no class stands for."""
    receivers = _sql_classes_by_signature(fn, at, sql_classes)
    if not receivers:
        return None
    pairs = set()
    for sig, cls in zip(fn.get("sqlSignatures") or [], receivers):
        ret = sql_classes.get(re.sub(r"\s*\(.*\)$", "", (sig.get("ret") or "").strip()))
        if ret is None:
            return None
        pairs.add((cls, ret))
    return [{"receiverClass": r, "returnClass": t} for r, t in sorted(pairs)]


def _sql_classes_by_signature(fn: dict, at: int, sql_classes: dict) -> list | None:
    """The class each SQL signature of a function names for its parameter `at`,
    in the order of the signatures, or None as #_sql_classes_at answers it."""
    params = fn.get("params") or []
    sigs = fn.get("sqlSignatures") or []
    if len(params) <= at or not sigs:
        return None
    pname = params[at]["name"]
    found = []
    for sig in sigs:
        args = sig.get("args") or []
        order = sig.get("sqlArgParams") or [p["name"] for p in params][:len(args)]
        if pname not in order or order.index(pname) >= len(args):
            return None
        sqltype = re.sub(r"\s*\(.*\)$", "", args[order.index(pname)].strip())
        cls = sql_classes.get(sqltype)
        if cls is None:
            return None
        found.append(cls)
    return found


def _sql_instantiations(fn: dict, entry: dict, sql_classes: dict) -> list | None:
    """The pairs of classes the SQL signatures of an operator instantiate its two
    operands to, where they name more than one pair.

    #_sql_receiver_class and #_sql_other_class each answer the nearest class
    common to what the signatures name for ONE operand, so the pairing between
    the two is lost: ``contains_tnumber_numspan`` is declared for ``(tint,
    intspan)``, ``(tbigint, bigintspan)`` and ``(tfloat, floatspan)``, and its entry
    states TNumber and Span, which also admits a TFloat with an IntSpan. Each
    pair is a member a binding with overloading can emit on its receiver class,
    calling the same C function (``TFloat.contains(FloatSpan)``), and an
    overload a binding without overloading can tell apart by both arguments."""
    at = 1 if entry.get("receiver") == "right" else 0
    receivers = _sql_classes_by_signature(fn, at, sql_classes)
    others = _sql_classes_by_signature(fn, 1 - at, sql_classes)
    if receivers is None or others is None:
        return None
    pairs = sorted(set(zip(receivers, others)))
    if len(pairs) < 2:
        return None
    return [{"receiverClass": r, "otherClass": o} for r, o in pairs]


def _sql_other_class(fn: dict, entry: dict, sql_classes: dict, parents: dict) -> str | None:
    """The class the other operand of an operator is, instantiated as
    #_sql_receiver_class instantiates the receiver: the nearest class common to
    the types its SQL signatures name there (``tgeometry`` and ``tgeompoint`` are
    TGeo, every number span is Span, ``tstzspan`` is TsTzSpan).

    A C parameter on a template is generic too: ``acontains_geo_tgeo``,
    ``acontains_geo_tcbuffer`` and ``acontains_geo_trgeometry`` all take a
    ``Temporal *``, so a binding typing the argument from the C type cannot tell
    the three apart, while the classes they are for differ."""
    at = 0 if entry.get("receiver") == "right" else 1
    found = _sql_classes_at(fn, at, sql_classes)
    return _nearest_common(found, parents) if found else None


def _token_other_class(fn: dict, entry: dict, candidates: list, ctypes: dict) -> str | None:
    """The class the other operand's type token names, for an operator that has
    no SQL signature to instantiate it from (#_sql_other_class).

    ``teq_th3index_th3index`` is declared in ``meos_h3.h`` and nowhere in SQL, so
    its signatures name nothing and its other operand, a ``const Temporal *``,
    reads as any Temporal, although its name says a TH3Index. The token is read
    as #_classify_operator reads the receiver's: it must be a class prefix
    exactly, and the operand must really be a value of that class."""
    token = entry.get("other")
    if not token or fn.get("sqlSignatures"):
        return None
    pref, tgt = _classify(token, candidates)
    if tgt is None or pref != token:
        return None
    params = fn.get("params") or []
    at = 0 if entry.get("receiver") == "right" else 1
    if len(params) <= at or not _operator_receiver(params[at], tgt["class"], ctypes):
        return None
    return tgt["class"]


def _ancestors_of(cls: str, parents: dict) -> list:
    """The parent chain of a class, nearest first, as #_tree walks it."""
    chain, p = [], parents.get(cls)
    while p:
        chain.append(p)
        p = parents.get(p)
    return chain


def _classify_from_base(fn: dict, candidates: list, ctypes: dict):
    """The class and entry of a constructor MEOS names ``<base>_to_<type>``.

    ``float_to_tbox``, ``float_timestamptz_to_tbox`` and ``timestamptz_to_stbox``
    build a value of the class ``<type>`` is the prefix of out of the values
    ``<base>`` names, so no class prefix starts them and #_classify leaves them
    out. The member reads ``<type>.from<Base>`` (``TBox.fromFloat``,
    ``STBox.fromTimestamptz``), the shape of the readers MEOS names
    ``<type>_from_<format>`` (``tbox_from_wkb`` is ``TBox.fromWKB``), camelCased
    as #_ooname camelCases every member. The type token must be a class prefix
    exactly, read with the candidates #_classify reads, and the function must
    answer a value of that class, so ``h3_cell_to_children``, whose last token is
    no class, stays out."""
    name = fn["name"]
    if "_to_" not in name:
        return None
    head, tail = name.rsplit("_to_", 1)
    pref, tgt = _classify(tail, candidates)
    if tgt is None or pref != tail or not head:
        return None
    cls = tgt["class"]
    if not ctypes.get(cls) or _pointee(fn.get("returnType", {}).get("c", "")) != ctypes[cls]:
        return None
    entry = {"function": name, "role": "constructor", "scope": "fromBase",
             "backing": name, "ooName": _camel("from_" + head)}
    if fn.get("api") != "public":
        entry["ooExclude"] = True
    return cls, entry


def _sql_return_class(fn: dict, sql_classes: dict, parents: dict) -> str | None:
    """The nearest class common to the types the SQL signatures of a function
    answer, as #_sql_receiver_class reads the receiver: ``int_to_set`` answers a
    ``Set *`` in C and an ``intset`` in SQL, so it builds an IntSet."""
    found = set()
    for sig in fn.get("sqlSignatures") or []:
        ret = re.sub(r"\s*\(.*\)$", "", (sig.get("ret") or "").strip())
        cls = sql_classes.get(ret)
        if cls is None:
            return None
        found.add(cls)
    return _nearest_common(found, parents) if found else None


def _operator_entry(fn: dict, op: str, receiver: str, other: str) -> dict:
    """One operator of a class: the method the receiver-typed rule names."""
    entry = {"function": fn["name"], "role": "operator", "scope": "operator",
             "backing": fn["name"], "ooName": _camel(op),
             "receiver": receiver}
    if other:
        entry["other"] = other
    if fn.get("api") != "public":
        entry["ooExclude"] = True
    return entry


def _role(fn_name: str) -> str:
    n = fn_name
    if n.endswith("_make") or "_from_base" in n or "_from_mfjson" in n \
            or n.endswith("_in") or n.endswith("_from_wkb") \
            or n.endswith("_from_hexwkb") or n.endswith("_copy"):
        return "constructor"
    if n.endswith("_out") or "_as_text" in n or "_as_wkb" in n \
            or "_as_hexwkb" in n or "_as_mfjson" in n or "_as_ewkt" in n:
        return "output"
    if "_to_" in n or n.endswith("_to_tbox") or n.endswith("_to_stbox"):
        return "conversion"
    if "_at_" in n or "_minus_" in n or n.endswith("_at_value") \
            or n.endswith("_minus_value"):
        return "restriction"
    for agg in ("_tagg", "_extent_transfn", "_transfn", "_finalfn",
                "_combinefn", "_tcount"):
        if agg in n:
            return "aggregate"
    if any(n.endswith(c) for c in ("_eq", "_ne", "_lt", "_le", "_gt", "_ge",
                                   "_cmp", "_overlaps", "_contains",
                                   "_intersects", "_eq_temporal")):
        return "predicate"
    return "accessor"


# Each acronym token spelled as the SQL names of MobilityDB spell it
# (``asHexWKB``, ``asHexEWKB``, ``asEWKB``, ``asEWKT``, ``asMFJSON``,
# ``asGeoJSON``, ``asMVTGeom``, ``setSRID``), so a binding's case transform
# renders it as the portable name does (Go ``AsHexWKB``, JS ``asHexWKB``); the
# tokens no SQL name carries are upper-case, as PostGIS's ``ST_AsGML``.
_ACRONYMS = {"wkb": "WKB", "ewkb": "EWKB", "hexwkb": "HexWKB",
             "hexewkb": "HexEWKB", "wkt": "WKT", "ewkt": "EWKT",
             "mfjson": "MFJSON", "mvt": "MVT", "mvtgeom": "MVTGeom",
             "geojson": "GeoJSON",
             "gml": "GML", "kml": "KML", "srid": "SRID", "srs": "SRS"}

# Generic superclass / companion tokens a method may carry when its scope is
# not the leaf class itself (``temporal_*`` on a concrete type, ``set_*`` on a
# collection). Tried after the class's own token, longest first.
_GENERIC_TOKENS = ("temporal", "tnumber", "tspatial", "tgeo", "tpoint",
                   "spanset", "span", "set", "box")

# Internal machinery that is classified to a class but is not a user-facing
# method: SQL aggregate transition/final/combine functions.
_OONAME_EXCLUDE_SUFFIXES = ("_transfn", "_finalfn", "_combinefn")

# Editorial name fixes for the rare cases mechanical derivation gets wrong.
# Deliberately minimal — the clean derived name is canonical, so legacy
# binding spellings are not carried forward. What belongs here is a function the
# derivation cannot name at all: one whose whole name IS its class's prefix, so
# dropping the prefix leaves nothing.
_OONAME_OVERRIDES: dict[str, str] = {
    # `meos_pc_schema(pcid)` answers the schema the cache holds for a pcid.
    "meos_pc_schema": "get",
}


def _strip_class_token(fn_name: str, cls: str, prefix: str = "") -> str:
    """Drop the class prefix the function-name object model encodes, leaving
    the bare member name. Tries the prefix the classifier matched and the
    class's own lower-cased token, then the generic superclass tokens, longest
    match wins.

    The matched prefix is what a class whose C prefix is not its lower-cased
    name is reached by — `geom_*` for Geometry, `geog_*` for Geography — and
    without it those members keep the prefix twice over, as `geomBuffer` on a
    Geometry."""
    tokens = sorted({cls.lower(), prefix, *_GENERIC_TOKENS} - {""},
                    key=len, reverse=True)
    for tok in tokens:
        if fn_name == tok:
            return ""
        if fn_name.startswith(tok + "_"):
            return fn_name[len(tok) + 1:]
    return fn_name


def _camel(member: str) -> str:
    """camelCase a snake member name, each acronym token spelled as `_ACRONYMS` states."""
    parts = [p for p in member.split("_") if p]
    out = []
    for i, p in enumerate(parts):
        if p in _ACRONYMS:
            out.append(_ACRONYMS[p])
        elif i == 0:
            out.append(p)
        else:
            out.append(p[:1].upper() + p[1:])
    return "".join(out)


def _ooname(fn_name: str, cls: str, prefix: str = "") -> str:
    """Canonical camelCase OO method name for a classified function."""
    if fn_name in _OONAME_OVERRIDES:
        return _OONAME_OVERRIDES[fn_name]
    return _camel(_strip_class_token(fn_name, cls, prefix))


def _oo_excluded(fn: dict, role: str) -> bool:
    """True for functions classified to a class that are internal machinery,
    not user OO methods: functions the catalog marks internal (the ``_p`` peeks,
    the bbox / skiplist plumbing, ``*_in`` / ``*_out``), aggregate transition
    helpers, and comparators with no SQL function (qsort / bound / min / max sort
    helpers).

    The internal-API check is the load-bearing one for a binding that generates
    its object layer from ``classes[*].methods``: without it that list mixes the
    public surface with functions a binding cannot call, so every binding would
    re-derive the split. The catalog carries ``api`` per function, so the method
    carries the exclusion and a binding keeps only ``not m['ooExclude']``."""
    name = fn["name"]
    if fn.get("api") != "public":
        return True
    if any(name.endswith(s) for s in _OONAME_EXCLUDE_SUFFIXES):
        return True
    if role == "predicate" and not fn.get("sqlfn"):
        return True
    return False


def _scan_errors(src_root: Path, public: set) -> dict:
    """Static scan: function → set of errorCode it can raise.

    Best-effort, brace-depth based. Builds an ``ensure_* → codes`` map and
    resolves one indirection level (MEOS guards args through ensure_*
    helpers that themselves call meos_error). Every entry is tagged
    via="direct"|"ensure"; nothing is asserted that is not textually
    present in the source.
    """
    raw: dict[str, dict[str, set]] = {}      # fn -> {direct:set, ens:set}
    for cf in sorted(src_root.glob("**/*.c")):
        try:
            lines = cf.read_text(errors="ignore").splitlines()
        except OSError:
            continue
        depth = 0
        cur = None
        prev = ""
        for ln in lines:
            if depth == 0 and "{" in ln:
                m = _FUNC_SIG_RE.match(ln) or _FUNC_SIG_RE.match(prev + ln)
                if m:
                    cur = m.group(2)
                    raw.setdefault(cur, {"direct": set(), "ens": set()})
            if cur:
                for c in _MEOS_ERROR_RE.findall(ln):
                    raw[cur]["direct"].add(c)
                for e in _ENSURE_CALL_RE.findall(ln):
                    raw[cur]["ens"].add(e)
            depth += ln.count("{") - ln.count("}")
            if depth <= 0:
                depth = 0
                cur = None
            prev = ln if not ln.strip().endswith((";", "}", "{")) else ""

    ensure_codes = {f: v["direct"] for f, v in raw.items()
                    if f.startswith("ensure_")}
    result = {}
    for fn in sorted(public):
        rec = raw.get(fn)
        if not rec:
            continue
        codes = []
        for c in sorted(rec["direct"]):
            codes.append({"code": c, "via": "direct"})
        seen = {c["code"] for c in codes}
        for e in sorted(rec["ens"]):
            for c in sorted(ensure_codes.get(e, ())):
                if c not in seen:
                    codes.append({"code": c, "via": "ensure", "through": e})
                    seen.add(c)
        if codes:
            result[fn] = codes
    return result


class MembershipUnavailable(RuntimeError):
    """The source states a membership the lattice cannot use.

    Raised where ``meos_catalog.c`` IS readable and disagrees with the model —
    a predicate the model names that MEOS does not declare, a predicate
    admitting nothing, a leaf modelling a type the relation catalog does not
    relate. Each is a real disagreement, never a missing file: an unreachable
    source is reported as ``membership.status`` instead, the way the error
    contract reports one.
    """


_PREDICATE_TEMPTYPE_RE = re.compile(r"\bT_T[A-Z0-9_]+\b")

#: The tdoubleN types exist for temporal aggregation and are not part of the
#: published model, so a predicate admitting them contributes the rest. Their
#: base types are internal for the same reason and `meos_basetype` says so in
#: its own comment.
_INTERNAL_TEMPTYPES = frozenset({"T_TDOUBLE2", "T_TDOUBLE3", "T_TDOUBLE4"})
_INTERNAL_BASETYPES = frozenset({"T_DOUBLE2", "T_DOUBLE3", "T_DOUBLE4"})


def _predicate_body(cat_src: str, name: str) -> str:
    """The body of the ``name(MeosType ...)`` membership predicate."""
    # The parameter may carry an attribute macro, as `tcellindex_type(MeosType
    # type UNUSED)` does when no cell family is compiled in.
    m = re.search(r"\n" + re.escape(name) + r"\(" + MEOSTYPE_PARAM
                  + r" \w+(?:\s+[A-Z_]+)?\)\s*", cat_src)
    if not m:
        raise MembershipUnavailable(
            f"meos/src/temporal declares no `{name}` predicate — the lattice "
            "names a membership oracle MEOS does not have")
    i = cat_src.index("{", m.end())
    depth, j = 0, i
    while j < len(cat_src):
        depth += (cat_src[j] == "{") - (cat_src[j] == "}")
        if depth == 0:
            return cat_src[i:j + 1]
        j += 1
    return cat_src[i:]


_PREDICATE_TYPE_RE = re.compile(r"\bT_[A-Z0-9_]+\b")


def predicate_types(cat_src: str, name: str) -> list:
    """Every MeosType a membership predicate admits, in MeosType order."""
    seen, out = set(), []
    for t in _PREDICATE_TYPE_RE.findall(_predicate_body(cat_src, name)):
        if t not in _INTERNAL_TEMPTYPES and t not in seen:
            seen.add(t)
            out.append(t)
    if not out:
        raise MembershipUnavailable(f"`{name}` admits no type")
    return out


def byreference_basetypes(cat_src: str) -> list:
    """The base types whose values cross the MEOS boundary as a pointer.

    `basetype_byvalue` names the ones a Datum carries whole; every other base
    type is reached through a pointer, so a method taking or answering one
    needs a class for it. Both predicates are MEOS's to state, which is why
    this reads them instead of listing the answer.
    """
    byvalue = set(predicate_types(cat_src, "basetype_byvalue"))
    return [t for t in predicate_types(cat_src, "meos_basetype")
            if t not in byvalue and t not in _INTERNAL_BASETYPES]


def predicate_temptypes(cat_src: str, name: str) -> list:
    """The temporal types a membership predicate admits, in MeosType order."""
    seen, out = set(), []
    for t in _PREDICATE_TEMPTYPE_RE.findall(_predicate_body(cat_src, name)):
        if t not in _INTERNAL_TEMPTYPES and t not in seen:
            seen.add(t)
            out.append(t)
    if not out:
        raise MembershipUnavailable(f"`{name}` admits no temporal type")
    return out


def release_nodes(nodes: dict, cat_src: str) -> dict:
    """The nodes of the model the release line of ``cat_src`` has.

    The model names every type MEOS has on master; a release line older than a type
    (stable-1.3 has no tjsonb or tbigint) does not list it in ``MEOS_TYPE_NAMES``, so the
    leaf modelling it is not a class of that line. A leaf whose type the line lists but
    does not relate is a disagreement, which #derive_membership raises; a catalog whose
    type names do not parse keeps every node, so that the derivation reports it.
    """
    named = type_names(cat_src)
    if not named:
        return nodes
    return {k: v for k, v in nodes.items()
            if v.get("kind") != "leaf" or all(t in named for t in v.get("temptypes") or ())}


def membership_source(catalog: Path) -> str:
    """The text a membership predicate is read from: ``meos_catalog.c`` first,
    then every other source beside it in ``meos/src/temporal``, the directory
    #locate_temporal_source of parser/typerelations.py resolves.  MEOS states a
    category of types where its family lives, as ``tcellindex_type`` of
    ``tcellindex.c`` states the temporal cell indexes, and #predicate_types
    reads the first definition of the name it is given, so the catalog's own
    predicates keep precedence."""
    parts = [catalog.read_text(errors="ignore")]
    for path in sorted(catalog.parent.glob("*.c")):
        if path != catalog:
            parts.append(path.read_text(errors="ignore"))
    return "\n".join(parts)


def derive_membership(nodes: dict, cat_src: str, basetypes: dict) -> None:
    """Fill each node's membership from the catalog, in place.

    A node naming a `predicate` takes the types that predicate admits; a leaf
    takes the base type ``MEOS_RELTYPE_CATALOG`` gives the one type it models.
    Neither is stated in the model file: both are MEOS's to say, and a copy of
    either is a second source that goes stale the next time MEOS gains a type.
    """
    for name, spec in nodes.items():
        pred = spec.get("predicate")
        if pred:
            spec["temptypes"] = predicate_temptypes(cat_src, pred)
        temptypes = spec.get("temptypes")
        if spec.get("kind") == "leaf" and temptypes:
            temptype = temptypes[0]
            if temptype not in basetypes:
                raise MembershipUnavailable(
                    f"{name} models {temptype}, which the relation catalog "
                    "gives no base type")
            spec["cBaseType"] = basetypes[temptype]


def _class_ctypes(classes: dict, functions: dict, parents: dict,
                  subtype_classes: dict) -> dict:
    """The C type each class's instances are a pointer to.

    A binding declares every wrapper in terms of it, so leaving it to each
    binding to work out is what makes a class a binding cannot type unless it
    is edited. It is read here from the signatures MEOS already publishes: a
    receiver-role method takes the value it is called on first, so the pointee
    of that parameter names the type, and the class's own methods answer for
    it. A class whose methods build values rather than take them — the
    concrete `<leaf><subtype>` classes hold constructors alone — takes the
    answer of the subtype it is a product of, then what its own methods RETURN,
    and any other class its parent's, which is the same C type by construction.
    """
    own, made = {}, {}
    for cls, spec in classes.items():
        seen, answered = {}, {}
        for method in spec["methods"]:
            fn = functions.get(method["function"])
            if not fn:
                continue
            pointee = _pointee(fn.get("returnType", {}).get("c", ""))
            if pointee and pointee not in _NOT_A_CLASS_POINTEE:
                answered[pointee] = answered.get(pointee, 0) + 1
            if method["role"] not in _RECEIVER_ROLES:
                continue
            params = fn.get("params")
            if not params:
                continue
            pointee = _pointee(params[0]["cType"])
            if pointee:
                seen[pointee] = seen.get(pointee, 0) + 1
        for source, out in ((seen, own), (answered, made)):
            ranked = sorted(source.items(), key=lambda kv: -kv[1])
            if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
                out[cls] = ranked[0][0]

    resolved: dict = {}

    def resolve(cls, walked=frozenset()):
        if cls in resolved:
            return resolved[cls]
        if cls in subtype_classes:
            # A concrete class is a value of its subtype whatever its own
            # methods take first: trgeometryseqset_make_gaps takes the
            # reference geometry, and its class is a TSequenceSet all the same.
            resolved[cls] = resolve(subtype_classes[cls], walked | {cls})
        elif cls in own:
            resolved[cls] = own[cls]
        elif cls in made:
            # No method takes one, so what the class MAKES says what it is —
            # a type MEOS only ever hands over, never calls a method on, is
            # reached that way and no other.
            resolved[cls] = made[cls]
        else:
            parent = parents.get(cls)
            resolved[cls] = (resolve(parent, walked | {cls})
                             if parent and parent not in walked | {cls}
                             else None)
        return resolved[cls]

    return {cls: resolve(cls) for cls in classes}


def attach_object_model(idl: dict, path: Path,
                        mobilitydb_src: Path | None = None) -> dict:
    """Attach ``idl["objectModel"]`` from the canonical lattice file."""
    if not Path(path).exists():
        return idl
    model = json.loads(Path(path).read_text())

    # The lattice's type membership is MEOS's to state, so it is read from
    # meos_catalog.c at each parse rather than carried in the model file. A
    # class the model names gains the types its predicate admits, and a leaf the
    # base type the relation catalog gives it, so a type MEOS adds reaches the
    # published model with no edit here.
    catalog = locate_catalog(mobilitydb_src)
    lattice_nodes = {k: v for k, v in model["lattice"].items()
                     if not k.startswith("_")}
    trait_nodes = {k: v for k, v in model["traits"].items()
                   if not k.startswith("_")}
    if catalog is not None:
        cat_src = catalog.read_text(errors="ignore")
        basetypes = temptype_basetypes(cat_src)
        lattice_nodes = release_nodes(lattice_nodes, cat_src)
        pred_src = membership_source(catalog)
        derive_membership(lattice_nodes, pred_src, basetypes)
        derive_membership(trait_nodes, pred_src, basetypes)
        membership = {"status": "derived", "source": str(catalog)}
    else:
        # Say so rather than publish an empty membership: a class naming no
        # type would be indistinguishable from one MEOS has no type for. The
        # error contract answers an unreachable source the same way.
        membership = {"status": "source-unavailable", "source": None}

    lat = _tree(lattice_nodes)
    for fam in _companion_families(model):
        _tree({k: v for k, v in model["companions"][fam]["nodes"].items()
               if not k.startswith("_")})

    candidates = _candidates(model)
    functions = idl.get("functions", [])
    public = {f["name"] for f in functions}

    classes: dict[str, dict] = {}
    function_to_class: dict[str, dict] = {}
    unclassified: list[str] = []

    for fn in functions:
        name = fn["name"]
        pref, tgt = _classify(name, candidates)
        if tgt is None:
            function_to_class[name] = {
                "class": None,
                "reason": "no-prefix-match (operator/base-helper/plumbing)"}
            unclassified.append(name)
            continue
        cls = tgt["class"]
        rec = classes.setdefault(cls, {"methods": []})
        role = _role(name)
        method = {"function": name, "role": role,
                  "scope": tgt["scope"], "backing": name,
                  "ooName": _ooname(name, cls, pref)
                            + (_value_box_suffix(tgt["valueOf"]) if tgt.get("valueOf") else "")}
        if _oo_excluded(fn, role):
            method["ooExclude"] = True
        sugar = []
        if role == "predicate" and not method.get("ooExclude"):
            sugar.append("operator")
        if "_to_" in name:
            sugar.append("cast")
        if name.endswith("_in") or name.endswith("_out"):
            sugar.append("io")
        if sugar:
            method["ooSugar"] = sugar
        rec["methods"].append(method)
        function_to_class[name] = {
            "class": cls, "scope": tgt["scope"], "axis": tgt["axis"],
            "matchedPrefix": pref, "via": "prefix", "backing": name}
        if "concreteOf" in tgt:
            function_to_class[name]["concreteOf"] = tgt["concreteOf"]
            function_to_class[name]["subtype"] = tgt["subtype"]

    # What each class's instances are a pointer to, so a binding declares its
    # wrappers from the model rather than from a map of its own.
    parents = {n: s.get("parent") for n, s in lat.items()}
    for fam in _companion_families(model):
        for n, s in model["companions"][fam]["nodes"].items():
            if not n.startswith("_"):
                parents[n] = s.get("parent")
    subtype_classes = {}
    for leaf in [n for n, s in lat.items() if s["kind"] == "leaf"]:
        for _tok, suffix, subtype in _SUBTYPE_SUFFIX:
            if leaf + suffix in classes:
                subtype_classes[leaf + suffix] = subtype
    ctypes = _class_ctypes(classes, {f["name"]: f for f in functions},
                           parents, subtype_classes)
    for cls, ctype in ctypes.items():
        if ctype:
            classes[cls]["cType"] = ctype

    by_name = {f["name"]: f for f in functions}
    sql_classes = _sql_type_classes(model)

    # The constructors MEOS names from what they are built of (`float_to_tbox`),
    # before the operators: a name `<base>_to_<type>` answering a value of the
    # class `<type>` names is that class's constructor, whatever token it starts
    # with, so `numspan_tstzspan_to_tbox` is TBox.fromNumspanTstzspan and never
    # an operator of TsTzSpan, whose prefix only its second operand carries.
    still = []
    for name in unclassified:
        made = _classify_from_base(by_name[name], candidates, ctypes)
        if made is None:
            still.append(name)
            continue
        cls, entry = made
        refined = _sql_return_class(by_name[name], sql_classes, parents)
        if refined and refined != cls and cls in _ancestors_of(refined, parents):
            entry["refinedFrom"] = cls
            cls = refined
        spec = classes.setdefault(cls, {"methods": []})
        if any(m["ooName"] == entry["ooName"] for m in spec["methods"]):
            still.append(name)
            continue
        spec["methods"].append(entry)
        function_to_class[name] = {
            "class": cls, "scope": "fromBase", "axis": "fromBase",
            "via": "fromBase", "backing": name}
    unclassified = still

    # The operators no prefix reaches, now that each class states its C type.
    still = []
    for name in unclassified:
        op = _classify_operator(by_name[name], candidates, ctypes)
        if op is None:
            still.append(name)
            continue
        cls, entry = op
        refined = _sql_receiver_class(by_name[name], entry, sql_classes, parents)
        if refined and refined != cls and cls in _ancestors_of(refined, parents):
            entry["refinedFrom"] = cls
            cls = refined
        other = (_sql_other_class(by_name[name], entry, sql_classes, parents)
                 or _token_other_class(by_name[name], entry, candidates, ctypes))
        if other:
            entry["otherClass"] = other
        pairs = _sql_instantiations(by_name[name], entry, sql_classes)
        if pairs:
            entry["instantiations"] = pairs
        classes.setdefault(cls, {"methods": []}).setdefault("operators", []).append(entry)
        function_to_class[name] = {
            "class": cls, "scope": "operator", "axis": "operator",
            "via": "operator", "backing": name}
    unclassified = still

    # What a member called on a value answers, where its C return type names
    # the template and every SQL signature answers the receiver's own type.
    for cls, spec in classes.items():
        for entry in spec["methods"] + spec.get("operators", []):
            fn = by_name.get(entry["function"])
            if fn is None:
                continue
            at = 1 if entry.get("receiver") == "right" else 0
            params = fn.get("params") or []
            if len(params) <= at or not _operator_receiver(params[at], cls, ctypes):
                continue
            if _answers_receiver(fn, at, sql_classes):
                entry["answersReceiver"] = True
                continue
            pairs = _sql_return_instantiations(fn, at, sql_classes)
            if pairs:
                entry["returnInstantiations"] = pairs

    # Error contract
    errors = dict(model["errors"])
    if mobilitydb_src and Path(mobilitydb_src).exists():
        raises = _scan_errors(Path(mobilitydb_src), public)
        errors["status"] = "scanned"
        errors["raises"] = raises
        errors["raisesCount"] = len(raises)
    else:
        errors["status"] = "source-unavailable"
        errors["raises"] = {}
        errors["raisesCount"] = 0

    leaves = sorted(n for n, s in lat.items() if s["kind"] == "leaf")
    abstracts = sorted(n for n, s in lat.items()
                       if s["kind"] in ("root", "abstract"))
    companion_nodes = {n for fam in _companion_families(model)
                       for n in model["companions"][fam]["nodes"]}
    concretes = sorted(c for c in classes
                       if c not in lat and c not in companion_nodes)

    idl["objectModel"] = {
        "provenance": model["provenance"],
        "axes": model["axes"],
        "membership": membership,
        "lattice": lat,
        "traits": model["traits"],
        "companions": model["companions"],
        "algebra": model["algebra"],
        "errors": errors,
        "scope": model["scope"],
        "notes": model["notes"],
        "corrections": model["corrections"],
        "dispatch": model.get("dispatch", {}),
        "classes": classes,
        "functionToClass": function_to_class,
        "summary": {
            "latticeNodes": len(lat),
            "abstractClasses": abstracts,
            "leafClasses": leaves,
            "concreteClasses": concretes,
            "classesWithMethods": len(classes),
            "functionsClassified": len(functions) - len(unclassified),
            "functionsTotal": len(functions),
            "unclassified": len(unclassified),
            "unclassifiedNames": sorted(unclassified),
            "coveragePct": (round((len(functions) - len(unclassified))
                                  * 100 / len(functions), 1)
                            if functions else 0.0),
            "errorStatus": errors["status"],
            "ooMethods": sum(len(c["methods"]) for c in classes.values()),
            "ooExcluded": sum(1 for c in classes.values()
                              for m in c["methods"] if m.get("ooExclude")),
        },
    }
    return idl
