# MEOS-API

**MEOS-API** is the machine-readable description of the [MEOS](https://github.com/MobilityDB/MEOS) C library's public API. It is a JSON catalog — `meos-api.json` — that lists every function, structure, and enum that MEOS exposes, with the type signatures, ownership rules, and documentation strings.

This catalog is the foundation for generating language bindings (Python, Java, Rust, Go, .NET, JavaScript), building documentation, or powering any tooling that needs to understand the MEOS API without re-parsing C headers.

## Table of contents

- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [Output format](#output-format)
- [Service-projection metadata](#service-projection-metadata)
- [Adding metadata](#adding-metadata)

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
