"""Generate the OGC API – Moving Features OpenAPI projection from the
enriched MEOS catalog.

Usage:
    python run.py <path-to-MobilityDB-meos-include>     # produce output/meos-idl.json
    python enrich.py                                    # add network/wire/api fields
    python generate_movfeat_openapi.py                  # read output/meos-idl.json
    python generate_movfeat_openapi.py path.json out.json
"""

import json
import sys
from pathlib import Path

from generator.movfeat import build_movfeat_openapi, _missing_summary

IN_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/meos-idl.json")
OUT_PATH = (Path(sys.argv[2]) if len(sys.argv) > 2
            else Path("output/meos-movfeat-openapi.json"))


def main() -> None:
    if not IN_PATH.exists():
        sys.exit(f"Catalog not found: {IN_PATH} — run `python run.py` first.")

    catalog = json.loads(IN_PATH.read_text())
    if "functions" not in catalog or not any(
        "network" in f for f in catalog["functions"]
    ):
        sys.exit(f"{IN_PATH} is not enriched (no `network` fields). "
                 "Run the enrichment pass first.")

    doc = build_movfeat_openapi(catalog)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(doc, indent=2) + "\n")

    cov = doc["info"]["x-meos-coverage"]
    print(f"      → {OUT_PATH} written", file=sys.stderr)
    print(
        f"Done: {len(doc['paths'])} OGC API – Moving Features paths over "
        f"{len(doc['components']['schemas'])} component schemas; "
        f"{cov['meos_backed']}/{cov['routes']} routes have a MEOS backing, "
        f"{cov['persistence_only']} are persistence-layer.",
        file=sys.stderr,
    )

    msg = _missing_summary(cov["missing_in_catalog"])
    if msg:
        print(msg, file=sys.stderr)


if __name__ == "__main__":
    main()
