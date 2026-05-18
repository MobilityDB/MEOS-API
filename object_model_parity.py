# Object-model parity audit — the meos-api.json analogue of
# portable_parity.py, for the class lattice.
#
#     python run.py                  # catalog with `objectModel`
#     python object_model_parity.py  # -> output/meos-object-model-parity.json
#
# It cross-references the DERIVED lattice against the most mature hand-built
# OO model (PyMEOS, the oracle — parsed from pymeos/factory.py, never
# hard-coded) and surfaces every structural divergence as a worklist entry.
# A divergence already explained by a curated `corrections` item is marked
# `known`; a new one is `needs-correction`. Nothing is silently dropped and
# no verdict is fabricated: if the oracle is absent the audit degrades to an
# honest `oracle-unavailable` status (same philosophy as portable_parity.py).

import json
import re
import sys
from pathlib import Path

IN_PATH = (Path(sys.argv[1]) if len(sys.argv) > 1
           else Path("output/meos-idl.json"))
OUT_PATH = (Path(sys.argv[2]) if len(sys.argv) > 2
            else Path("output/meos-object-model-parity.json"))
# PyMEOS oracle: factory.py. Default = sibling checkout; overridable.
PYMEOS = (Path(sys.argv[3]) if len(sys.argv) > 3
          else Path(__file__).resolve().parent.parent
          / "PyMEOS" / "pymeos" / "factory.py")

_SUBTYPE = {"INSTANT": "TINSTANT", "SEQUENCE": "TSEQUENCE",
            "SEQUENCE_SET": "TSEQUENCESET"}
_TEMPORAL_RE = re.compile(
    r"\(\s*MeosType\.(\w+)\s*,\s*MeosTemporalSubtype\.(\w+)\s*\)\s*:\s*(\w+)")
_COLL_RE = re.compile(r"MeosType\.(\w+)\s*:\s*(\w+)")


def _parse_oracle(path: Path):
    """Extract PyMEOS's factory matrix: temporal {(temptype,subtype):cls},
    collection {temptype:cls}.  Returns None if unavailable."""
    if not Path(path).exists():
        return None
    txt = Path(path).read_text()
    # Split the two factories so collection regex doesn't catch temporal lines.
    coll_start = txt.find("_CollectionFactory")
    temporal_txt = txt[:coll_start] if coll_start > 0 else txt
    coll_txt = txt[coll_start:] if coll_start > 0 else ""
    temporal = {(t, _SUBTYPE.get(s, s)): c
                for t, s, c in _TEMPORAL_RE.findall(temporal_txt)}
    collection = {t: c for t, c in _COLL_RE.findall(coll_txt)}
    return {"temporal": temporal, "collection": collection}


def build_parity(catalog: dict, oracle) -> dict:
    om = catalog.get("objectModel")
    if not om:
        raise ValueError("catalog has no `objectModel` — run run.py")

    lat = {k: v for k, v in om["lattice"].items() if not k.startswith("_")}
    leaves = {n: s for n, s in lat.items() if s["kind"] == "leaf"}
    coll_nodes = {k: v for k, v in om["companions"]["Collection"]["nodes"]
                  .items() if not k.startswith("_") and v["kind"] == "leaf"}
    known = {c["id"]: c for c in om["corrections"]["items"]}

    work = []

    def add(kind, detail, status, ref=None):
        work.append({"kind": kind, "detail": detail, "status": status,
                     **({"correction": ref} if ref else {})})

    # Always carry the curated corrections (the user's standing requirement:
    # every irregularity surfaced as durable, reviewable data).
    for c in om["corrections"]["items"]:
        add(f"curated:{c['side']}:{c['severity']}",
            f"{c['id']} {c['location']} — {c['observed']}",
            "known", c["id"])

    if oracle is None:
        return {
            "status": "oracle-unavailable",
            "oraclePath": str(PYMEOS),
            "note": "PyMEOS factory.py not found; reporting curated "
                    "corrections only — no fabricated parity verdict.",
            "total": len(work), "aligned": 0,
            "divergences": len(work),
            "worklist": work,
        }

    # MEOS-derived concrete matrix (every leaf × the 3 subtypes) vs PyMEOS.
    o_temporal = oracle["temporal"]
    o_classes = set(o_temporal.values())
    for leaf, spec in sorted(leaves.items()):
        tt = spec["temptypes"][0]
        for tok, suf, sub in (("TINSTANT", "Inst", "TINSTANT"),
                              ("TSEQUENCE", "Seq", "TSEQUENCE"),
                              ("TSEQUENCESET", "SeqSet", "TSEQUENCESET")):
            meos_cls = leaf + suf
            if (tt, tok) not in o_temporal:
                cid = next((i for i, c in known.items()
                            if c["side"] == "pymeos"
                            and "missing-class" in c["severity"]
                            and (leaf in c["observed"]
                                 or "full leaf" in c["suggested"])), None)
                add("concrete-missing-in-pymeos",
                    f"{meos_cls} ({tt},{tok}) defined by MEOS, absent from "
                    f"PyMEOS _TemporalFactory",
                    "known" if cid else "needs-correction", cid)
    # PyMEOS classes with no MEOS leaf (should be none — superset check).
    meos_concrete = {lf + s for lf in leaves
                     for s in ("Inst", "Seq", "SeqSet")}
    for oc in sorted(o_classes - meos_concrete):
        add("concrete-missing-in-meos",
            f"PyMEOS defines {oc} with no corresponding MEOS leaf×subtype",
            "needs-correction")

    # Abstract intermediates the oracle lacks (informational divergence).
    pymeos_abstracts = {"TNumber", "TPoint", "TGeomPoint", "TGeogPoint",
                        "Temporal", "TInstant", "TSequence", "TSequenceSet",
                        "TBool", "TInt", "TFloat", "TText"}
    for n, s in sorted(lat.items()):
        if s["kind"] in ("root", "abstract") and n not in pymeos_abstracts:
            cid = {"TAlpha": "OM-P2", "TSpatial": "OM-P7",
                   "TGeo": "OM-P7"}.get(n)
            add("abstract-missing-in-pymeos",
                f"{n} ({s.get('predicate')}) is a real MEOS grouping with "
                f"no PyMEOS abstract class",
                "known" if cid else "needs-correction", cid)

    # Collection hierarchy vs PyMEOS _CollectionFactory.
    o_coll = set(oracle["collection"].keys())
    for node, spec in sorted(coll_nodes.items()):
        if spec["temptype"] not in o_coll:
            add("collection-missing-in-pymeos",
                f"{node} ({spec['temptype']}) defined by MEOS, absent from "
                f"PyMEOS _CollectionFactory",
                "known", "OM-P6")

    aligned_concrete = len(meos_concrete & o_classes)
    needs = [w for w in work if w["status"] == "needs-correction"]
    return {
        "status": "audited",
        "oraclePath": str(PYMEOS),
        "total": len(work),
        "aligned": aligned_concrete,
        "divergences": len(work),
        "needsCorrection": len(needs),
        "knownCorrections": len(work) - len(needs),
        "byKind": _by_kind(work),
        "summary": om["summary"],
        "worklist": work,
    }


def _by_kind(work):
    out = {}
    for w in work:
        out[w["kind"]] = out.get(w["kind"], 0) + 1
    return out


def main() -> None:
    if not IN_PATH.exists():
        sys.exit(f"Catalog not found: {IN_PATH} — run `python run.py` first.")
    oracle = _parse_oracle(PYMEOS)
    rep = build_parity(json.loads(IN_PATH.read_text()), oracle)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(rep, indent=2))
    if rep["status"] == "oracle-unavailable":
        print(f"[object-model-parity] oracle unavailable ({PYMEOS}); "
              f"{rep['divergences']} curated corrections carried "
              f"→ {OUT_PATH}", file=sys.stderr)
    else:
        print(f"[object-model-parity] {rep['aligned']} concrete classes "
              f"aligned with PyMEOS; {rep['divergences']} divergences "
              f"({rep['knownCorrections']} known, {rep['needsCorrection']} "
              f"need a correction) → {OUT_PATH}", file=sys.stderr)
        for w in rep["worklist"]:
            if w["status"] == "needs-correction":
                print(f"  needs-correction: {w['kind']} — {w['detail']}",
                      file=sys.stderr)


if __name__ == "__main__":
    main()
