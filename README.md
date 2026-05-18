# MEOS-API

**MEOS-API** is the machine-readable description of the [MEOS](https://github.com/MobilityDB/MEOS) C library's public API. It is a JSON catalog — `meos-api.json` — that lists every function, structure, and enum that MEOS exposes, with the type signatures, ownership rules, and documentation strings.

This catalog is the foundation for generating language bindings (Python, Java, Rust, Go, .NET, JavaScript), building documentation, or powering any tooling that needs to understand the MEOS API without re-parsing C headers.

## Table of contents

- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [Output format](#output-format)
- [Adding metadata](#adding-metadata)
- [OpenAPI generation](#openapi-generation)

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

## OpenAPI generation

The enriched catalog (the `network` / `wire` / `typeEncodings` produced by the
service-projection pass) can be projected onto an **OpenAPI 3.1** contract —
this is the concrete "OpenAPI is a projection of MEOS-API" step:

```bash
python run.py                 # produce the enriched catalog
python generate_openapi.py    # output/meos-idl.json -> output/meos-openapi.json
```

Every *stateless-exposable* MEOS function becomes one RPC-style
`POST /{function}` operation (≈ an OGC API – Processes "process"); opaque
values cross the wire as strings carried in their `typeEncodings`
(text / MF-JSON / HexWKB), surfaced as reusable component schemas. `x-meos-*`
extensions carry the decode/encode function names and category so a
downstream server or MCP generator can consume the same document.

Against the live MobilityDB `master` catalog this yields **1952 operations**
(90% of the public API; internal `meos_internal*.h` policy-excluded),
including array-of-string params for builders. The generator is pure
`dict` → `dict` (no libclang,
no MEOS runtime); see [`docs/openapi.md`](docs/openapi.md) for the projection
rules, `x-meos-*` extensions, and roadmap (OGC API, MCP, runtime server), and
[`tests/test_openapi.py`](tests/test_openapi.py) for worked examples
(`python3 tests/test_openapi.py`).
