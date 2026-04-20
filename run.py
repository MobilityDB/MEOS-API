import sys
import json
from pathlib import Path

from parser.parser import parse_all_headers, merge_meta


HEADERS_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./meos/include")
META_PATH   = Path("./meta/meos-meta.json")
OUTPUT_DIR  = Path("./output")


def main():
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 1. Parse C headers
    print(f"[1/2] Parsing {HEADERS_DIR}...", file=sys.stderr)
    idl = parse_all_headers(HEADERS_DIR)

    # 2. Merge with manual metadata
    if META_PATH.exists():
        print(f"[2/2] Merging with {META_PATH}...", file=sys.stderr)
        idl = merge_meta(idl, META_PATH)
    else:
        print(f"[2/2] No meta found at {META_PATH}, skipping.", file=sys.stderr)

    idl_path = OUTPUT_DIR / "meos-idl.json"
    with open(idl_path, "w") as f:
        json.dump(idl, f, indent=2)
    print(f"      → {idl_path} written", file=sys.stderr)

    print(f"\nDone: {len(idl['functions'])} functions, "
          f"{len(idl['structs'])} structs, "
          f"{len(idl['enums'])} enums", file=sys.stderr)


if __name__ == "__main__":
    main()
