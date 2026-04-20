# MEOS IDL Generator

**MEOS IDL Generator** is a tool that allows you to automatically extract the full API of the [MEOS](https://libmeos.org/) C library and save it as a structured JSON file. Instead of manually reading through hundreds of C header files, you run one command and get a clean, complete description of every function, data structure, and enum that MEOS exposes.

This JSON file, `meos-idl.json`, can then be used as a foundation for generating language bindings (Python, JavaScript, Java, Rust...), building documentation, or powering any tooling that needs to understand the MEOS API.

## Table of contents

- [How it works](#how-it-works)
- [Getting started](#getting-started)
- [Output format](#output-format)
- [Adding metadata](#adding-metadata)

## How it works

The tool works in two steps:

1. **Parser**: scans all the needed MEOS `.h` header files using libclang and extracts every function signature, struct, and enum into structured JSON.
2. **Merger**: enriches the result with manual annotations from `meta/meos-meta.json`, such as documentation and memory ownership rules.

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

### 3. Generate the IDL

```bash
python run.py
```

The result is then written to `output/meos-idl.json`.

You can also point the tool at a different headers directory:

```bash
python run.py /path/to/custom/include
```

## Output format

`meos-idl.json` contains 3 top-level arrays: `functions`, `structs`, and `enums`.

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

Fields like `ownership`, `nullable`, and `doc` come from `meos-meta.json` and are merged on top of what the parser found in the C code.

## Adding metadata

To annotate a function with documentation or extra information, edit `meta/meos-meta.json`:

```json
{
  "functions": {
    "my_function": {
      "ownership": "caller",
      "nullable": false,
      "doc": "Returns a new object. The caller is responsible for freeing it."
    }
  }
}
```

Then re-run `python run.py`. The new fields will appear in `meos-idl.json`.