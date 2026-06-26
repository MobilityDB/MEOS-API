"""Generate the temporal-covering projection from the enriched MEOS catalog.

Usage:
    python run.py <path-to-MobilityDB-meos-include>   # produce output/meos-idl.json
    python generate_covering.py                       # read output/meos-idl.json
    python generate_covering.py path.json out.json
"""

import json
import sys
from pathlib import Path

from generator.covering import build_covering_projection

IN_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/meos-idl.json")
OUT_PATH = (Path(sys.argv[2]) if len(sys.argv) > 2
            else Path("output/meos-covering-projection.json"))


def main() -> None:
    if not IN_PATH.exists():
        sys.exit(f"Catalog not found: {IN_PATH} — run `python run.py` first.")

    catalog = json.loads(IN_PATH.read_text())
    if "temporalCovering" not in catalog:
        sys.exit(f"{IN_PATH} has no `temporalCovering` — it is attached by "
                 "run.py; regenerate the catalog.")

    proj = build_covering_projection(catalog)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(proj, indent=2) + "\n")
    print(f"      → {OUT_PATH} written", file=sys.stderr)
    print(f"[covering-projection] {proj['count']} temporal types projected "
          f"to covering columns", file=sys.stderr)


if __name__ == "__main__":
    main()
