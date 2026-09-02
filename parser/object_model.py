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

from parser.typerelations import locate_catalog, temptype_basetypes


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
_COMPANION_PREFIX_ALIASES = {"GeomSet": ["geomset", "geoset"]}


#: The roles whose first parameter is the value the method is called on. A
#: constructor builds one out of something else and an aggregate takes the
#: accumulator, so neither says what the class's instances are.
_RECEIVER_ROLES = frozenset({"accessor", "predicate", "conversion",
                             "restriction", "output"})

_QUALIFIER_RE = re.compile(r"\b(?:const|struct)\b")


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
    out.sort(key=lambda kv: len(kv[0]), reverse=True)
    return out


def _classify(fn_name: str, candidates: list):
    for pref, target in candidates:
        if fn_name == pref or fn_name.startswith(pref + "_"):
            return pref, target
    return None, None


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


# Acronym runs kept upper-case so a binding's case transform can render them
# idiomatically (Python ``as_hex_wkb``, Go ``AsHexWKB``, JS ``asHexWKB``).
_ACRONYMS = {"wkb", "ewkb", "hexwkb", "wkt", "ewkt", "mfjson", "mvt",
             "geojson", "gml", "kml", "srid", "srs"}

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
# binding spellings are not carried forward.
_OONAME_OVERRIDES: dict[str, str] = {}


def _strip_class_token(fn_name: str, cls: str) -> str:
    """Drop the class prefix the function-name object model encodes, leaving
    the bare member name. Tries the class's own lower-cased token first, then
    the generic superclass tokens, longest match wins."""
    tokens = sorted({cls.lower(), *_GENERIC_TOKENS}, key=len, reverse=True)
    for tok in tokens:
        if fn_name == tok:
            return ""
        if fn_name.startswith(tok + "_"):
            return fn_name[len(tok) + 1:]
    return fn_name


def _camel(member: str) -> str:
    """camelCase a snake member name, upper-casing whole acronym runs."""
    parts = [p for p in member.split("_") if p]
    out = []
    for i, p in enumerate(parts):
        if p in _ACRONYMS:
            out.append(p.upper())
        elif i == 0:
            out.append(p)
        else:
            out.append(p[:1].upper() + p[1:])
    return "".join(out)


def _ooname(fn_name: str, cls: str) -> str:
    """Canonical camelCase OO method name for a classified function."""
    if fn_name in _OONAME_OVERRIDES:
        return _OONAME_OVERRIDES[fn_name]
    return _camel(_strip_class_token(fn_name, cls))


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
    m = re.search(r"\n" + re.escape(name) + r"\(MeosType \w+\)\s*", cat_src)
    if not m:
        raise MembershipUnavailable(
            f"meos_catalog.c declares no `{name}` predicate — the lattice names "
            "a membership oracle MEOS does not have")
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
    answer of the subtype it is a product of, and any other class its parent's,
    which is the same C type by construction.
    """
    own = {}
    for cls, spec in classes.items():
        seen = {}
        for method in spec["methods"]:
            if method["role"] not in _RECEIVER_ROLES:
                continue
            fn = functions.get(method["function"])
            params = fn.get("params") if fn else None
            if not params:
                continue
            pointee = _pointee(params[0]["cType"])
            if pointee:
                seen[pointee] = seen.get(pointee, 0) + 1
        ranked = sorted(seen.items(), key=lambda kv: -kv[1])
        if ranked and (len(ranked) == 1 or ranked[0][1] > ranked[1][1]):
            own[cls] = ranked[0][0]

    resolved: dict = {}

    def resolve(cls, walked=frozenset()):
        if cls in resolved:
            return resolved[cls]
        if cls in own:
            resolved[cls] = own[cls]
        elif cls in subtype_classes:
            resolved[cls] = resolve(subtype_classes[cls], walked | {cls})
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
        derive_membership(lattice_nodes, cat_src, basetypes)
        derive_membership(trait_nodes, cat_src, basetypes)
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
                  "ooName": _ooname(name, cls)}
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
