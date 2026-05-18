import sys
import json
from pathlib import Path

from parser.parser import parse_all_headers, merge_meta
from parser.object_model import attach_object_model, find_mobilitydb_src


HEADERS_DIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("./meos/include")
META_PATH    = Path("./meta/meos-meta.json")
OBJMODEL_PATH = Path("./meta/object-model.json")
OUTPUT_DIR   = Path("./output")

# MobilityDB C sources for the error-contract scan. Explicit argv[2] wins;
# otherwise resolved (env / _mobilitydb sparse checkout / src sibling).
# Absent → honest source-unavailable signal, never a fabricated empty set.
MOBILITYDB_SRC = (Path(sys.argv[2]) if len(sys.argv) > 2
                  else find_mobilitydb_src(HEADERS_DIR))


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

    # 3. Derive the explicit object model (class lattice + methods + error
    #    contract) from the implicit MEOS prefix convention.
    print(f"[3/3] Deriving object model from {OBJMODEL_PATH} "
          f"(error scan: {MOBILITYDB_SRC})...", file=sys.stderr)
    idl = attach_object_model(idl, OBJMODEL_PATH, MOBILITYDB_SRC)

    idl_path = OUTPUT_DIR / "meos-idl.json"
    with open(idl_path, "w") as f:
        json.dump(idl, f, indent=2)
    print(f"      → {idl_path} written", file=sys.stderr)

    om = idl.get("objectModel", {}).get("summary", {})
    print(f"\nDone: {len(idl['functions'])} functions, "
          f"{len(idl['structs'])} structs, "
          f"{len(idl['enums'])} enums", file=sys.stderr)
    if om:
        print(f"      object model: {om['classesWithMethods']} classes, "
              f"{om['functionsClassified']}/{om['functionsTotal']} functions "
              f"classified ({om['coveragePct']}%), "
              f"errors: {om['errorStatus']}", file=sys.stderr)


if __name__ == "__main__":
    main()
