import sys
import json
from pathlib import Path

from parser.parser import parse_all_headers, merge_meta
from parser.header_types import reconcile
from parser.enrich import enrich_idl


HEADERS_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./meos/include")
META_PATH   = Path("./meta/meos-meta.json")
OUTPUT_DIR  = Path("./output")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Parse C headers
    print(f"[1/4] Parsing {HEADERS_DIR}...", file=sys.stderr)
    idl = parse_all_headers(HEADERS_DIR)

    # 2. Restore opaque types the PG stub headers #define'd away to int.
    print(f"[2/4] Reconciling types from header source...", file=sys.stderr)
    idl = reconcile(idl, HEADERS_DIR)

    # 3. Derive service-projection metadata (category / encodings / network).
    #    Runs before the merge so manual annotations override the heuristics.
    print(f"[3/4] Enriching {len(idl['functions'])} functions...", file=sys.stderr)
    idl = enrich_idl(idl)

    # 4. Merge with manual metadata
    if META_PATH.exists():
        print(f"[4/4] Merging with {META_PATH}...", file=sys.stderr)
        idl = merge_meta(idl, META_PATH)
    else:
        print(f"[4/4] No meta found at {META_PATH}, skipping.", file=sys.stderr)

    idl_path = OUTPUT_DIR / "meos-idl.json"
    with open(idl_path, "w") as f:
        json.dump(idl, f, indent=2)
    print(f"      → {idl_path} written", file=sys.stderr)

    exposable = idl.get("enrichment", {}).get("exposableFunctions", 0)
    print(f"\nDone: {len(idl['functions'])} functions "
          f"({exposable} stateless-exposable), "
          f"{len(idl['structs'])} structs, "
          f"{len(idl['enums'])} enums", file=sys.stderr)


if __name__ == "__main__":
    main()
