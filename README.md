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
- [MCP generation](#mcp-generation)
- [The object model](#the-object-model)
- [Runtime server](#runtime-server)

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

The pipeline runs as a chain of stages:

1. **Parser** — scans the MEOS `.h` header files using libclang and extracts every function signature, struct, and enum into structured JSON; a recovery pass restores the PG-vendored C types (`bool`, `int64`, `Timestamp(Tz)`, `H3Index`) that the preprocessor collapses to `int`.
2. **Reconcile** — restores opaque types the PostgreSQL stub headers `#define` to `int` (`Interval`, `text`, …) from the header source, so they are not mistaken for `int *` out-parameters.
3. **Enrich** — derives the service-projection metadata (`category` / `typeEncodings` / `network` / `wire`).
4. **Merger** — applies manual annotations from `meta/meos-meta.json` (documentation, ownership, overrides) on top.
5. **Portable aliases** — attaches the canonical portable bare-name mapping from `meta/portable-aliases.json`. See [Portable bare-name dialect](#portable-bare-name-dialect).
6. **Object model** — makes the *implicit* MEOS class hierarchy explicit: it derives the class lattice and assigns every function to the class it is a method of, from the canonical mapping in `meta/object-model.json`. See [The object model](#the-object-model).

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

The result is written to `output/meos-idl.json`.

You can also point the tool at a different headers directory:

```bash
python run.py /path/to/custom/include
```

The object-model step also derives the per-function error contract by
scanning the MobilityDB C sources (`_mobilitydb/meos/src`, fetched by
`setup.py`). To audit the derived lattice against the most mature
hand-built model (PyMEOS):

```bash
python object_model_parity.py   # -> output/meos-object-model-parity.json
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

In addition, `meos-idl.json` carries an `objectModel` block: the explicit
class lattice (`classes`, `lattice`), the reverse index assigning each
function to the class it is a method of (`functionToClass`), the
closed-algebra companion hierarchies (`companions`), the error contract
(`errors`), and the irregularity worklist (`corrections`).

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

Against the live MobilityDB `master` catalog this yields one MCP tool per
stateless-exposable function (internal `meos_internal*.h` policy-excluded),
array params rendered as JSON arrays.
Pure `dict` → `dict` (no libclang, no MEOS runtime); see
[`docs/mcp.md`](docs/mcp.md) for the projection rules and roadmap, and
[`tests/test_mcp.py`](tests/test_mcp.py) for worked examples
(`python3 tests/test_mcp.py`).

## The object model

MEOS is C — it has no classes. The object model is encoded by convention
in the `Temporal`/`TInstant`/`TSequence`/`TSequenceSet` struct family (the
template axis), the `temptype` discriminator whose base type is the
missing template parameter (the type-family axis), and the function-name
prefixes that bind a function to the class it is a method of
(`temporal_*` = the late-bound superclass; `tnumber_*`/`tspatial_*`/
`tpoint_*`/`tgeo_*` = abstract families; `tbool_*`/`tint_*`/… = exact
types). `meta/object-model.json` makes that lattice explicit so every
binding/engine derives the **same** classes and methods from one mapping.

See [docs/object-model.md](docs/object-model.md) for the full
specification, the closed-algebra companion hierarchies, the error
contract, the parity audit, and the irregularity worklist.

## Runtime server

The same enriched catalog also drives a runtime HTTP server — the projection
that **executes** rather than just describes:

```bash
python run.py                                 # produce the enriched catalog
python serve.py                               # 127.0.0.1:8080 (StubEngine)
MEOS_LIBRARY_PATH=/path/libmeos.so python serve.py   # real engine
```

Each *stateless-exposable* function is served as `POST /{function}`:
validate the JSON body, `decode` each serialized string to an opaque handle,
`invoke` the function, `encode` the result, reply `{"result": …}` (`204`
void, `400 {"error","code"}` on failure). All MEOS work sits behind a
pluggable `Engine`: `CtypesEngine` (`dlopen` a built `libmeos`, every opaque
value an anonymous `void *`) or `StubEngine` (no build needed; routing and
validation still work).

Built from the live MobilityDB `master` catalog this is **1963 operations**
(91% of the public API; internal `meos_internal*.h` policy-excluded);
generation, routing, validation and dispatch are exercised end-to-end over
real HTTP (`tests/test_server.py`), and the full stack is validated against
an installed `/usr/local/lib/libmeos.so` — `POST /temporal_copy` →
`200 {"result":"{t@…, f@…}"}` (decode → invoke → `temporal_out(maxdd=15)`),
`floatset_value_n` n=2 → `2.5` via a byref out-parameter, a malformed body
→ `400` with the real MEOS message, and the server survives it
(`tests/test_engine_integration.py`, skipped unless `MEOS_LIBRARY_PATH` is
set). See [`docs/server.md`](docs/server.md). Stdlib only (`http.server`);
no new dependencies.
