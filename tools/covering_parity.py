# Temporal-covering parity audit — the catalog analogue of the portable
# bare-name audit (tools/portable_parity.py).
#
#     python run.py                       # catalog with `temporalCovering` + functions
#     python tools/covering_parity.py     # -> output/meos-covering-parity.json
#
# The covering descriptor (meta/temporal-covering.json) is only useful to a
# binding generator if every C symbol it names is actually exported by the
# catalog, and every temporal type it lists is a real MeosType. This audit
# checks both and reports any miss as a precise worklist — an honest signal
# that an accessor must be added/exported in MEOS (close-in-MEOS-C), never a
# fabricated pass.

import json
import sys
from pathlib import Path

IN_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/meos-idl.json")
OUT_PATH = (Path(sys.argv[2]) if len(sys.argv) > 2
            else Path("output/meos-covering-parity.json"))


def _meos_type_tokens(catalog: dict) -> set:
    """Canonical lower-case temporal type names from the MeosType enum
    (`T_TGEOMPOINT` -> `tgeompoint`)."""
    tokens = set()
    for enum in catalog.get("enums", []):
        if enum.get("name") != "MeosType":
            continue
        for v in enum.get("values", []):
            name = v.get("name") if isinstance(v, dict) else v
            if isinstance(name, str) and name.startswith("T_"):
                tokens.add(name[2:].lower())
    return tokens


def build_parity(catalog: dict) -> dict:
    cov = catalog.get("temporalCovering")
    if not cov:
        raise ValueError("catalog has no `temporalCovering` — run run.py")
    names = {f["name"] for f in catalog.get("functions", [])}
    type_tokens = _meos_type_tokens(catalog)

    # 1. Every referenced C symbol must be exported by the catalog.
    symbols = {s: (s in names) for s in cov["symbols"]}
    missing_symbols = sorted(s for s, ok in symbols.items() if not ok)

    # 2. Every covered type must be a real MeosType (skip the enum check only
    #    when the catalog carries no MeosType enum, e.g. a synthetic unit-test
    #    catalog — then types are reported `unverified`, never silently ok).
    if type_tokens:
        types = {t: (t in type_tokens) for t in cov["types"]}
        invalid_types = sorted(t for t, ok in types.items() if not ok)
        types_checked = True
    else:
        types = {t: None for t in cov["types"]}
        invalid_types = []
        types_checked = False

    total_sym = len(symbols)
    backed_sym = total_sym - len(missing_symbols)
    return {
        "symbolsTotal": total_sym,
        "symbolsBacked": backed_sym,
        "symbolsMissing": missing_symbols,            # accessors to add/export
        "typesTotal": len(types),
        "typesValid": sum(1 for ok in types.values() if ok),
        "typesInvalid": invalid_types,
        "typesChecked": types_checked,
        "parityPct": round(backed_sym * 100 / total_sym, 1) if total_sym else 0,
        "bySymbol": symbols,
        "byType": types,
    }


def main() -> None:
    if not IN_PATH.exists():
        sys.exit(f"Catalog not found: {IN_PATH} — run `python run.py` first.")
    rep = build_parity(json.loads(IN_PATH.read_text()))
    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(rep, indent=2))
    print(f"[covering-parity] {rep['symbolsBacked']}/{rep['symbolsTotal']} "
          f"referenced symbols backed in the catalog ({rep['parityPct']}%); "
          f"{rep['typesValid']}/{rep['typesTotal']} types valid "
          f"→ {OUT_PATH}", file=sys.stderr)
    for s in rep["symbolsMissing"]:
        print(f"  missing-symbol: {s!r} — add/export in MEOS", file=sys.stderr)
    for t in rep["typesInvalid"]:
        print(f"  invalid-type: {t!r} — not a MeosType", file=sys.stderr)


if __name__ == "__main__":
    main()
