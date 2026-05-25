import sys
import json
from pathlib import Path

from parser.parser import parse_all_headers, merge_meta
from parser.portable import attach_portable_aliases
from parser.shapeinfer import infer_shapes
from parser.nullable import merge_nullable


HEADERS_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./meos/include")
META_PATH   = Path("./meta/meos-meta.json")
PORTABLE_PATH = Path("./meta/portable-aliases.json")
OUTPUT_DIR  = Path("./output")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Parse C headers
    print(f"[1/3] Parsing {HEADERS_DIR}...", file=sys.stderr)
    idl = parse_all_headers(HEADERS_DIR)

    # 1b. Generate the codegen `shape` from the signatures + Doxygen, replacing
    #     the hand-maintained meta stub.  outputArrays/arrayReturn come from the
    #     parameter forms; nullable comes from the C `@param ... may be NULL` SoT.
    idl, sh = infer_shapes(idl)
    print(f"      inferred shape: {sh['arrayReturn']} array returns, "
          f"{sh['outputArrays']} output arrays", file=sys.stderr)
    idl, nn = merge_nullable(idl, HEADERS_DIR.parent)
    print(f"      nullable params from Doxygen `may be NULL`: {nn}",
          file=sys.stderr)

    # 2. Merge with manual metadata
    if META_PATH.exists():
        print(f"[2/3] Merging with {META_PATH}...", file=sys.stderr)
        idl = merge_meta(idl, META_PATH)
    else:
        print(f"[2/3] No meta found at {META_PATH}, skipping.", file=sys.stderr)

    # 3. Attach the canonical portable bare-name mapping (codegen truth)
    print(f"[3/3] Attaching portable aliases from {PORTABLE_PATH}...",
          file=sys.stderr)
    idl = attach_portable_aliases(idl, PORTABLE_PATH)

    idl_path = OUTPUT_DIR / "meos-idl.json"
    with open(idl_path, "w") as f:
        json.dump(idl, f, indent=2)
    print(f"      → {idl_path} written", file=sys.stderr)

    pa = idl.get("portableAliases", {}).get("count", 0)
    print(f"\nDone: {len(idl['functions'])} functions, "
          f"{len(idl['structs'])} structs, "
          f"{len(idl['enums'])} enums, "
          f"{pa} portable bare-name aliases", file=sys.stderr)


if __name__ == "__main__":
    main()
