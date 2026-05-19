# MEOS-API

**MEOS-API** is the machine-readable description of the [MEOS](https://github.com/MobilityDB/MEOS) C library's public API. It is a JSON catalog — `meos-api.json` — that lists every function, structure, and enum that MEOS exposes, with the type signatures, ownership rules, and documentation strings.

This catalog is the foundation for generating language bindings (Python, Java, Go, Rust, and .NET/C#), building documentation, or powering any tooling that needs to understand the MEOS API without re-parsing C headers. See [Ecosystem](#ecosystem) for the projects that build on it.

## Table of contents

- [Ecosystem](#ecosystem)
- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [Output format](#output-format)
- [Adding metadata](#adding-metadata)

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
