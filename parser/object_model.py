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


def find_mobilitydb_src(headers_dir: Path | None = None) -> Path | None:
    """Resolve the MobilityDB C source root for the error scan / drift gate.

    First existing of: $MOBILITYDB_SRC, the sparse-checkout
    ``_mobilitydb/meos/src``, or the ``src`` sibling of the headers dir.
    Returns None when no source tree is available — callers must degrade to
    an honest signal, never fabricate.
    """
    candidates = []
    env = os.environ.get("MOBILITYDB_SRC")
    if env:
        candidates.append(Path(env))
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
    for fam in ("Box", "Collection"):
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
    for fn in public:
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


def attach_object_model(idl: dict, path: Path,
                        mobilitydb_src: Path | None = None) -> dict:
    """Attach ``idl["objectModel"]`` from the canonical lattice file."""
    if not Path(path).exists():
        return idl
    model = json.loads(Path(path).read_text())

    lat = _tree({k: v for k, v in model["lattice"].items()
                 if not k.startswith("_")})
    for fam in ("Box", "Collection"):
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
        method = {"function": name, "role": _role(name),
                  "scope": tgt["scope"], "backing": name}
        rec["methods"].append(method)
        function_to_class[name] = {
            "class": cls, "scope": tgt["scope"], "axis": tgt["axis"],
            "matchedPrefix": pref, "via": "prefix", "backing": name}
        if "concreteOf" in tgt:
            function_to_class[name]["concreteOf"] = tgt["concreteOf"]
            function_to_class[name]["subtype"] = tgt["subtype"]

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
    concretes = sorted(c for c in classes
                       if c not in lat
                       and c not in model["companions"]["Box"]["nodes"]
                       and c not in model["companions"]["Collection"]["nodes"])

    idl["objectModel"] = {
        "provenance": model["provenance"],
        "axes": model["axes"],
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
        },
    }
    return idl
