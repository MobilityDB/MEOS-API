# Portable bare-name parity audit — the meos-api.json analogue of
# MobilityDB's `tools/portable_aliases/generate.py --check`.
#
#     python run.py                 # catalog with `portableAliases` + functions
#     python tools/portable_parity.py     # -> output/meos-portable-parity.json
#
# For every canonical bare name (PR #8 / RFC #920) it reports the catalog
# function family that backs it, by the MEOS bare-name prefix convention
# (`overlaps_*`, `teq_*`, `same_*`, …). A bare name with no prefix match is
# **not** asserted to be an API gap (some map through a different C prefix,
# e.g. `nearestApproachDistance` ↔ `nad_*`): it is flagged
# `needs-explicit-backing` so the cross-repo work can add an explicit
# operator→C-family entry — an honest signal, never a fabricated verdict.

import json
import sys
from pathlib import Path

IN_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/meos-idl.json")
OUT_PATH = (Path(sys.argv[2]) if len(sys.argv) > 2
            else Path("output/meos-portable-parity.json"))


def build_parity(catalog: dict) -> dict:
    pa = catalog.get("portableAliases")
    if not pa:
        raise ValueError("catalog has no `portableAliases` — run run.py")
    fam_of = {p["bareName"]: (fam, p["operator"])
              for fam, lst in pa["families"].items() for p in lst}
    explicit = pa.get("explicitBacking", {})
    names = [f["name"] for f in catalog.get("functions", [])]

    def _matches(prefix):
        return [n for n in names
                if n == prefix or n.startswith(prefix + "_")]

    by_bare = {}
    for bare, (fam, op) in sorted(fam_of.items()):
        hits, via = _matches(bare), "prefix"
        if not hits:                          # try the verified explicit map
            for pref in explicit.get(bare, []):
                hits += _matches(pref)
            via = "explicit" if hits else None
        by_bare[bare] = {
            "operator": op, "family": fam, "via": via,
            "backedBy": len(hits),
            "sample": sorted(hits)[:3],
            "status": "backed" if hits else "needs-explicit-backing",
        }
    backed = [b for b, v in by_bare.items() if v["status"] == "backed"]
    unbacked = sorted(b for b, v in by_bare.items()
                      if v["status"] == "needs-explicit-backing")
    total = len(by_bare)

    # Defensive cross-reference: every `alreadyCanonical` family entry has a
    # `pattern` like `"ever_*"` that must match at least one catalog function.
    # If upstream renames `ever_*` → `e_*`, the pattern will match zero — this
    # is the audit's "this curated assumption no longer holds" signal,
    # surfaced honestly so the next regen catches the drift instead of
    # silently passing on a stale curated entry.
    canonical_drift = []
    for entry in pa.get("alreadyCanonical", []):
        if entry.get("kind") != "family":
            continue
        pat = entry.get("pattern", "")
        if not pat:
            continue
        # Strip trailing '*' wildcard for the prefix match
        prefix = pat[:-1] if pat.endswith("*") else pat
        matches = [n for n in names if n.startswith(prefix)]
        if not matches:
            canonical_drift.append({
                "family": entry.get("family"),
                "pattern": pat,
                "issue": "pattern matches zero catalog functions — upstream may have renamed the family",
            })

    return {
        "total": total,
        "backed": len(backed),
        "needsExplicitBacking": len(unbacked),
        "parityPct": round(len(backed) * 100 / total, 1) if total else 0,
        "canonicalDrift": canonical_drift,  # empty list = no drift detected
        "unbacked": unbacked,           # the precise cross-repo worklist
        "byBareName": by_bare,
    }


def main() -> None:
    if not IN_PATH.exists():
        sys.exit(f"Catalog not found: {IN_PATH} — run `python run.py` first.")
    rep = build_parity(json.loads(IN_PATH.read_text()))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(rep, indent=2))
    print(f"[portable-parity] {rep['backed']}/{rep['total']} bare names "
          f"backed in the catalog ({rep['parityPct']}%); "
          f"{rep['needsExplicitBacking']} need an explicit backing entry "
          f"→ {OUT_PATH}", file=sys.stderr)
    for b in rep["unbacked"]:
        v = rep["byBareName"][b]
        print(f"  needs-explicit-backing: {b!r}  ({v['operator']}, "
              f"{v['family']})", file=sys.stderr)
    for drift in rep["canonicalDrift"]:
        print(f"  canonical-drift: family={drift['family']!r} pattern={drift['pattern']!r} — "
              f"{drift['issue']}", file=sys.stderr)


if __name__ == "__main__":
    main()
