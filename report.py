# Emit a coverage / irregularity report from the enriched catalog.
#
#     python run.py                 # produce output/meos-idl.json
#     python report.py              # -> output/meos-coverage.json (+ stderr summary)
#
# `worklist` is the actionable form: one entry per non-exposable public
# function with a `class` and a concrete `suggest` — the precise upstream
# regularization that would make it stateless-projectable (e.g. "rename the
# out-parameter to `result`", "add a trailing `int *count`", "return a
# struct instead of N out-parameters", "add a single-arg `T_in`/`T_out`").
# `byClass` ranks the classes by size, so the upstream work is prioritised
# by leverage. Fixing an irregularity upstream removes its entry and lifts
# coverage toward 100%; internal (`meos_internal*.h`) functions and
# inherently-stateful aggregates are reported but are correct exclusions,
# never silent gaps. This is the direct, no-coupling input for the
# cross-repo API-uniformization workstream.

import json
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _base(c: str) -> str:
    return " ".join(re.sub(r"\b(const|volatile|struct|union|enum)\b", " ",
                           c).replace("*", " ").split())


def _dep(c: str) -> int:
    return c.count("*")


def _classify(fn: dict):
    """``(klass, suggestion)`` — the concrete upstream regularization that
    would make this function stateless-projectable. Turns the worklist from
    *what is broken* into *what to change upstream*.
    """
    reason = fn["network"]["reason"] or ""
    tags = {p.split(":")[0] for p in reason.split("; ")}
    detail = {p.split(":", 1)[1] for p in reason.split("; ") if ":" in p}
    params = fn.get("params", [])
    ret = fn.get("returnType", {}).get("canonical", "")

    if tags & {"lifecycle", "index"}:
        return ("plumbing",
                "intentionally not exposed (process/library plumbing)")
    if fn.get("category") == "aggregate" or "SkipList" in detail:
        return ("stateful",
                "stateful aggregation — expose via a stateful endpoint, "
                "not a stateless RPC")
    if "Datum" in detail or "MeosArray" in detail:
        return ("internal-generic",
                "Datum/array-generic — expose a typed variant; keep the "
                "generic form internal")

    outptrs = [p for p in params if _dep(p["canonical"]) >= 2
               or (_dep(p["canonical"]) == 1 and _base(p["canonical"]) in
                   ("int", "long", "double", "float", "bool"))]
    if "unsupported-return" in tags and _dep(ret) >= 1:
        return ("array-return-shape",
                f"array return `{ret}` lacks a length: add a trailing "
                "`int *count` out-parameter (+ an element encoder)")
    if len(outptrs) >= 2:
        return ("multi-out",
                f"{len(outptrs)} out-parameters: return a struct (or split "
                "into separate single-result accessors)")
    if len(outptrs) == 1 and outptrs[0]["name"] not in ("result", "value"):
        return ("out-param-naming",
                f"rename out-parameter `{outptrs[0]['name']}` to `result` "
                "(`bool f(.., T *result)` convention)")
    nocodec = sorted(d for d in detail if d[:1].isupper())
    if tags & {"no-decoder", "no-encoder"} and nocodec:
        t = nocodec[0]
        return ("no-codec",
                f"type `{t}` has no stateless codec: add a single-argument "
                f"`{t.lower()}_in`/`{t.lower()}_out` wrapper, or keep it "
                "internal")
    if "array-or-out-param" in tags:
        return ("array-shape",
                "pass element arrays as an adjacent `(Elem **arr, int "
                "count)`; use `bool f(.., T *result)` for out-values")
    return ("other", "regularize the signature to a stateless shape")


IN_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/meos-idl.json")
OUT_PATH = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("output/meos-coverage.json")


def build_report(catalog: dict) -> dict:
    fns = catalog.get("functions", [])
    pub = [f for f in fns if f.get("api") == "public"]
    exposable = [f for f in pub if f["network"]["exposable"]]
    by_reason: dict = defaultdict(list)
    for f in pub:
        if f["network"]["exposable"]:
            continue
        # collapse "tag:detail; tag:detail" to the set of distinct tags
        tags = sorted({p.split(":")[0]
                       for p in (f["network"]["reason"] or "").split("; ")})
        by_reason["; ".join(tags)].append(f["name"])

    worklist = []
    for f in pub:
        if f["network"]["exposable"]:
            continue
        klass, suggest = _classify(f)
        worklist.append({
            "name": f["name"], "file": f.get("file"),
            "reason": f["network"]["reason"],
            "class": klass, "suggest": suggest,
        })
    worklist.sort(key=lambda w: (w["class"], w["name"]))
    by_class = Counter(w["class"] for w in worklist)

    total = len(pub)
    n_exp = len(exposable)
    return {
        "publicTotal": total,
        "exposable": n_exp,
        "coveragePct": round(n_exp * 100 / total, 1) if total else 0,
        "internalExcluded": len(fns) - total,
        "gap": total - n_exp,
        "byClass": dict(by_class.most_common()),
        "byReason": {k: sorted(v)
                     for k, v in sorted(by_reason.items(),
                                        key=lambda kv: -len(kv[1]))},
        # actionable: one upstream-change suggestion per gap function
        "worklist": worklist,
    }


def main() -> None:
    if not IN_PATH.exists():
        sys.exit(f"Catalog not found: {IN_PATH} — run `python run.py` first.")
    catalog = json.loads(IN_PATH.read_text())
    if not any("network" in f for f in catalog.get("functions", [])):
        sys.exit(f"{IN_PATH} is not enriched.")

    rep = build_report(catalog)
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(rep, indent=2))

    print(f"[coverage] public {rep['exposable']}/{rep['publicTotal']} "
          f"({rep['coveragePct']}%), gap {rep['gap']}, "
          f"{rep['internalExcluded']} internal excluded → {OUT_PATH}",
          file=sys.stderr)
    for klass, n in rep["byClass"].items():
        print(f"  {n:4d}  {klass}", file=sys.stderr)


if __name__ == "__main__":
    main()
