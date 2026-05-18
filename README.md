# MEOS-API

**MEOS-API** is the machine-readable description of the [MEOS](https://github.com/MobilityDB/MEOS) C library's public API. It is a JSON catalog — `meos-api.json` — that lists every function, structure, and enum that MEOS exposes, with the type signatures, ownership rules, and documentation strings.

This catalog is the foundation for generating language bindings (Python, Java, Rust, Go, .NET, JavaScript), building documentation, or powering any tooling that needs to understand the MEOS API without re-parsing C headers.

## Table of contents

- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [Output format](#output-format)
- [Adding metadata](#adding-metadata)
- [MCP generation](#mcp-generation)

## How it works

The pipeline runs in two steps:

1. **Parser** — scans the MEOS `.h` header files using libclang and extracts every function signature, struct, and enum into structured JSON.
2. **Merger** — enriches the parser output with manual annotations from `meta/meos-meta.json`, such as documentation and memory ownership rules.

## Getting started

### Prerequisites

- **Python 3.10** or later
- **Git** (to fetch the MobilityDB headers)

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Fetch the MEOS headers

This step downloads the MEOS and PostgreSQL headers directly from the MobilityDB GitHub repository. You only need to run it once.

```bash
python setup.py
```

To pin to a specific version:

```bash
python setup.py --branch v1.2.0
```

### 3. Generate the catalog

```bash
python run.py
```

The result is written to `output/meos-api.json`.

You can also point the tool at a different headers directory:

```bash
python run.py /path/to/custom/include
```

## Output format

`meos-api.json` contains 3 top-level arrays: `functions`, `structs`, and `enums`.

A typical function entry looks like this:

```json
{
  "name": "tpointseq_make",
  "file": "meos.h",
  "returnType": { "c": "TSequence *", "canonical": "TSequence *" },
  "params": [
    { "name": "instants", "cType": "TInstant **", "canonical": "TInstant **" },
    { "name": "count",    "cType": "int",          "canonical": "int" }
  ],
  "ownership": "caller",
  "nullable": true,
  "doc": "Creates a temporal point sequence from an array of instants."
}
```

## Adding metadata

Manual annotations (ownership rules, additional documentation, deprecation flags, etc.) live in `meta/meos-meta.json`. The merger applies them on top of the libclang-parsed structure when generating the final catalog.

## MCP generation

The enriched catalog also projects onto a **Model Context Protocol (MCP)**
tool manifest, so an LLM/agent can call the MEOS value algebra directly:

```bash
python run.py                 # produce the enriched catalog
python generate_mcp.py        # output/meos-idl.json -> output/meos-mcp.json
```

Every *stateless-exposable* MEOS function becomes one MCP tool with a
**self-contained** JSON Schema (2020-12) — enums and opaque-type schemas are
inlined, since MCP clients don't resolve external `$ref`s. Spatiotemporal
values are passed as serialized strings (text/WKT, MF-JSON, HexWKB);
`annotations` mark the tools read-only/idempotent; `x-meos.{decode,encode}`
give a runtime everything it needs to dispatch a call.

Against the live MobilityDB `master` catalog this yields **1952 tools**
(90% of the public API; internal `meos_internal*.h` policy-excluded),
array params rendered as JSON arrays.
Pure `dict` → `dict` (no libclang, no MEOS runtime); see
[`docs/mcp.md`](docs/mcp.md) for the projection rules and roadmap, and
[`tests/test_mcp.py`](tests/test_mcp.py) for worked examples
(`python3 tests/test_mcp.py`).
