import sys
import json
from pathlib import Path

from parser.parser import parse_all_headers, merge_meta
from parser.portable import attach_portable_aliases
from parser.sqlnames import merge_sql_names


HEADERS_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./meos/include")
META_PATH   = Path("./meta/meos-meta.json")
PORTABLE_PATH = Path("./meta/portable-aliases.json")
OUTPUT_DIR  = Path("./output")
# Full MobilityDB checkout (meos/src + mobilitydb/src) carrying the Doxygen
# @csqlfn / @sqlfn / @sqlop tags that link the MEOS-C, MobilityDB-C and SQL names.
MDB_SRC     = Path("./_mobilitydb")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Parse C headers
    print(f"[1/3] Parsing {HEADERS_DIR}...", file=sys.stderr)
    idl = parse_all_headers(HEADERS_DIR)

    # 2. Merge with manual metadata
    if META_PATH.exists():
        print(f"[2/3] Merging with {META_PATH}...", file=sys.stderr)
        idl = merge_meta(idl, META_PATH)
    else:
        print(f"[2/3] No meta found at {META_PATH}, skipping.", file=sys.stderr)

    # 2b. Attach the MEOS-C -> MobilityDB-C -> SQL name chain (+ operator) from the
    #     @csqlfn / @sqlfn / @sqlop Doxygen tags, so every binding derives its names
    #     from one source of truth instead of a hand-maintained per-binding map.
    idl, sn = merge_sql_names(idl, MDB_SRC)
    print(f"      SQL name chain attached to {sn} functions", file=sys.stderr)

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
