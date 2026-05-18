# Generate an MCP tool manifest from the enriched MEOS catalog.
#
# Usage:
#     python run.py                    # first, to produce the catalog
#     python generate_mcp.py           # output/meos-idl.json -> output/meos-mcp.json
#     python generate_mcp.py in.json [out.json]

import json
import sys
from pathlib import Path

from generator.mcp import build_mcp

IN_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/meos-idl.json")
OUT_PATH = Path(sys.argv[2]) if len(sys.argv) > 2 else Path("output/meos-mcp.json")


def main() -> None:
    if not IN_PATH.exists():
        sys.exit(f"Catalog not found: {IN_PATH} — run `python run.py` first.")

    catalog = json.loads(IN_PATH.read_text())
    if "functions" not in catalog or not any(
        "network" in f for f in catalog["functions"]
    ):
        sys.exit(f"{IN_PATH} is not enriched (no `network` fields). "
                 "Run the enrichment pass first.")

    manifest = build_mcp(catalog)

    OUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUT_PATH.write_text(json.dumps(manifest, indent=2))

    print(f"[mcp] {len(manifest['tools'])} tools → {OUT_PATH}",
          file=sys.stderr)


if __name__ == "__main__":
    main()
