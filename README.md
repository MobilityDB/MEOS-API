# MEOS-API

**MEOS-API** is the machine-readable description of the [MEOS](https://github.com/MobilityDB/MEOS) C library's public API. It is a JSON catalog — `meos-api.json` — that lists every function, structure, and enum that MEOS exposes, with the type signatures, ownership rules, and documentation strings.

This catalog is the foundation for generating language bindings (Python, Java, Go, Rust, and .NET/C#), building documentation, or powering any tooling that needs to understand the MEOS API without re-parsing C headers. See [Ecosystem](#ecosystem) for the projects that build on it.

## Table of contents

- [Ecosystem](#ecosystem)
- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [Output format](#output-format)
- [Service-projection metadata](#service-projection-metadata)
- [Adding metadata](#adding-metadata)
- [Portable bare-name dialect](#portable-bare-name-dialect)
- [OpenAPI generation](#openapi-generation)

## Ecosystem

MEOS is the common computational substrate of the MobilityDB ecosystem. This
catalog is the language-independent contract that the projects below consume —
directly against the MEOS C library or by code-generating from `meos-api.json`.
The full ecosystem (including visualization and tooling) lives at
[github.com/MobilityDB](https://github.com/MobilityDB).

### Engine

| Project | Description |
|---|---|
| [MEOS](https://github.com/MobilityDB/MEOS) | Mobility Engine Open Source — the core C library this catalog describes |
| [MobilityDB](https://github.com/MobilityDB/MobilityDB) | PostgreSQL / PostGIS extension for spatiotemporal trajectory data |
| [MobilityDuck](https://github.com/MobilityDB/MobilityDuck) | DuckDB extension for temporal and spatiotemporal data |

### Language bindings

| Project | Language |
|---|---|
| [PyMEOS](https://github.com/MobilityDB/PyMEOS) (via [PyMEOS-CFFI](https://github.com/MobilityDB/PyMEOS-CFFI)) | Python |
| [JMEOS](https://github.com/MobilityDB/JMEOS) | Java |
| [GoMEOS](https://github.com/MobilityDB/GoMEOS) | Go |
| [meos-rs](https://github.com/MobilityDB/meos-rs) | Rust |
| [MEOS.NET](https://github.com/MobilityDB/MEOS.NET) | .NET / C# |
| [MEOS.js](https://github.com/MobilityDB/MEOS.js) | JavaScript (WebAssembly / Node.js) |

### Analytics & services

| Project | Description |
|---|---|
| [MobilitySpark](https://github.com/MobilityDB/MobilitySpark) | Trajectory data platform on Spark-SQL |
| [MobilityAPI](https://github.com/MobilityDB/MobilityAPI) | HTTP server implementing the OGC API – Moving Features standard |

### Catalog projections

Beyond binding code-generation, the enriched catalog is projected into
service contracts generated from the same model:

| Projection | Description |
|---|---|
| OpenAPI 3.1 contract | A language-agnostic HTTP contract for the MEOS value algebra |
| Model Context Protocol (MCP) tool manifest | One MCP tool per stateless-exposable function, so LLMs/agents can call the MEOS spatiotemporal algebra directly |
| Contract-driven runtime server | An HTTP server with a pluggable MEOS engine, driven by the generated contract |

## How it works

The pipeline runs in four steps:

1. **Parser** — scans the MEOS `.h` header files using libclang and extracts every function signature, struct, and enum into structured JSON.
2. **Reconcile** — restores opaque types the PostgreSQL stub headers `#define` to `int` (`Interval`, `text`, …) from the header source, so they are not mistaken for `int *` out-parameters.
3. **Enrich** — derives the service-projection metadata (`category` / `typeEncodings` / `network` / `wire`).
4. **Merger** — applies manual annotations from `meta/meos-meta.json` (documentation, ownership, overrides) on top.

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

## Service-projection metadata

C headers describe *signatures*; they do not say what a function **is**, how an
opaque type crosses the wire, or whether an operation can be served
*statelessly*. A second pass (`parser/enrich.py`) derives that — the metadata a
service generator (OpenAPI, MCP, gRPC, …) needs to project MEOS onto a network
API. It runs **before** the merge, so every derived field is overridable from
`meta/meos-meta.json`.

Each function gains a `category`, a `network` verdict, and a `wire` mapping:

```json
{
  "name": "temporal_eq",
  "returnType": { "c": "bool", "canonical": "int" },
  "params": [ { "name": "temp1", "canonical": "const struct Temporal *" },
              { "name": "temp2", "canonical": "const struct Temporal *" } ],
  "category": "predicate",
  "network": { "exposable": true, "method": "POST", "reason": null },
  "wire": {
    "params": [
      { "name": "temp1", "kind": "serialized", "cType": "const struct Temporal *",
        "decode": "temporal_in", "encodings": ["mfjson","text","wkb"] },
      { "name": "temp2", "kind": "serialized", "cType": "const struct Temporal *",
        "decode": "temporal_in", "encodings": ["mfjson","text","wkb"] }
    ],
    "result": { "kind": "json", "json": "integer" }
  }
}
```

(MEOS predicates return `int`, and libclang emits canonical spellings such as
`const struct Temporal *` — the enrichment matches those.)

```text
Live coverage (MobilityDB master): 2161 public + 511 internal functions.
The service projects the public user API; internal (meos_internal*.h,
Datum-generic) is policy-excluded.
  1963 / 2161 = 91% of the public API stateless-exposable (verified).
```

The catalog also gains a top-level `typeEncodings` map (opaque type → its
in/out functions) and an `enrichment` summary (category counts, exposable
count) for coverage tracking. Non-exposable functions carry a precise
`reason` (`array-or-out-param:…`, `no-encoder:…`, `lifecycle`, `index`, …) so
generators can report exactly what they can and cannot emit.

See [`docs/enrichment.md`](docs/enrichment.md) for the full contract and
[`tests/test_enrich.py`](tests/test_enrich.py) for worked examples on real
MEOS signatures (run: `python3 tests/test_enrich.py`).

## Adding metadata

Manual annotations (ownership rules, additional documentation, deprecation flags, etc.) live in `meta/meos-meta.json`. The merger applies them on top of the libclang-parsed structure when generating the final catalog — including any field derived by the service-projection pass (e.g. correcting a `category` or forcing `network.exposable`).

## Portable bare-name dialect

`meta/portable-aliases.json` is the **single codegen source of truth**
(RFC #920) for the canonical portable bare-name dialect — the operator →
bare-name mapping that MobilityDB now registers natively (PR #1075). The
pipeline folds it into the catalog as `portableAliases` (with `byOperator`
/ `byBareName` lookups), so **every binding/engine generates the identical
bare names** and a user learns one reference and assumes the rest.

It is curated canonical data, kept verbatim (only bijective lookups are
derived — no C-symbol guessing; upstream aliases reuse each operator's own
backing function, equivalence by construction). The mapping is
type-agnostic and applies to **every** temporal type family —
`temporal`, `geo`, `cbuffer`, `npoint`, `pose`, `rgeo` are all in scope and
must not be excluded from any parity headline. `python tools/portable_parity.py`
audits it against the catalog — currently **29/29 = 100%** backed (verified,
no guessing). See [`docs/portable-aliases.md`](docs/portable-aliases.md).

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
