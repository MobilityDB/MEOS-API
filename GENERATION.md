# MEOS-API generation — the catalog producer and the ecosystem chain

MEOS-API is the **root** of the per-binding generator policy: it is the **catalog producer**,
not a generated binding. Every other repo is a projection *of* this catalog.

## The policy (ecosystem-wide)

Every MobilityDB language/surface binding is a **pure projection of the MEOS-API catalog**,
and each binding owns its own generator, in its own repo, in a canonical layout. The single
source of truth is the **catalog** this repo produces: `output/meos-idl.json`, generated from
the MEOS C headers.

## What MEOS-API generates

`run.py <meos/include>` parses the MEOS public headers with libclang and emits
`output/meos-idl.json`: every function, struct, and enum with signatures, ownership, shape
(output arrays / nullability), recovered collapsed C types (`bool`/`int64`/`Timestamp`/…
that the preprocessor flattens to `int`), `@ingroup` groups, the `@sqlfn` SQL-name map, and
the portable bare-name aliases. The `generator/` modules project the catalog onto the
language-**agnostic** service contracts (OpenAPI, MCP, the runtime server, the OGC Moving
Features projection) — the surfaces that need no foreign toolchain. Language bindings live in
their own repos and generate from this catalog.

## The chain (do not invert)

```
MobilityDB pin
  -> MEOS-API   run.py  -> output/meos-idl.json   (+ libmeos.so built from the same pin)
       -> JMEOS  (jar)  -> { MobilitySpark, MobilityFlink, MobilityKafka }
       -> PyMEOS-CFFI   -> PyMEOS
       -> GoMEOS / MEOS.NET / meos-rs / MobilityDuck / MobilityNebula
```

## Producing the catalog by hand

The catalog is one `run.py` invocation over a set of MEOS headers. Two header sources give
different fidelity, and the choice matters:

- the **installed** headers (`cmake --install` output) are self-contained — `meos.h` is the
  generated `meos_export.h`, with `postgres_ext_defs.in.h` spliced in place of the source
  tree's `#include <postgres.h>` — so libclang resolves every struct field to its real C type
  and byte offset. FFI bindings need this.
- the **source** tree headers (`<checkout>/meos/include`) parse without a build, but wrap the
  PostgreSQL types in stubs, so struct layouts are approximate.

Either way `MDB_SRC_ROOT` must point at the MobilityDB *source* checkout, because the Doxygen
`@ingroup` groups and the `@sqlfn` SQL-name map are read from `meos/src`, `mobilitydb/src` and
`mobilitydb/sql`.

```bash
MDB=~/src/MobilityDB          # a checkout at the commit you are deriving from
MEOSAPI=~/src/MEOS-API
pip install -r "$MEOSAPI/requirements.txt"

# Full fidelity: build and install libmeos first, then parse the installed headers.
cmake -S "$MDB" -B "$MDB/build" -DCMAKE_BUILD_TYPE=Release -DMEOS=ON -DALL=ON
cmake --build "$MDB/build" -j"$(nproc)"
cmake --install "$MDB/build" --prefix "$MDB/.prefix"
cd "$MEOSAPI" && MDB_SRC_ROOT="$MDB" python3 run.py "$MDB/.prefix/include"

# Headers only, no build (approximate struct layouts):
cd "$MEOSAPI" && MDB_SRC_ROOT="$MDB" python3 run.py "$MDB/meos/include"
```

Both write `output/meos-idl.json`, which is what every binding consumes. `-DALL=ON` enables
each optional family, so the catalog covers the whole surface; a narrower family set yields a
correspondingly narrower catalog.

Each binding then regenerates from that file through its own entry point — for the JVM
substrate, `JMEOS/tools/regen-from-catalog.sh <catalog>`, which also builds the jar the JVM
consumers bind. See each repo's `GENERATION.md`.

## Sequencing several bindings

`tools/ecosystem-generate.sh <PIN>` builds the catalog and `libmeos.so` from one MobilityDB
ref and then walks the bindings in dependency order. It invokes each binding's own
`tools/regen-from-pin.sh`, and reports and skips any binding that does not have one — JMEOS
regenerates through `tools/regen-from-catalog.sh` and is skipped by this script, so run it
directly as above.

## Consuming MEOS from a binding (provision-meos)

A binding **never commits** `meos-idl.json` (or a `libmeos.so`). Both are derived artifacts of
one MobilityDB commit, and a committed copy is drift waiting to happen. Instead a binding records
the **MobilityDB commit it targets** and derives the catalog — and, for native/FFI bindings, an
installed libmeos — in CI via the shared composite action
`MobilityDB/MEOS-API/.github/actions/provision-meos@master`. One coordinate in, catalog *and*
native library out: they are generated from the same ref every run, so they always match — zero
drift.

The action checks out `MobilityDB@<ref>`, runs `run.py` to emit `output/meos-idl.json`, and
optionally builds and installs all-families libmeos. Its interface:

- inputs: `mobilitydb-ref` (required — SHA or branch), `build-libmeos` (`"true"`/`"false"`,
  default `"false"`), `families` (default `-DALL=ON`, for the optional libmeos build).
- outputs: `catalog-path` (absolute path to the generated `meos-idl.json`) and `libmeos-prefix`
  (`/usr/local` when `build-libmeos=true`, else empty).

### Minimal CI recipe

```yaml
- name: Resolve the MEOS source commit
  id: meos
  run: echo "sha=$(tr -d '[:space:]' < tools/meos-source-commit.txt)" >> "$GITHUB_OUTPUT"
- name: Provision MEOS
  id: provision
  uses: MobilityDB/MEOS-API/.github/actions/provision-meos@master
  with:
    mobilitydb-ref: ${{ steps.meos.outputs.sha }}
    build-libmeos: "true"   # true for native/FFI bindings; false for pure-catalog codegen
# catalog consumers then stage the derived catalog where their generator reads it, e.g.:
#   cp "${{ steps.provision.outputs.catalog-path }}" <path/to/meos-idl.json>
# then run the binding's own generator + tests.
```

`mobilitydb-ref` can be a **pinned SHA** — read from a tracked `meos-source-commit.txt` as above,
which makes the run reproducible — or plain `master` to **track latest**. Either way there is no
drift: the catalog (and libmeos) are regenerated from that same ref in the same run. Native/FFI
bindings pass `build-libmeos: "true"` — libmeos installs under `/usr/local` (`libmeos-prefix`),
and its install also stages `spatial_ref_sys.csv` so SRIDs resolve at runtime. Pure-catalog
bindings leave `build-libmeos` at its default and consume only `catalog-path`.

### Two archetypes

- **Catalog-deriving** — the binding drops its committed `meos-idl.json` and derives it in CI,
  copying `catalog-path` to where its generator reads it before generating sources:
  MobilitySpark (`cp $catalog-path tools/meos-idl.json`, PR #37) and JMEOS (stages to
  `codegen/input/meos-idl.json`, PR #44).
- **libmeos-only** — the binding has no catalog of its own (its facades come from `javap` over
  the JMEOS jar, not from a catalog) and uses the action purely to get libmeos installed with
  `build-libmeos: "true"`: MobilityFlink (PR #41) and MobilityKafka (PR #21).

### Adding a new binding

(a) add the two CI steps above; (b) point your generator at `catalog-path` (or `cp` it into
place); (c) `git rm` any committed `meos-idl.json` / `libmeos.so` and add them to `.gitignore`.
