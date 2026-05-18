# MEOS-API

**MEOS-API** is the machine-readable description of the [MEOS](https://github.com/MobilityDB/MEOS) C library's public API. It is a JSON catalog — `meos-api.json` — that lists every function, structure, and enum that MEOS exposes, with the type signatures, ownership rules, and documentation strings.

This catalog is the foundation for generating language bindings (Python, Java, Rust, Go, .NET, JavaScript), building documentation, or powering any tooling that needs to understand the MEOS API without re-parsing C headers.

## Table of contents

- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [Output format](#output-format)
- [Adding metadata](#adding-metadata)
- [The object model](#the-object-model)

## How it works

The pipeline runs in three steps:

1. **Parser** — scans the MEOS `.h` header files using libclang and extracts every function signature, struct, and enum into structured JSON.
2. **Merger** — enriches the parser output with manual annotations from `meta/meos-meta.json`, such as documentation and memory ownership rules.
3. **Object model** — makes the *implicit* MEOS class hierarchy explicit: it derives the class lattice and assigns every function to the class it is a method of, from the canonical mapping in `meta/object-model.json`. See [The object model](#the-object-model).

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

In addition, `meos-idl.json` carries an `objectModel` block: the explicit
class lattice (`classes`, `lattice`), the reverse index assigning each
function to the class it is a method of (`functionToClass`), the
closed-algebra companion hierarchies (`companions`), the error contract
(`errors`), and the irregularity worklist (`corrections`).

## Adding metadata

Manual annotations (ownership rules, additional documentation, deprecation flags, etc.) live in `meta/meos-meta.json`. The merger applies them on top of the libclang-parsed structure when generating the final catalog.

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
