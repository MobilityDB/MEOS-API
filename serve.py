# Run the contract-driven MEOS HTTP server.
#
# Usage:
#     python run.py                       # produce the enriched catalog
#     python serve.py                     # serve output/meos-idl.json on :8080
#     python serve.py catalog.json 0.0.0.0 9000
#
# Engine: set MEOS_LIBRARY_PATH=/path/to/libmeos.so for the real ctypes
# engine; otherwise a non-computing StubEngine is used (routes/validation
# work, MEOS calls return placeholders).

import json
import sys
from pathlib import Path

from server.app import make_server
from server.engine import from_env

IN_PATH = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("output/meos-idl.json")
HOST = sys.argv[2] if len(sys.argv) > 2 else "127.0.0.1"
PORT = int(sys.argv[3]) if len(sys.argv) > 3 else 8080


def main() -> None:
    if not IN_PATH.exists():
        sys.exit(f"Catalog not found: {IN_PATH} — run `python run.py` first.")
    catalog = json.loads(IN_PATH.read_text())
    if not any("network" in f for f in catalog.get("functions", [])):
        sys.exit(f"{IN_PATH} is not enriched (no `network` fields).")

    engine = from_env()
    srv = make_server(catalog, engine, HOST, PORT)
    n = sum(1 for f in catalog["functions"]
            if f.get("network", {}).get("exposable"))
    print(f"MEOS server on http://{HOST}:{PORT}  "
          f"({n} operations, engine={getattr(engine, 'name', '?')})",
          file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        engine.close()


if __name__ == "__main__":
    main()
